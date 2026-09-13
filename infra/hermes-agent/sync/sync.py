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
    """True when the recorded ref AND image both match what we are asked for."""
    try:
        rec = json.loads(Path(applied_path).read_text())
    except Exception:
        return False
    return rec.get("ref") == ref and rec.get("image") == image


def clone(dest: Path) -> str | None:
    """Clone REPO_URL at REF into dest. Returns the applied ref, or None."""
    try:
        subprocess.run(["git", "clone", "--no-checkout", REPO_URL, str(dest)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "checkout", REF],
                       check=True, capture_output=True)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return REF
    except subprocess.CalledProcessError as exc:
        log(f"WARNING clone failed: {exc.stderr.decode(errors='replace').strip()[:300]}")
        return None


def apply_cron(home: Path, declared_file: Path) -> None:
    """Merge declared jobs into home/cron/jobs.json by id."""
    if not declared_file.is_file():
        return
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
    except Exception as exc:
        log(f"WARNING cron upsert failed for {home}: {exc}")


def sync_skills(home: Path) -> None:
    """Run the bundled-skill sync for one home (root or a profile)."""
    try:
        subprocess.run(
            [VENV_PY, "-c", "from tools.skills_sync import sync_skills; sync_skills()"],
            env={**os.environ, "HERMES_HOME": str(home)},
            cwd="/opt/hermes", check=True, capture_output=True, timeout=120,
        )
        log(f"skills synced for {home}")
    except Exception as exc:
        log(f"WARNING skills_sync failed for {home}: {exc}")


def copy_root_files(staged: Path) -> None:
    for name in ("SOUL.md", "config.yaml"):
        src = staged / "hermes" / "root" / name
        if src.is_file():
            shutil.copy2(src, HERMES_HOME / name)
            log(f"copied {name}")


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if should_skip(APPLIED, REF, IMAGE):
        log(f"ref {REF} + image already applied; skipping")
        return 0

    if STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)
    applied_ref = clone(STAGING)

    if applied_ref:
        shutil.rmtree(LAST_GOOD, ignore_errors=True)
        shutil.copytree(STAGING, LAST_GOOD)
    elif LAST_GOOD.is_dir():
        log("falling back to last-good tree")
        shutil.rmtree(STAGING, ignore_errors=True)
        shutil.copytree(LAST_GOOD, STAGING)
        try:
            applied_ref = json.loads(APPLIED.read_text()).get("ref")
        except Exception:
            applied_ref = None
    else:
        log("ERROR no clone and no last-good tree; leaving config untouched")
        return 0

    copy_root_files(STAGING)
    apply_cron(HERMES_HOME, STAGING / "hermes" / "root" / "cron" / "jobs.json")
    sync_skills(HERMES_HOME)

    APPLIED.write_text(json.dumps({
        "ref": applied_ref, "image": IMAGE,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "profiles": [], "result": "ok",
    }, indent=2))
    log(f"applied ref {applied_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
