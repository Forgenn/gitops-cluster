import pytest
from cron_upsert import upsert_jobs, MANAGED_BY


def _live(*jobs):
    return {"jobs": list(jobs)}


def test_declared_job_is_added_to_empty_store():
    out = upsert_jobs(_live(), [{"id": "a", "name": "A", "managed_by": MANAGED_BY}])
    assert [j["id"] for j in out["jobs"]] == ["a"]


def test_runtime_fields_survive_a_redeclaration():
    live = _live({
        "id": "a", "name": "old", "managed_by": MANAGED_BY,
        "next_run_at": "2026-09-14T09:00:00+00:00",
        "last_run_at": "2026-09-13T09:00:00+00:00",
        "last_status": "completed", "failure_streak": 2,
        "repeat": {"times": None, "completed": 7},
    })
    out = upsert_jobs(live, [{"id": "a", "name": "new", "managed_by": MANAGED_BY,
                              "repeat": {"times": None, "completed": 0}}])
    job = out["jobs"][0]
    assert job["name"] == "new"                                    # declared wins
    assert job["next_run_at"] == "2026-09-14T09:00:00+00:00"       # runtime preserved
    assert job["last_status"] == "completed"
    assert job["failure_streak"] == 2
    assert job["repeat"]["completed"] == 7                         # counter preserved


def test_undeclared_unmanaged_job_is_never_deleted():
    live = _live({"id": "telegram-made", "name": "renew cert"})
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY}])
    assert sorted(j["id"] for j in out["jobs"]) == ["a", "telegram-made"]


def test_previously_managed_job_absent_from_repo_is_retired():
    live = _live({"id": "gone", "managed_by": MANAGED_BY},
                 {"id": "mine", "name": "hand made"})
    out = upsert_jobs(live, [])
    assert [j["id"] for j in out["jobs"]] == ["mine"]


def test_declared_state_overrides_live_state():
    live = _live({"id": "a", "managed_by": MANAGED_BY, "state": "scheduled"})
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY, "state": "paused"}])
    assert out["jobs"][0]["state"] == "paused"


def test_live_state_is_kept_when_declaration_is_silent():
    live = _live({"id": "a", "managed_by": MANAGED_BY, "state": "paused"})
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY}])
    assert out["jobs"][0]["state"] == "paused"


def test_declared_jobs_are_stamped_even_if_the_file_forgot():
    out = upsert_jobs(_live(), [{"id": "a"}])
    assert out["jobs"][0]["managed_by"] == MANAGED_BY


def test_missing_jobs_key_is_tolerated():
    out = upsert_jobs({}, [{"id": "a"}])
    assert [j["id"] for j in out["jobs"]] == ["a"]


def test_declared_id_collision_with_unmanaged_job_skips_declaration():
    """FINDING 1: A declared id that collides with an unmanaged live job should skip the declaration."""
    live = _live({"id": "shared-id", "name": "hand made by telegram"})
    out = upsert_jobs(live, [{"id": "shared-id", "managed_by": MANAGED_BY}])
    # The unmanaged job should remain completely untouched
    assert len(out["jobs"]) == 1
    assert out["jobs"][0]["id"] == "shared-id"
    assert out["jobs"][0]["name"] == "hand made by telegram"
    # The declared job should NOT be added
    assert out["jobs"][0].get("managed_by") is None


def test_runtime_fields_are_deep_copied_not_aliased():
    """FINDING 2: Runtime fields (object-valued) must not be aliased from caller's live argument."""
    live = _live({
        "id": "a", "managed_by": MANAGED_BY,
        "monitor_state": {"key": "original_value"}
    })
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY}])
    # Modify the output's monitor_state
    out["jobs"][0]["monitor_state"]["key"] = "modified_value"
    # The input's monitor_state should NOT have been modified
    assert live["jobs"][0]["monitor_state"]["key"] == "original_value"


def test_omitted_repeat_preserves_times_from_live():
    """FINDING 3: When declaration omits 'repeat', carry over the previous repeat object's other fields."""
    live = _live({
        "id": "a", "managed_by": MANAGED_BY,
        "repeat": {"times": 5, "completed": 3}
    })
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY}])
    # repeat.times should be preserved from live
    assert out["jobs"][0]["repeat"]["times"] == 5
    # repeat.completed should also be preserved
    assert out["jobs"][0]["repeat"]["completed"] == 3
