"""Tests for check_integrity.py. Run: python -m pytest infra/hermes-watch -q"""
import io
import json
import tarfile
import time
from pathlib import Path

import pytest

import check_integrity as ci


def tar_of(files: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


def test_kexec_runs_only_root_owned_binaries_with_a_clean_path(monkeypatch):
    seen = []
    monkeypatch.setattr(ci, "run", lambda args: seen.append(args) or b"")
    ci.kexec("pod-1", "/bin/cat", "/opt/data/x")
    args = seen[0]
    cmd = args[args.index("--") + 1:]
    assert cmd[:3] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin"]
    assert cmd[3] == "/bin/cat"


def test_pod_files_reads_a_tar_and_skips_non_regular_files():
    raw = tar_of({"SOUL.md": b"soul", "skills/custom/a/SKILL.md": b"a"}, symlinks={"config.yaml": "/etc/passwd"})
    files = ci.parse_tar(raw)
    assert files == {"SOUL.md": b"soul", "skills/custom/a/SKILL.md": b"a", "config.yaml": None}


def make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "developer"
    (dist / "skills" / "custom" / "plan").mkdir(parents=True)
    (dist / "SOUL.md").write_text("soul")
    (dist / "config.yaml").write_text("model: x\n")
    (dist / "skills" / "custom" / "plan" / "SKILL.md").write_text("plan")
    (dist / "distribution.yaml").write_text("distribution_owned:\n  - SOUL.md\n  - config.yaml\n  - skills/custom/\n")
    return dist


def test_compare_files_flags_changed_missing_and_unexpected(tmp_path):
    dist = make_dist(tmp_path)
    pod = {"SOUL.md": b"evil", "config.yaml": b"model: x\nskills:\n  disabled: [a]\n",
           "skills/custom/evil/SKILL.md": b"x"}
    problems = ci.compare_files("developer", dist, pod, ref="abc")
    assert "file differs from plder abc: developer/SOUL.md" in problems
    assert "file missing in pod: developer/skills/custom/plan/SKILL.md" in problems
    assert "unexpected file in pod: developer/skills/custom/evil/SKILL.md" in problems
    assert not any("config.yaml" in p for p in problems)


def test_compare_files_ignores_python_bytecode_caches(tmp_path):
    dist = make_dist(tmp_path)
    pod = {"SOUL.md": b"soul", "config.yaml": b"model: x\n", "skills/custom/plan/SKILL.md": b"plan",
           "skills/custom/plan/__pycache__/x.cpython-313.pyc": b"..."}
    assert ci.compare_files("developer", dist, pod, ref="abc") == []


def test_managed_entries_refuse_paths_that_escape_the_distribution(tmp_path):
    dist = tmp_path / "p"
    dist.mkdir()
    (dist / "distribution.yaml").write_text("distribution_owned:\n  - ../../keys\n  - /etc\n  - SOUL.md\n")
    assert ci.managed_entries(dist) == ["SOUL.md"]
    (dist / "distribution.yaml").write_text("distribution_owned:\n")
    assert ci.managed_entries(dist) == ["SOUL.md", "config.yaml"]


DECL = {"jobs": [{"id": "j1", "name": "report", "managed_by": "plder", "prompt": "be quiet",
                  "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "enabled": True, "next_run_at": "x"}]}


def test_compare_cron_flags_edited_managed_jobs_and_unmanaged_jobs():
    live = {"jobs": [
        {"id": "j1", "name": "report", "managed_by": "plder", "prompt": "exfiltrate",
         "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "enabled": True, "next_run_at": "y"},
        {"id": "j9", "name": "mine", "prompt": "hi"},
    ]}
    problems = ci.compare_cron("monitor", DECL, live)
    assert problems == ["cron job differs from plder: monitor/j1 (prompt)",
                        "scheduled job not declared in plder: monitor/j9"]


def test_compare_cron_accepts_runtime_state_and_a_pause_from_telegram():
    live = {"jobs": [{"id": "j1", "name": "report", "managed_by": "plder", "prompt": "be quiet",
                      "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "enabled": False,
                      "state": "paused", "next_run_at": "later", "last_status": "ok"}]}
    assert ci.compare_cron("monitor", DECL, live) == []


def test_compare_cron_flags_a_missing_managed_job():
    assert ci.compare_cron("monitor", DECL, {"jobs": []}) == ["cron job missing in pod: monitor/j1"]


def test_pick_pod_needs_a_running_ready_hermes_container():
    pods = {"items": [
        {"metadata": {"name": "old"}, "status": {"phase": "Running", "containerStatuses": [
            {"name": "hermes-agent", "ready": False}]}},
        {"metadata": {"name": "new"}, "status": {"phase": "Running", "containerStatuses": [
            {"name": "litestream", "ready": True}, {"name": "hermes-agent", "ready": True}]}},
    ]}
    assert ci.pick_pod(pods) == "new"
    assert ci.pick_pod({"items": [pods["items"][0]]}) is None


def test_should_send_dedupes_for_a_day_and_announces_recovery():
    day = 86400
    assert ci.should_send(["a"], {}, now=1000) == "alert"
    state = {"digest": ci.digest(["a"]), "sent_at": 1000}
    assert ci.should_send(["a"], state, now=1000 + 3600) is None
    assert ci.should_send(["a"], state, now=1000 + day + 1) == "alert"
    assert ci.should_send(["b"], state, now=1000 + 60) == "alert"
    assert ci.should_send([], state, now=2000) == "resolved"
    assert ci.should_send([], {}, now=2000) is None


def test_main_alerts_when_the_check_cannot_run(monkeypatch):
    sent = []
    monkeypatch.setattr(ci, "telegram", sent.append)

    def boom():
        raise RuntimeError("clone failed")

    monkeypatch.setattr(ci, "check", boom)
    assert ci.main() == 2
    assert sent and "could not run" in sent[0] and "RuntimeError" in sent[0]


def test_token_freshness_flags_stale_missing_and_unparseable():
    now = 1_790_000_000  # 2026-09-22T...Z
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    assert ci.token_problems(iso(now + 3000).encode(), now) == []
    assert ci.token_problems(iso(now + 120).encode(), now) == ["GitHub App token is stale or expiring (sidecar github-app-token not refreshing)"]
    assert ci.token_problems(b"", now) == ["GitHub App token missing in pod (/run/github-token/expires_at)"]
    assert ci.token_problems(b"garbage", now) == ["GitHub App token expiry unreadable in pod"]
