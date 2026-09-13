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


def upsert_jobs_with_conflicts(
    live: Dict[str, Any], declared: List[Dict[str, Any]]
) -> tuple[Dict[str, Any], List[str]]:
    """Return a merged jobs document and list of conflicting (skipped) declared ids.

    - A declared job replaces its live namesake field-for-field, except that
      RUNTIME_FIELDS and repeat.completed carry over from the live copy.
    - ``state`` carries over too, unless the declaration sets it explicitly —
      that is how a job is paused or resumed from git.
    - A live job with no declaration survives untouched when it is not ours,
      and is retired when it is (it was declared once and has since been
      removed from the repo).
    - REFUSE to adopt an unmanaged live job. If a declared id matches a live
      job that is NOT already managed_by == MANAGED_BY, the declared job is
      skipped entirely (not added, not merged, not stamped). This preserves
      data: the operator can deliberately adopt by deleting the live job first.
    """
    live_jobs = {j["id"]: j for j in (live or {}).get("jobs", [])}
    merged: List[Dict[str, Any]] = []
    conflicts: List[str] = []
    processed_ids: set[str] = set()  # Track which declared ids were actually processed

    for decl in declared:
        prev = live_jobs.get(decl["id"])

        # FINDING 1: Refuse to adopt unmanaged jobs
        if prev is not None and prev.get("managed_by") != MANAGED_BY:
            # Skip this declaration and collect the conflict
            conflicts.append(decl["id"])
            continue

        job = copy.deepcopy(decl)
        job["managed_by"] = MANAGED_BY
        processed_ids.add(decl["id"])

        if prev is not None:
            for field in RUNTIME_FIELDS:
                if field in prev:
                    # FINDING 2: Deep-copy runtime fields to avoid aliasing
                    job[field] = copy.deepcopy(prev[field])
            if "state" not in decl and "state" in prev:
                job["state"] = prev["state"]

            # FINDING 3: Preserve repeat fields from live (especially times)
            if prev.get("repeat") is not None or "repeat" in decl:
                # Start with a deep copy of the previous repeat dict if it exists
                merged_repeat = copy.deepcopy(prev.get("repeat", {}))
                # Merge in any declared repeat fields (but preserve completed from live)
                if "repeat" in decl:
                    declared_repeat = decl.get("repeat", {})
                    for key, value in declared_repeat.items():
                        if key != "completed":  # Never override completed
                            merged_repeat[key] = value
                job["repeat"] = merged_repeat

        merged.append(job)

    for job_id, job in live_jobs.items():
        if job_id in processed_ids:
            continue
        if job.get("managed_by") == MANAGED_BY:
            continue  # retired: we declared it once, the repo dropped it
        merged.append(job)

    return {"jobs": merged}, conflicts


def upsert_jobs(live: Dict[str, Any], declared: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Thin wrapper around upsert_jobs_with_conflicts that discards the conflict list.

    See upsert_jobs_with_conflicts for full documentation.
    """
    doc, _ = upsert_jobs_with_conflicts(live, declared)
    return doc
