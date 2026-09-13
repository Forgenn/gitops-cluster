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
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cron_upsert import upsert_jobs_with_conflicts

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_DIR = HERMES_HOME / ".agent-config"
APPLIED = STATE_DIR / "applied"
LAST_GOOD = STATE_DIR / "last-good"
STAGING = Path("/staging")
REPO_URL = os.environ.get("AGENT_CONFIG_REPO", "git@github.com-plder:Forgenn/plder.git")
REF = os.environ.get("AGENT_CONFIG_REF", "")
IMAGE = os.environ.get("AGENT_IMAGE", "")
VENV_PY = "/opt/hermes/.venv/bin/python"


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
    except subprocess.TimeoutExpired as exc:
        log(f"WARNING clone timed out after 180s")
        return None
    except subprocess.CalledProcessError as exc:
        log(f"WARNING clone failed: {exc.stderr.decode(errors='replace').strip()[:300]}")
        return None
    except Exception as exc:
        log(f"WARNING clone failed: {type(exc).__name__}: {str(exc)[:300]}")
        return None


def apply_cron(home: Path, declared_file: Path) -> bool:
    """Merge declared jobs into home/cron/jobs.json by id. Returns True on success."""
    if not declared_file.is_file():
        return True  # Nothing to do is success
    try:
        declared = json.loads(declared_file.read_text()).get("jobs", [])
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


def copy_root_files(staged: Path) -> bool:
    """Copy root files from staged tree. Returns True on success."""
    try:
        for name in ("SOUL.md", "config.yaml"):
            src = staged / "hermes" / "root" / name
            if src.is_file():
                shutil.copy2(src, HERMES_HOME / name)
                log(f"copied {name}")
        return True
    except Exception as exc:
        log(f"WARNING copy_root_files failed: {exc}")
        return False


def main() -> int:
    try:
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
                shutil.copytree(LAST_GOOD, STAGING)
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
        steps_ok = sync_skills(HERMES_HOME) and steps_ok

        result = "ok" if steps_ok else "partial"
        try:
            APPLIED.write_text(json.dumps({
                "ref": applied_ref, "image": IMAGE,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "profiles": [], "result": result,
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
