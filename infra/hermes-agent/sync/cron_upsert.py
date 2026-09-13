"""Merge declared cron jobs into a live Hermes jobs.json.

Hermes rewrites jobs.json on every scheduler tick (next_run_at, last_run_at,
last_status, failure_streak, repeat.completed), and users create jobs
conversationally from Telegram. A declarative overwrite would therefore both
reset the scheduler and delete the user's own jobs. This merges by id instead.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

MANAGED_BY = "plder"

# Fields Hermes owns at runtime; never clobbered by a redeclaration.
RUNTIME_FIELDS: tuple[str, ...] = (
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
    "failure_streak",
    "monitor_state",
    "paused_at",
    "paused_reason",
    "created_at",
)


def upsert_jobs(live: Dict[str, Any], declared: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a merged jobs document.

    - A declared job replaces its live namesake field-for-field, except that
      RUNTIME_FIELDS and repeat.completed carry over from the live copy.
    - ``state`` carries over too, unless the declaration sets it explicitly —
      that is how a job is paused or resumed from git.
    - A live job with no declaration survives untouched when it is not ours,
      and is retired when it is (it was declared once and has since been
      removed from the repo).
    """
    live_jobs = {j["id"]: j for j in (live or {}).get("jobs", [])}
    declared_ids = {d["id"] for d in declared}
    merged: List[Dict[str, Any]] = []

    for decl in declared:
        job = copy.deepcopy(decl)
        job["managed_by"] = MANAGED_BY
        prev = live_jobs.get(job["id"])
        if prev is not None:
            for field in RUNTIME_FIELDS:
                if field in prev:
                    job[field] = prev[field]
            if "state" not in decl and "state" in prev:
                job["state"] = prev["state"]
            if "completed" in (prev.get("repeat") or {}):
                job.setdefault("repeat", {})["completed"] = prev["repeat"]["completed"]
        merged.append(job)

    for job_id, job in live_jobs.items():
        if job_id in declared_ids:
            continue
        if job.get("managed_by") == MANAGED_BY:
            continue  # retired: we declared it once, the repo dropped it
        merged.append(job)

    return {"jobs": merged}
