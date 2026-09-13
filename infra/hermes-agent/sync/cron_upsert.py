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

# Fields Hermes owns at runtime; a declaration may never set them.
#
# NOTE ON ITS ROLE: this used to be the allowlist of live fields that survived a
# redeclaration, which meant every field Hermes writes and the repo does not
# declare was silently dropped on each sync -- confirmed to include run_claim,
# last_delivery_error, provider_snapshot, model_snapshot, base_url, origin,
# enabled_toolsets and the conditionally-persisted attach_to_session and
# reasoning_effort, and by construction anything a future image adds. The merge
# is now inverted (live job first, declared keys overlaid on top), so unknown
# live fields survive without being listed anywhere.
#
# The tuple is kept because it still has a job, an inverted one: it is now a
# DENYLIST of fields a declaration is not allowed to set. jobs.json declarations
# are realistically produced by copying a live job out of the pod, and such a
# copy carries scheduler state; overlaying it would rewind next_run_at and reset
# failure_streak on every sync. Live always wins for these.
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

    - A redeclaration STARTS FROM THE LIVE JOB and overlays the declared keys on
      top. Every live field the repo does not mention survives by construction —
      including ones neither this module nor the declaration has ever heard of.
      (The reverse — building from the declaration and copying an allowlist of
      live fields back — quietly destroyed everything outside that allowlist.)
    - RUNTIME_FIELDS are then re-applied from the live job: a declaration may
      not set scheduler state even if it names it.
    - ``state`` carries over unless the declaration sets it explicitly — that is
      how a job is paused or resumed from git. This now holds by construction:
      a declaration that is silent about ``state`` cannot overwrite it.
    - ``repeat`` is merged key-by-key rather than replaced, and live
      ``repeat.completed`` always wins; the declared value is used only to seed
      a counter the live job does not have yet.
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

        processed_ids.add(decl["id"])

        if prev is None:
            job = copy.deepcopy(decl)
        else:
            # INVERTED MERGE: the live job is the base, the declaration is an
            # overlay. Anything Hermes wrote and the repo does not declare is
            # carried through untouched -- no allowlist to keep in sync with the
            # image. Deep-copied throughout so nothing aliases the caller's live
            # document.
            job = copy.deepcopy(prev)
            for key, value in decl.items():
                job[key] = copy.deepcopy(value)

        job["managed_by"] = MANAGED_BY

        if prev is not None:
            # Special rule 1: scheduler state is live-owned. Re-applied AFTER the
            # overlay so a declaration carrying stale runtime fields (e.g. copied
            # out of a live jobs.json) cannot rewind the scheduler.
            for field in RUNTIME_FIELDS:
                if field in prev:
                    job[field] = copy.deepcopy(prev[field])

            # `state`: declared wins only when the declaration sets it
            # explicitly, otherwise the live state survives. Guaranteed by the
            # overlay above -- asserted here only as documentation of intent.

            # Special rule 2: `repeat` is merged, not replaced, and live
            # `completed` takes precedence. Declared `completed` is used only to
            # seed a counter the live job does not have; if neither side has one
            # the key stays absent.
            if prev.get("repeat") is not None or "repeat" in decl:
                # Start with a deep copy of the previous repeat dict if it exists.
                # `or {}` not a default: a non-repeating live job stores an
                # explicit "repeat": null, and dict.get would hand back None.
                merged_repeat = copy.deepcopy(prev.get("repeat") or {})
                # Merge in any declared repeat fields (but preserve completed from live)
                if "repeat" in decl:
                    declared_repeat = decl.get("repeat") or {}
                    for key, value in declared_repeat.items():
                        if key != "completed":  # Never override completed
                            merged_repeat[key] = value
                    # For completed: live wins if it has one, otherwise use declared if it has one
                    if "completed" not in merged_repeat and "completed" in declared_repeat:
                        merged_repeat["completed"] = declared_repeat["completed"]
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
