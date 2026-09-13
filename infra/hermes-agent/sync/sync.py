#!/usr/bin/env python3
# infra/hermes-agent/sync/sync.py
"""Apply declared Hermes config from a plder checkout onto /opt/data.

Runs as an initContainer before the gateway starts. It must NEVER fail the
pod: the agent is the operator's primary interface, and a config problem
must not take it offline. Every failure path logs and continues.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cron_upsert import MANAGED_BY, upsert_jobs_with_conflicts

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_DIR = HERMES_HOME / ".agent-config"
APPLIED = STATE_DIR / "applied"
LAST_GOOD = STATE_DIR / "last-good"
STAGING = Path("/staging")
REPO_URL = os.environ.get("AGENT_CONFIG_REPO", "git@github.com-plder:Forgenn/plder.git")
REF = os.environ.get("AGENT_CONFIG_REF", "")
IMAGE = os.environ.get("AGENT_IMAGE", "")
VENV_PY = "/opt/hermes/.venv/bin/python"
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
# Same shape Hermes itself enforces for profile names: lowercase, digits, dashes.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def log(msg: str) -> None:
    print(f"[profile-sync] {msg}", flush=True)


def should_skip(applied_path: Path, ref: str, image: str) -> bool:
    """True when the recorded ref AND image both match AND result is 'ok'."""
    try:
        rec = json.loads(Path(applied_path).read_text())
    except Exception:
        return False
    return (rec.get("ref") == ref and rec.get("image") == image
            and rec.get("result") == "ok")


def clone(dest: Path) -> str | None:
    """Clone REPO_URL at REF into dest. Returns the applied ref, or None."""
    try:
        subprocess.run(["git", "clone", "--no-checkout", REPO_URL, str(dest)],
                       check=True, capture_output=True, timeout=180)
        subprocess.run(["git", "-C", str(dest), "checkout", REF],
                       check=True, capture_output=True, timeout=180)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return REF
    except subprocess.TimeoutExpired:
        log("WARNING clone timed out after 180s")
        return None
    except subprocess.CalledProcessError as exc:
        log(f"WARNING clone failed: {exc.stderr.decode(errors='replace').strip()[:300]}")
        return None
    except Exception as exc:
        log(f"WARNING clone failed: {type(exc).__name__}: {str(exc)[:300]}")
        return None


def sanitize_declared(declared: list) -> list:
    """Drop malformed declarations instead of aborting the whole cron apply.

    Two shapes are handled, both of which used to be silently destructive:
    a job with no ``id`` raised KeyError inside the upsert and killed the entire
    cron step (every other declared job lost, result flipped to "partial"), and
    a duplicate id produced two entries for the same job in jobs.json.
    """
    clean: list = []
    seen: set = set()
    for entry in declared:
        if not isinstance(entry, dict) or not entry.get("id"):
            log(f"WARNING skipping declared job with no id: {str(entry)[:120]}")
            continue
        if entry["id"] in seen:
            log(f"WARNING skipping duplicate declared job id: {entry['id']}")
            continue
        seen.add(entry["id"])
        clean.append(entry)
    return clean


def apply_cron(home: Path, declared_file: Path) -> bool:
    """Merge declared jobs into home/cron/jobs.json by id. Returns True on success."""
    if not declared_file.is_file():
        return True  # Nothing to do is success
    try:
        declared = sanitize_declared(json.loads(declared_file.read_text()).get("jobs", []))
        live_file = home / "cron" / "jobs.json"
        live = json.loads(live_file.read_text()) if live_file.is_file() else {}
        merged, conflicts = upsert_jobs_with_conflicts(live, declared)
        if conflicts:
            log(f"WARNING {len(conflicts)} declared job(s) skipped — id already used by a hand-made job: {', '.join(conflicts)}")
        live_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = live_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2))
        tmp.replace(live_file)
        log(f"cron: {len(merged['jobs'])} job(s) in {live_file}")
        return True
    except Exception as exc:
        log(f"WARNING cron upsert failed for {home}: {exc}")
        return False


def sync_skills(home: Path) -> bool:
    """Run the bundled-skill sync for one home (root or a profile). Returns True on success."""
    try:
        subprocess.run(
            [VENV_PY, "-c", "from tools.skills_sync import sync_skills; sync_skills()"],
            env={**os.environ, "HERMES_HOME": str(home)},
            cwd="/opt/hermes", check=True, capture_output=True, timeout=120,
        )
        log(f"skills synced for {home}")
        return True
    except Exception as exc:
        log(f"WARNING skills_sync failed for {home}: {exc}")
        return False


# DELIBERATELY EXCLUDES config.yaml. It stays ConfigMap-owned until this sync is
# proven in production, for two reasons:
#
#   1. Copying it here does nothing. The main container bind-mounts the
#      hermes-config ConfigMap read-only over /opt/data/config.yaml via subPath,
#      so a file written here is masked and the agent never reads it.
#   2. Copying it here actively breaks the sync. The copy target is the
#      kubelet-created subPath stub, owned by root; shutil.copy2 as uid 10000
#      raises PermissionError, copy_root_files returns False, result becomes
#      "partial", should_skip therefore never fires, and every single restart
#      re-clones from GitHub.
#
# Retiring the ConfigMap mount is the right end state and is tracked in the plan,
# but not in this phase: if the clone soft-fails on a fresh PVC there would then
# be no config.yaml at all and the agent would boot unconfigured.
ROOT_FILES: tuple[str, ...] = ("SOUL.md",)


def copy_root_files(staged: Path) -> bool:
    """Copy root files from staged tree. Returns True on success."""
    try:
        for name in ROOT_FILES:
            src = staged / "hermes" / "root" / name
            if src.is_file():
                shutil.copy2(src, HERMES_HOME / name)
                log(f"copied {name}")
        return True
    except Exception as exc:
        log(f"WARNING copy_root_files failed: {exc}")
        return False


def _apply_cron_after_failed_install(name: str, src: Path) -> None:
    """Apply a profile's declared cron even though its install just failed.

    main() applies the root cron BEFORE installing profiles, so a job moved from
    the root declaration to a profile's has already been retired from the root
    store by now. Skipping the profile's cron on a failed install would leave
    that job in NEITHER store. When the profile already exists on the volume
    (installed on an earlier boot) its store is a legitimate target, so apply
    it anyway; the caller has already marked the run not-ok. A profile with no
    directory yet gets nothing: there is no installed profile to hold a store.
    """
    home = HERMES_HOME / "profiles" / name
    if home.is_dir():
        log(f"profile {name} already exists; applying its cron despite the failed install")
        apply_cron(home, src / "cron" / "jobs.json")


def install_profiles(staged: Path) -> tuple[list[str], bool]:
    """Install every declared profile distribution, then apply its cron.

    A declared profile is a directory under staged/hermes/profiles/ containing a
    distribution.yaml. `hermes profile install --force` copies only the paths the
    manifest lists and never touches user-owned data (memories, sessions, .env).
    A failed install still applies the cron of a profile that already exists
    (see _apply_cron_after_failed_install) but never counts as installed.
    Returns (installed profile names, all_ok). Never raises.
    """
    root = staged / "hermes" / "profiles"
    installed: list[str] = []
    ok = True
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    except Exception as exc:
        log(f"WARNING could not list declared profiles in {root}: {exc}")
        return installed, False

    for src in candidates:
        name = src.name
        if not (src / "distribution.yaml").is_file():
            continue
        if not _PROFILE_NAME_RE.match(name):
            log(f"WARNING skipping declared profile with an invalid name: {name!r}")
            ok = False
            continue
        try:
            subprocess.run(
                [HERMES_BIN, "profile", "install", str(src), "--name", name, "--force", "-y"],
                env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
                check=True, capture_output=True, timeout=180,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip()[:300]
            log(f"WARNING profile install failed for {name}: {detail}")
            ok = False
            _apply_cron_after_failed_install(name, src)
            continue
        except Exception as exc:
            log(f"WARNING profile install failed for {name}: {type(exc).__name__}: {str(exc)[:300]}")
            ok = False
            _apply_cron_after_failed_install(name, src)
            continue

        installed.append(name)
        log(f"installed profile {name}")
        home = HERMES_HOME / "profiles" / name
        ok = apply_cron(home, src / "cron" / "jobs.json") and ok
        # Log-only, for the same reason as the root call in main(): a transient
        # skills failure must not flip result to "partial" and force a re-clone
        # on every restart. Unlike root, profiles get NO sync from stage2-hook,
        # so this call is what finally keeps their bundled skills current.
        sync_skills(home)
    return installed, ok


def declared_profile_names(staged: Path) -> set[str] | None:
    """Names of the profiles whose cron this run declares, or None if unknown.

    A profile counts only when it has a distribution.yaml, a valid name AND a
    cron/jobs.json: a profile that loses its cron declaration has declared no
    jobs, so its plder jobs must be retired like those of a removed profile.
    None means the staged tree could not be listed; the caller must then retire
    nothing, because an unreadable tree is not a declaration of zero profiles.
    """
    root = staged / "hermes" / "profiles"
    try:
        if not root.is_dir():
            return set()
        return {
            p.name for p in root.iterdir()
            if p.is_dir()
            and _PROFILE_NAME_RE.match(p.name)
            and (p / "distribution.yaml").is_file()
            and (p / "cron" / "jobs.json").is_file()
        }
    except Exception as exc:
        log(f"WARNING could not list declared profiles in {root}: {exc}")
        return None


def _live_profile_dirs() -> list[Path]:
    root = HERMES_HOME / "profiles"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _is_managed(job) -> bool:
    return isinstance(job, dict) and job.get("managed_by") == MANAGED_BY


def retire_undeclared_profile_jobs(declared: set[str]) -> bool:
    """Retire plder-managed jobs from every live profile not declared this run.

    This is the empty-declaration merge (upsert_jobs_with_conflicts(live, []))
    written as a plain filter, for two reasons: it keeps every other live job
    exactly as it is, including entries the id-keyed merge would collapse, and
    it keeps the store's other top-level keys (Hermes writes `updated_at`).
    The file is rewritten ONLY when a managed job was actually removed, so an
    undeclared profile with no plder jobs -- e.g. a bot created in Hermes
    Desktop -- is never touched. Returns False only if a store that needed a
    write could not be written. Never raises.
    """
    ok = True
    try:
        homes = _live_profile_dirs()
    except Exception as exc:
        log(f"WARNING could not list live profiles: {exc}")
        return False
    for home in homes:
        if home.name in declared:
            continue
        live_file = home / "cron" / "jobs.json"
        # Missing or unreadable store: nothing to do, and deliberately NOT a
        # failure. Voting here would let one corrupt hand-made store (not ours)
        # pin result at "partial" and force a re-clone on every boot.
        try:
            if not live_file.is_file():
                continue
            live = json.loads(live_file.read_text())
        except Exception as exc:
            log(f"could not read the cron store of undeclared profile {home.name}; left as is: {exc}")
            continue
        jobs = live.get("jobs") if isinstance(live, dict) else None
        if not isinstance(jobs, list):
            continue
        retired = [j.get("id") for j in jobs if _is_managed(j)]
        if not retired:
            continue
        new = dict(live)
        new["jobs"] = [j for j in jobs if not _is_managed(j)]
        try:
            tmp = live_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(new, indent=2))
            tmp.replace(live_file)
            log(f"cron: retired {len(retired)} plder job(s) from undeclared profile "
                f"{home.name}: {', '.join(str(i) for i in retired)}")
        except Exception as exc:
            log(f"WARNING could not retire plder jobs for undeclared profile {home.name}: {exc}")
            ok = False
    return ok


def warn_unserved_profiles(staged: Path) -> None:
    """Warn about live profiles whose enabled cron jobs will never run.

    Under gateway multiplex only allowlisted profiles are served, so a profile
    created outside git (e.g. in Hermes Desktop) shows a valid next_run_at and
    silently never runs. The allowlist is read from the staged root config, the
    declared mirror of the live ConfigMap. Warning only: never writes, never
    affects the result, never raises. No config, no key, or an unreadable
    file means no warning.
    """
    try:
        # Present in the image's venv. Imported lazily so a missing module can
        # only ever skip this warning, never stop the script from starting.
        import yaml
        cfg_file = staged / "hermes" / "root" / "config.yaml"
        if not cfg_file.is_file():
            return
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        gateway = cfg.get("gateway") if isinstance(cfg, dict) else None
        allowlist = gateway.get("multiplex_profile_allowlist") if isinstance(gateway, dict) else None
        if not isinstance(allowlist, list):
            return
        allowed = {str(name) for name in allowlist}
        homes = _live_profile_dirs()
    except Exception as exc:
        log(f"could not check the multiplex allowlist: {type(exc).__name__}: {exc}")
        return
    for home in homes:
        if home.name in allowed:
            continue
        try:
            live = json.loads((home / "cron" / "jobs.json").read_text())
            jobs = live.get("jobs") if isinstance(live, dict) else None
            if isinstance(jobs, list) and any(
                    isinstance(j, dict) and j.get("enabled") is True for j in jobs):
                log(f"WARNING profile {home.name} has enabled cron jobs that will not run: "
                    f"it is not in gateway.multiplex_profile_allowlist")
        except Exception:
            continue


def main() -> int:
    try:
        # Early guard: no REF means nothing is declared to apply
        if not REF:
            log("no AGENT_CONFIG_REF set; nothing to apply")
            return 0

        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(f"WARNING STATE_DIR.mkdir failed: {exc}")
            return 0

        if should_skip(APPLIED, REF, IMAGE):
            log(f"ref {REF} + image already applied; skipping")
            return 0

        if STAGING.exists():
            shutil.rmtree(STAGING, ignore_errors=True)
        applied_ref = clone(STAGING)

        if applied_ref:
            # No dirs_exist_ok here, on purpose. LAST_GOOD is an ordinary
            # directory on the PVC, so the rmtree above really does remove it --
            # it does not have STAGING's mount-point problem. If the rmtree ever
            # DID fail, merging a new tree into the remains of an older one would
            # produce a hybrid last-good that a future offline boot would apply.
            # Failing here instead degrades safely: we lose the fallback tree for
            # this boot and still apply the fresh clone.
            shutil.rmtree(LAST_GOOD, ignore_errors=True)
            try:
                shutil.copytree(STAGING, LAST_GOOD)
            except Exception as exc:
                log(f"WARNING failed to save last-good tree: {exc}")
                # Delete partial tree to avoid corruption
                shutil.rmtree(LAST_GOOD, ignore_errors=True)
                # Still try to apply the current staging
        elif LAST_GOOD.is_dir():
            log("falling back to last-good tree")
            shutil.rmtree(STAGING, ignore_errors=True)
            try:
                # dirs_exist_ok is MANDATORY here: /staging is an emptyDir MOUNT
                # POINT. rmtree empties it but cannot remove the directory itself
                # (EBUSY, swallowed by ignore_errors), so the directory always
                # still exists at this line. Without dirs_exist_ok this raised
                # FileExistsError every time and the whole offline-resilience
                # path was dead code.
                shutil.copytree(LAST_GOOD, STAGING, dirs_exist_ok=True)
            except Exception as exc:
                log(f"WARNING failed to restore last-good tree: {exc}")
                # Delete partial tree to avoid corruption
                shutil.rmtree(STAGING, ignore_errors=True)
                return 0
            try:
                applied_ref = json.loads(APPLIED.read_text()).get("ref")
            except Exception:
                applied_ref = None
        else:
            log("ERROR no clone and no last-good tree; leaving config untouched")
            return 0

        # Track success/failure of each step
        steps_ok = True
        steps_ok = copy_root_files(STAGING) and steps_ok
        steps_ok = apply_cron(HERMES_HOME, STAGING / "hermes" / "root" / "cron" / "jobs.json") and steps_ok
        # DELIBERATELY NOT part of steps_ok. docker/stage2-hook.sh already runs
        # this exact command for the root home on every container start and
        # treats failure as `|| warn`; this phase has no profile homes, so the
        # call here is a redundant repeat kept only for symmetry with the
        # profile-aware version to come. Letting it vote would be actively
        # harmful: should_skip requires result == "ok", so a single transient
        # failure would write result: "partial" and every restart from then on
        # would re-clone from GitHub -- making the agent's boot depend on
        # network reachability for no benefit. Log-only, matching the hook.
        sync_skills(HERMES_HOME)

        # Profiles run AFTER the root cron apply on purpose: moving a job from the
        # root declaration to a profile's must retire it from the root store and
        # create it in the profile store within the same run.
        profiles, profiles_ok = install_profiles(STAGING)
        steps_ok = profiles_ok and steps_ok

        # A profile removed from plder (or stripped of its manifest or cron
        # declaration) must not keep running its plder jobs. Runs after the
        # declared profiles so it only ever sees the leftovers.
        declared = declared_profile_names(STAGING)
        if declared is None:
            log("WARNING declared profiles unknown; not retiring jobs of undeclared profiles")
        else:
            steps_ok = retire_undeclared_profile_jobs(declared) and steps_ok

        warn_unserved_profiles(STAGING)

        result = "ok" if steps_ok else "partial"
        try:
            APPLIED.write_text(json.dumps({
                "ref": applied_ref, "image": IMAGE,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "profiles": profiles, "result": result,
            }, indent=2))
            log(f"applied ref {applied_ref} (result: {result})")
        except Exception as exc:
            log(f"WARNING failed to write APPLIED record: {exc}")

        return 0
    except Exception as exc:
        log(f"ERROR main() caught exception: {type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
