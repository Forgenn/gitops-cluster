"""Test suite for sync.py config sync entrypoint.

CRITICAL: All tests in this module use an autouse fixture that isolates main()
to tmp_path, rebinding module-level path globals (HERMES_HOME, STATE_DIR, etc).
This is essential because sync.py resolves these paths at module import time,
and a developer machine may have HERMES_HOME pointing at a real Hermes Desktop
installation. A bare main() call without isolation would operate on the live
system. The autouse fixture makes isolation the default — a future developer
cannot accidentally call main() unsafely.
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock
from sync import should_skip, main
import shutil
import pytest
import sync as sync_module


def _write(tmp_path: Path, ref: str, image: str, result: str = "ok",
           sync_version=None) -> Path:
    p = tmp_path / "applied"
    p.write_text(json.dumps({
        "ref": ref, "image": image, "result": result,
        "sync_version": sync_module.SYNC_VERSION if sync_version is None else sync_version,
    }))
    return p


@pytest.fixture(autouse=True)
def isolated_main(tmp_path, tmp_path_factory, monkeypatch):
    """Fixture that isolates main() to use tmp_path instead of live system paths.

    AUTOUSE: This fixture automatically applies to every test in this module.
    Monkeypatches all module-level path globals so tests are hermetic and cannot
    accidentally modify the operator's live Hermes installation.

    STAGING MODELS AN emptyDir MOUNT POINT, which is what /staging is in the pod:

      * it already exists before sync.py runs (the fixture used to point STAGING
        at a path that did not exist, which is exactly why the dead last-good
        fallback went unnoticed);
      * it is a separate filesystem from /opt/data, so it is created outside
        tmp_path and tests asserting "main() created nothing under HERMES_HOME"
        still mean that;
      * shutil.rmtree can EMPTY it but can never REMOVE it -- the final rmdir
        gets EBUSY, which ignore_errors=True swallows, so the directory is
        still standing afterwards. Without this last part a test cannot
        reproduce the FileExistsError that made the fallback dead code on every
        real boot, because a plain tmp directory really does get removed.
    """
    state_dir = tmp_path / ".agent-config"
    staging = tmp_path_factory.mktemp("staging-mount")
    monkeypatch.setattr(sync_module, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(sync_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(sync_module, "APPLIED", state_dir / "applied")
    monkeypatch.setattr(sync_module, "LAST_GOOD", state_dir / "last-good")
    monkeypatch.setattr(sync_module, "STAGING", staging)

    real_rmtree = shutil.rmtree

    def mount_point_aware_rmtree(path, *args, **kwargs):
        if Path(path) == staging and staging.is_dir():
            for child in staging.iterdir():
                if child.is_dir() and not child.is_symlink():
                    real_rmtree(child, *args, **kwargs)
                else:
                    child.unlink()
            return  # the mount point itself survives, as in the pod
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", mount_point_aware_rmtree)
    return main


def test_skips_when_ref_and_image_both_match(tmp_path):
    p = _write(tmp_path, "abc123", "img:v1")
    assert should_skip(p, "abc123", "img:v1") is True


def test_does_not_skip_when_ref_changed(tmp_path):
    p = _write(tmp_path, "abc123", "img:v1")
    assert should_skip(p, "def456", "img:v1") is False


def test_does_not_skip_when_image_changed(tmp_path):
    p = _write(tmp_path, "abc123", "img:v1")
    assert should_skip(p, "abc123", "img:v2") is False


def test_does_not_skip_when_no_record_exists(tmp_path):
    assert should_skip(tmp_path / "absent", "abc123", "img:v1") is False


def test_corrupt_record_does_not_skip(tmp_path):
    p = tmp_path / "applied"
    p.write_text("{not json")
    assert should_skip(p, "abc123", "img:v1") is False


def test_does_not_skip_when_result_is_partial(tmp_path):
    """A record with result='partial' must NOT skip even if ref and image match."""
    p = _write(tmp_path, "abc123", "img:v1", "partial")
    assert should_skip(p, "abc123", "img:v1") is False


def test_skips_only_when_result_ok_and_ref_image_match(tmp_path):
    """Only when result='ok' AND ref+image match should we skip."""
    p = _write(tmp_path, "abc123", "img:v1", "ok")
    assert should_skip(p, "abc123", "img:v1") is True


def test_does_not_skip_a_record_written_by_an_older_sync(tmp_path):
    """A change to what a sync writes must re-apply once, even at the same ref."""
    p = _write(tmp_path, "abc123", "img:v1", sync_version=sync_module.SYNC_VERSION - 1)
    assert should_skip(p, "abc123", "img:v1") is False


def test_does_not_skip_a_record_with_no_sync_version(tmp_path):
    """Records from before SYNC_VERSION existed (the live pod's today) re-apply."""
    p = tmp_path / "applied"
    p.write_text(json.dumps({"ref": "abc123", "image": "img:v1", "result": "ok"}))
    assert should_skip(p, "abc123", "img:v1") is False


def test_main_with_empty_ref_creates_nothing(isolated_main, tmp_path, monkeypatch):
    """With empty REF, main() returns 0 and creates nothing."""
    monkeypatch.setattr(sync_module, "REF", "")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    result = isolated_main()

    assert result == 0
    # Verify nothing was created
    assert len(list(tmp_path.iterdir())) == 0


def test_main_with_unset_ref_creates_nothing(isolated_main, tmp_path, monkeypatch):
    """With unset REF (empty string via environ), main() returns 0 and creates nothing."""
    monkeypatch.setattr(sync_module, "REF", "")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    result = isolated_main()

    assert result == 0
    # Verify nothing was created in tmp_path
    assert len(list(tmp_path.iterdir())) == 0


def test_main_returns_zero_on_mkdir_failure(isolated_main, tmp_path, monkeypatch):
    """main() must return 0 even when STATE_DIR.mkdir() raises an exception."""
    monkeypatch.setattr(sync_module, "REF", "abc123")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    def mock_mkdir(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", mock_mkdir)

    result = isolated_main()
    assert result == 0


def test_main_returns_zero_on_copytree_failure_with_partial_cleanup(isolated_main, tmp_path, monkeypatch):
    """main() must return 0 when copytree fails, and must not leave partial LAST_GOOD tree."""
    monkeypatch.setattr(sync_module, "REF", "abc123")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    # Mock clone to succeed and create a staging directory
    def mock_clone(dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "file.txt").write_text("staged content")
        return "abc123"

    monkeypatch.setattr(sync_module, "clone", mock_clone)

    # Mock copytree to fail when saving to LAST_GOOD
    original_copytree = shutil.copytree
    def mock_copytree(src, dst, **kwargs):
        if "last-good" in str(dst):
            raise OSError("Disk full")
        return original_copytree(src, dst, **kwargs)

    monkeypatch.setattr("shutil.copytree", mock_copytree)

    # Mock subprocess.run to prevent real process spawning in sync_skills
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))

    result = isolated_main()

    # main() must return 0 despite failure
    assert result == 0

    # Verify no partial LAST_GOOD tree is left behind
    last_good = sync_module.LAST_GOOD
    assert not last_good.exists(), "Partial LAST_GOOD tree was not cleaned up"


def test_main_with_successful_clone_creates_applied_record(isolated_main, tmp_path, monkeypatch):
    """main() successfully applies config and records applied state."""
    monkeypatch.setattr(sync_module, "REF", "abc123")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    # Mock clone to create a minimal staged tree
    def mock_clone(dest):
        dest.mkdir(parents=True, exist_ok=True)
        hermes_root = dest / "hermes" / "root"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "SOUL.md").write_text("# SOUL")
        (hermes_root / "config.yaml").write_text("config: value")
        return "abc123"

    monkeypatch.setattr(sync_module, "clone", mock_clone)

    # Mock subprocess calls that sync_skills makes
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))

    result = isolated_main()

    assert result == 0

    # Verify applied record was written
    applied = sync_module.APPLIED
    assert applied.exists()
    rec = json.loads(applied.read_text())
    assert rec["ref"] == "abc123"
    assert rec["image"] == "img:v1"
    assert rec["result"] == "ok"
    assert rec["sync_version"] == sync_module.SYNC_VERSION


def test_fallback_restores_last_good_into_an_already_existing_staging(
    isolated_main, tmp_path, monkeypatch
):
    """The offline path must actually work with STAGING already present.

    This is the regression guard for the dead fallback: /staging is an emptyDir
    mount point, so it always exists and rmtree cannot remove it. Without
    dirs_exist_ok=True the copytree raised FileExistsError on every real boot and
    nothing was ever restored.
    """
    monkeypatch.setattr(sync_module, "REF", "newsha")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    # A previous successful sync left a last-good tree and an applied record.
    declared_dir = sync_module.LAST_GOOD / "hermes" / "root" / "cron"
    declared_dir.mkdir(parents=True)
    (sync_module.LAST_GOOD / "hermes" / "root" / "SOUL.md").write_text("# LAST GOOD SOUL")
    (declared_dir / "jobs.json").write_text(json.dumps(
        {"jobs": [{"id": "j1", "name": "daily report", "managed_by": "plder"}]}))
    sync_module.APPLIED.write_text(json.dumps(
        {"ref": "oldsha", "image": "img:v0", "result": "ok"}))

    # STAGING exists already, as the emptyDir mount point always does, and
    # carries junk from an earlier boot.
    staging = sync_module.STAGING
    assert staging.is_dir(), "fixture must model the mount point"
    (staging / "leftover.txt").write_text("from a previous boot")

    # The clone fails: offline, or PLDER_DEPLOY_KEY_READ absent.
    monkeypatch.setattr(sync_module, "clone", lambda dest: None)
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))

    assert isolated_main() == 0

    # The staged tree actually landed in STAGING...
    assert (staging / "hermes" / "root" / "SOUL.md").read_text() == "# LAST GOOD SOUL"
    # ...and was applied onto the home.
    assert (tmp_path / "SOUL.md").read_text() == "# LAST GOOD SOUL"
    jobs = json.loads((tmp_path / "cron" / "jobs.json").read_text())
    assert [j["id"] for j in jobs["jobs"]] == ["j1"]

    # The record names the ref that actually landed (the previous one), so the
    # next boot does not skip and retries the new ref.
    rec = json.loads(sync_module.APPLIED.read_text())
    assert rec["ref"] == "oldsha"
    assert should_skip(sync_module.APPLIED, "newsha", "img:v1") is False


def test_fallback_survives_the_staging_mount_points_copystat_hazard(
    isolated_main, tmp_path, monkeypatch
):
    """Regression guard for the live 2026-09-16 failure: copystat on /staging itself.

    shutil.copytree finishes with copystat(src, dst) on the DESTINATION ROOT.
    /staging is a root-owned emptyDir mount point and the container runs as
    uid 10000, so a copystat call against /staging itself raises
    PermissionError even though every file underneath copies fine. The
    dirs_exist_ok fix (see test_fallback_restores_last_good_into_an_already_
    existing_staging) got the copytree call past FileExistsError, but it then
    died here in production: `[Errno 1] Operation not permitted: '/staging'`,
    and the whole offline-resilience path was still dead. The fix must copy
    LAST_GOOD's children into STAGING instead of copytree-ing onto STAGING
    itself, so copystat is never called with STAGING as the destination.
    """
    monkeypatch.setattr(sync_module, "REF", "newsha")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    declared_dir = sync_module.LAST_GOOD / "hermes" / "root" / "cron"
    declared_dir.mkdir(parents=True)
    (sync_module.LAST_GOOD / "hermes" / "root" / "SOUL.md").write_text("# LAST GOOD SOUL")
    # A valid config.yaml too, so copy_root_config does not itself vote
    # "partial" for reasons unrelated to what this test is regression-guarding.
    (sync_module.LAST_GOOD / "hermes" / "root" / "config.yaml").write_text(
        "model:\n  default: test\n")
    (declared_dir / "jobs.json").write_text(json.dumps(
        {"jobs": [{"id": "j1", "name": "daily report", "managed_by": "plder"}]}))
    sync_module.APPLIED.write_text(json.dumps(
        {"ref": "oldsha", "image": "img:v0", "result": "ok"}))

    staging = sync_module.STAGING
    assert staging.is_dir(), "fixture must model the mount point"

    # The clone fails: offline, or PLDER_DEPLOY_KEY_READ absent.
    monkeypatch.setattr(sync_module, "clone", lambda dest: None)
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))

    real_copystat = shutil.copystat

    def mount_point_aware_copystat(src, dst, *args, **kwargs):
        # Only the mount point ROOT is root-owned and un-chown-able by uid
        # 10000; freshly-created children underneath it are not.
        if Path(dst) == staging:
            raise PermissionError(1, "Operation not permitted", str(staging))
        return real_copystat(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copystat", mount_point_aware_copystat)

    log_lines = []
    monkeypatch.setattr(sync_module, "log", lambda msg: log_lines.append(msg))

    assert isolated_main() == 0

    assert not any("failed to restore" in line for line in log_lines)

    # The staged tree actually landed in STAGING...
    assert (staging / "hermes" / "root" / "SOUL.md").read_text() == "# LAST GOOD SOUL"
    # ...and was applied onto the home.
    assert (tmp_path / "SOUL.md").read_text() == "# LAST GOOD SOUL"
    jobs = json.loads((tmp_path / "cron" / "jobs.json").read_text())
    assert [j["id"] for j in jobs["jobs"]] == ["j1"]

    rec = json.loads(sync_module.APPLIED.read_text())
    assert rec["ref"] == "oldsha"
    assert rec["result"] == "ok"


def test_skills_sync_failure_does_not_flip_the_result_to_partial(
    isolated_main, tmp_path, monkeypatch
):
    """sync_skills is log-only: stage2-hook.sh already runs it with `|| warn`.

    If it voted, one transient failure would write result: "partial", and since
    should_skip requires result == "ok" every later restart would re-clone from
    GitHub -- making the agent's boot depend on network reachability.
    """
    monkeypatch.setattr(sync_module, "REF", "abc123")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    def mock_clone(dest):
        (dest / "hermes" / "root").mkdir(parents=True, exist_ok=True)
        (dest / "hermes" / "root" / "SOUL.md").write_text("# SOUL")
        (dest / "hermes" / "root" / "config.yaml").write_bytes(b"model:\n  default: test\n")
        return "abc123"

    monkeypatch.setattr(sync_module, "clone", mock_clone)
    monkeypatch.setattr(sync_module, "sync_skills", lambda home: False)

    assert isolated_main() == 0
    assert json.loads(sync_module.APPLIED.read_text())["result"] == "ok"


def test_apply_cron_skips_a_declared_job_with_no_id(tmp_path):
    """A declaration missing `id` used to raise KeyError and abort the whole apply."""
    declared = tmp_path / "declared.json"
    declared.write_text(json.dumps({"jobs": [
        {"name": "typo, no id"},
        {"id": "good", "name": "fine"},
    ]}))

    assert sync_module.apply_cron(tmp_path, declared) is True

    jobs = json.loads((tmp_path / "cron" / "jobs.json").read_text())["jobs"]
    assert [j["id"] for j in jobs] == ["good"]


def test_apply_cron_skips_duplicate_declared_ids(tmp_path):
    """Duplicate declared ids used to produce two entries for the same job."""
    declared = tmp_path / "declared.json"
    declared.write_text(json.dumps({"jobs": [
        {"id": "dup", "name": "first wins"},
        {"id": "dup", "name": "second is dropped"},
    ]}))

    assert sync_module.apply_cron(tmp_path, declared) is True

    jobs = json.loads((tmp_path / "cron" / "jobs.json").read_text())["jobs"]
    assert [j["id"] for j in jobs] == ["dup"]
    assert jobs[0]["name"] == "first wins"


# ---- install_profiles -------------------------------------------------------

def _profile(staged, name, jobs=None, manifest=True):
    d = staged / "hermes" / "profiles" / name
    d.mkdir(parents=True)
    if manifest:
        (d / "distribution.yaml").write_text(f"name: {name}\nversion: 0.1.0\n")
    if jobs is not None:
        (d / "cron").mkdir()
        (d / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    return d


def _fake_run(calls, fail_for=(), exc=None):
    def run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        if cmd[1:3] == ["profile", "install"] and cmd[5] in fail_for:
            raise exc or sync_module.subprocess.CalledProcessError(1, cmd, stderr=b"boom")
        return MagicMock(returncode=0)
    return run


def _installs(calls):
    return [c for c in calls if c["cmd"][1:3] == ["profile", "install"]]


def test_install_profiles_installs_each_declared_profile(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    _profile(staged, "shopper")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    names, ok = sync_module.install_profiles(staged)
    assert names == ["monitor", "shopper"]
    assert ok is True
    cmds = [c["cmd"] for c in _installs(calls)]
    assert [c[5] for c in cmds] == ["monitor", "shopper"]
    assert all(c[0] == sync_module.HERMES_BIN and "--force" in c and "-y" in c for c in cmds)
    assert all(c["env"]["HERMES_HOME"] == str(sync_module.HERMES_HOME) for c in _installs(calls))


def test_install_profiles_ignores_directories_without_a_manifest(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    _profile(staged, "notes", manifest=False)
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    names, ok = sync_module.install_profiles(staged)
    assert names == ["monitor"] and ok is True
    assert [c["cmd"][5] for c in _installs(calls)] == ["monitor"]


def test_install_profiles_continues_after_a_failed_install(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    _profile(staged, "shopper")
    # monitor declares a job, so the assertion below is not vacuous.
    (staged / "hermes" / "profiles" / "monitor" / "cron").mkdir()
    (staged / "hermes" / "profiles" / "monitor" / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "m1", "name": "monitor job"}]}))
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls, fail_for=("monitor",)))
    names, ok = sync_module.install_profiles(staged)
    assert names == ["shopper"]
    assert ok is False
    # The failed profile has no directory under HERMES_HOME, so no cron store
    # may be conjured for it.
    assert not (sync_module.HERMES_HOME / "profiles" / "monitor" / "cron" / "jobs.json").exists()


def test_install_profiles_survives_a_missing_binary(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run",
                        _fake_run(calls, fail_for=("monitor",), exc=FileNotFoundError("hermes")))
    names, ok = sync_module.install_profiles(staged)
    assert names == [] and ok is False


def test_install_profiles_survives_a_failure_with_no_stderr(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    calls = []
    err = sync_module.subprocess.CalledProcessError(1, ["hermes"], stderr=None)
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls, fail_for=("monitor",), exc=err))
    names, ok = sync_module.install_profiles(staged)
    assert names == [] and ok is False


def test_install_profiles_rejects_an_invalid_profile_name(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "Bad_Name")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    names, ok = sync_module.install_profiles(staged)
    assert names == [] and ok is False
    assert _installs(calls) == []


def test_install_profiles_applies_the_profiles_declared_cron(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor", jobs=[{"id": "abc123def456", "name": "[bot:monitor] daily"}])
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run([]))
    names, ok = sync_module.install_profiles(staged)
    live = json.loads((sync_module.HERMES_HOME / "profiles" / "monitor" / "cron" / "jobs.json").read_text())
    assert ok is True
    assert [j["id"] for j in live["jobs"]] == ["abc123def456"]
    assert live["jobs"][0]["managed_by"] == "plder"


def test_install_profiles_with_no_profiles_directory_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    assert sync_module.install_profiles(sync_module.STAGING) == ([], True)
    assert calls == []


def test_main_moves_a_job_from_root_to_a_profile_and_records_it(monkeypatch):
    """The root declaration drops the job and the profile declares it: after one
    run the root store has retired it and the profile store holds it."""
    job = {"id": "6270d3f018f2", "name": "[bot:monitor] daily exception report"}
    home = sync_module.HERMES_HOME
    (home / "cron").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{**job, "managed_by": "plder"}]}))

    def fake_clone(dest):
        (dest / "hermes" / "root" / "cron").mkdir(parents=True)
        (dest / "hermes" / "root" / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
        (dest / "hermes" / "root" / "config.yaml").write_bytes(b"model:\n  default: test\n")
        _profile(dest, "monitor", jobs=[job])
        return "abc1234"

    monkeypatch.setattr(sync_module, "REF", "abc1234")
    monkeypatch.setattr(sync_module, "clone", fake_clone)
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run([]))
    assert main() == 0

    root_live = json.loads((home / "cron" / "jobs.json").read_text())
    prof_live = json.loads((home / "profiles" / "monitor" / "cron" / "jobs.json").read_text())
    applied = json.loads(sync_module.APPLIED.read_text())
    assert root_live["jobs"] == []
    assert [j["id"] for j in prof_live["jobs"]] == ["6270d3f018f2"]
    assert applied["profiles"] == ["monitor"]
    assert applied["result"] == "ok"


# ---- fix wave: failed install, orphaned profiles, unserved profiles ---------

def _stage_tree(dest, root_jobs=(), profiles=(), allowlist=None):
    """Write a staged plder tree: root cron, optional root config, profiles.

    ``profiles`` is a sequence of (name, jobs) pairs.
    """
    (dest / "hermes" / "root" / "cron").mkdir(parents=True)
    (dest / "hermes" / "root" / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": list(root_jobs)}))
    if allowlist is not None:
        lines = ["gateway:", "  multiplex_profiles: true", "  multiplex_profile_allowlist:"]
        lines += [f"    - {name}" for name in allowlist]
        (dest / "hermes" / "root" / "config.yaml").write_text("\n".join(lines) + "\n")
    else:
        (dest / "hermes" / "root" / "config.yaml").write_bytes(b"model:\n  default: test\n")
    for name, jobs in profiles:
        _profile(dest, name, jobs=jobs)


def _live_store(home, name, doc):
    d = home / "profiles" / name / "cron"
    d.mkdir(parents=True)
    f = d / "jobs.json"
    f.write_text(json.dumps(doc, indent=2))
    return f


def _run_main(monkeypatch, fake_clone, fail_for=()):
    monkeypatch.setattr(sync_module, "REF", "abc1234")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")
    monkeypatch.setattr(sync_module, "clone", fake_clone)
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run([], fail_for=fail_for))
    assert main() == 0
    return json.loads(sync_module.APPLIED.read_text())


def test_main_failed_install_of_an_existing_profile_still_receives_a_moved_job(monkeypatch):
    """B: root retires the job first; if the profile install then fails, the
    profile's cron must still be applied or the job exists in neither store."""
    job = {"id": "6270d3f018f2", "name": "[bot:monitor] daily exception report"}
    home = sync_module.HERMES_HOME
    (home / "cron").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{**job, "managed_by": "plder"}]}))
    (home / "profiles" / "monitor").mkdir(parents=True)  # installed on an earlier boot

    def fake_clone(dest):
        _stage_tree(dest, root_jobs=[], profiles=[("monitor", [job])])
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone, fail_for=("monitor",))

    root_live = json.loads((home / "cron" / "jobs.json").read_text())
    prof_file = home / "profiles" / "monitor" / "cron" / "jobs.json"
    assert root_live["jobs"] == []
    assert prof_file.is_file(), "job was retired from root and never reached the profile store"
    assert [j["id"] for j in json.loads(prof_file.read_text())["jobs"]] == ["6270d3f018f2"]
    assert applied["result"] == "partial"
    assert applied["profiles"] == []


def test_main_retires_managed_jobs_of_a_profile_removed_from_plder(monkeypatch):
    """C: a live profile that is no longer declared loses its plder jobs only."""
    home = sync_module.HERMES_HOME
    _live_store(home, "oldbot", {"jobs": [
        {"id": "managed1", "name": "declared once", "managed_by": "plder", "enabled": True},
        {"id": "hand1", "name": "made from telegram", "enabled": True},
    ], "updated_at": "2026-09-13T10:00:00+00:00"})

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])])
        return "abc1234"

    _run_main(monkeypatch, fake_clone)

    live = json.loads((home / "profiles" / "oldbot" / "cron" / "jobs.json").read_text())
    assert [j["id"] for j in live["jobs"]] == ["hand1"]
    assert live["jobs"][0] == {"id": "hand1", "name": "made from telegram", "enabled": True}
    assert live["updated_at"] == "2026-09-13T10:00:00+00:00"


def test_main_leaves_an_undeclared_profile_with_only_hand_made_jobs_byte_identical(monkeypatch):
    """C: a Desktop-created bot with no plder jobs is never rewritten."""
    home = sync_module.HERMES_HOME
    f = _live_store(home, "desktopbot", {"jobs": [
        {"id": "hand1", "name": "made in desktop", "enabled": True},
    ], "updated_at": "2026-09-13T10:00:00+00:00"})
    before_bytes = f.read_bytes()
    before_mtime = f.stat().st_mtime_ns

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])])
        return "abc1234"

    _run_main(monkeypatch, fake_clone)

    assert f.read_bytes() == before_bytes
    assert f.stat().st_mtime_ns == before_mtime


def test_main_does_not_retire_the_jobs_of_a_declared_profile(monkeypatch):
    """C: the retire pass must skip declared profiles, or it would undo their apply."""
    job = {"id": "abc123def456", "name": "[bot:monitor] daily"}
    home = sync_module.HERMES_HOME
    _live_store(home, "monitor", {"jobs": [{**job, "managed_by": "plder"}]})
    calls = []
    real = getattr(sync_module, "retire_undeclared_profile_jobs", None)
    if real is not None:
        def spy(declared):
            calls.append(set(declared))
            return real(declared)
        monkeypatch.setattr(sync_module, "retire_undeclared_profile_jobs", spy)

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [job])])
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone)

    live = json.loads((home / "profiles" / "monitor" / "cron" / "jobs.json").read_text())
    assert [j["id"] for j in live["jobs"]] == ["abc123def456"]
    assert applied["result"] == "ok"
    assert calls == [{"monitor"}], "retire pass must run exactly once, with monitor declared"


def test_main_retires_managed_jobs_of_a_declared_profile_that_lost_its_cron_declaration(monkeypatch):
    """C: a profile still declared but with no cron/jobs.json declares no jobs."""
    home = sync_module.HERMES_HOME
    _live_store(home, "monitor", {"jobs": [
        {"id": "managed1", "managed_by": "plder"},
        {"id": "hand1", "name": "made from telegram"},
    ]})

    def fake_clone(dest):
        _stage_tree(dest)
        _profile(dest, "monitor", jobs=None)  # manifest, but no cron/jobs.json
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone)

    live = json.loads((home / "profiles" / "monitor" / "cron" / "jobs.json").read_text())
    assert [j["id"] for j in live["jobs"]] == ["hand1"]
    assert applied["profiles"] == ["monitor"]
    assert applied["result"] == "ok"


def test_main_leaves_a_corrupt_store_of_an_undeclared_profile_alone(monkeypatch):
    """C: an unreadable store is nothing to do -- not rewritten, not a failure."""
    home = sync_module.HERMES_HOME
    d = home / "profiles" / "desktopbot" / "cron"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text("{not json")

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])])
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone)

    assert (d / "jobs.json").read_text() == "{not json"
    assert applied["result"] == "ok"


def test_main_warns_about_an_unlisted_profile_with_an_enabled_job(monkeypatch, capsys):
    """F: under multiplex an unlisted profile's jobs never run; say so."""
    home = sync_module.HERMES_HOME
    _live_store(home, "desktopbot", {"jobs": [{"id": "hand1", "enabled": True}]})

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])], allowlist=["monitor"])
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone)

    out = capsys.readouterr().out
    warnings = [l for l in out.splitlines() if "WARNING" in l and "desktopbot" in l]
    assert len(warnings) == 1, out
    assert "multiplex_profile_allowlist" in warnings[0]
    assert "will not run" in warnings[0]
    assert applied["result"] == "ok"


def test_main_does_not_warn_about_an_allowlisted_profile(monkeypatch, capsys):
    """F: an allowlisted profile with enabled jobs is served; no warning."""
    job = {"id": "abc123def456", "name": "[bot:monitor] daily", "enabled": True}

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [job])], allowlist=["monitor"])
        return "abc1234"

    _run_main(monkeypatch, fake_clone)
    out = capsys.readouterr().out
    assert "multiplex_profile_allowlist" not in out, out


def test_main_does_not_warn_when_the_staged_root_config_is_missing(monkeypatch, capsys):
    """F: no declared config means nothing to compare against; stay silent.

    A missing root config is also a broken declaration: the live config.yaml
    is kept and the run is recorded as partial (see copy_root_config)."""
    home = sync_module.HERMES_HOME
    _live_store(home, "desktopbot", {"jobs": [{"id": "hand1", "enabled": True}]})

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])], allowlist=None)
        (dest / "hermes" / "root" / "config.yaml").unlink()
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone)
    out = capsys.readouterr().out
    assert "multiplex_profile_allowlist" not in out, out
    assert applied["result"] == "partial"


def test_main_profile_skills_sync_failure_does_not_flip_the_result_to_partial(monkeypatch):
    """G: per-profile skills sync is log-only, like the root call."""
    home = sync_module.HERMES_HOME

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])])
        return "abc1234"

    # Root succeeds, every profile home fails.
    monkeypatch.setattr(sync_module, "sync_skills", lambda h: Path(h) == Path(home))
    applied = _run_main(monkeypatch, fake_clone)
    assert applied["profiles"] == ["monitor"]
    assert applied["result"] == "ok"


# ---- root config.yaml: plder is the single source ----------------------------

GOOD_CONFIG = b"model:\n  default: deepseek/deepseek-v4-flash-0731\n_config_version: 38\n"
LIVE_CONFIG = b"model:\n  default: live\n"


def _clone_with_root(config_bytes):
    """A staged tree whose root declares config_bytes (None = no config.yaml)."""
    def fake_clone(dest):
        root = dest / "hermes" / "root"
        (root / "cron").mkdir(parents=True, exist_ok=True)
        (root / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
        (root / "SOUL.md").write_text("# SOUL")
        if config_bytes is not None:
            (root / "config.yaml").write_bytes(config_bytes)
        return "abc1234"
    return fake_clone


def test_root_config_is_installed_byte_identical(monkeypatch, capsys):
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert (sync_module.HERMES_HOME / "config.yaml").read_bytes() == GOOD_CONFIG
    assert applied["result"] == "ok"
    assert applied["sync_version"] == sync_module.SYNC_VERSION
    assert "copied config.yaml" in capsys.readouterr().out


def test_root_config_replaces_a_drifted_live_file_and_keeps_the_previous_one(monkeypatch):
    """Git wins; what it replaced (runtime edits, the kubelet stub) is kept for inspection."""
    home = sync_module.HERMES_HOME
    (home / "config.yaml").write_bytes(LIVE_CONFIG)
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert (home / "config.yaml").read_bytes() == GOOD_CONFIG
    assert (sync_module.STATE_DIR / "config.yaml.previous").read_bytes() == LIVE_CONFIG
    assert applied["result"] == "ok"


def test_identical_live_root_config_is_not_rewritten(monkeypatch, capsys):
    home = sync_module.HERMES_HOME
    live = home / "config.yaml"
    live.write_bytes(GOOD_CONFIG)
    os.utime(live, ns=(1_000_000_000, 1_000_000_000))
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert live.stat().st_mtime_ns == 1_000_000_000
    assert not (sync_module.STATE_DIR / "config.yaml.previous").exists()
    assert applied["result"] == "ok"
    assert "config.yaml already matches the declaration" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    None,
    b"model: [unclosed\n",
    b"",
    b"- just\n- a list\n",
    b"\xff\xfe not utf-8\n",
], ids=["missing", "unparseable", "empty", "not-a-mapping", "not-utf8"])
def test_a_bad_declared_root_config_keeps_the_live_file(monkeypatch, capsys, bad):
    """Config must never take the gateway down: refuse, keep what boots today."""
    home = sync_module.HERMES_HOME
    live = home / "config.yaml"
    live.write_bytes(LIVE_CONFIG)
    applied = _run_main(monkeypatch, _clone_with_root(bad))
    assert live.read_bytes() == LIVE_CONFIG
    assert applied["result"] == "partial"
    assert "keeping the live config.yaml" in capsys.readouterr().out
    assert not list(home.glob(".config.yaml*")), "temp file left behind"


def test_a_bad_declaration_on_a_fresh_home_writes_no_config(monkeypatch):
    """No live file and a broken declaration: write nothing, so stage2 seeds its example."""
    applied = _run_main(monkeypatch, _clone_with_root(b"model: [unclosed\n"))
    assert not (sync_module.HERMES_HOME / "config.yaml").exists()
    assert applied["result"] == "partial"


def test_a_failed_replace_keeps_the_live_file_and_removes_the_temp(monkeypatch):
    home = sync_module.HERMES_HOME
    live = home / "config.yaml"
    live.write_bytes(LIVE_CONFIG)
    real_replace = os.replace

    def refuse_config(src, dst, *args, **kwargs):
        if Path(dst).name == "config.yaml":
            raise PermissionError("simulated: cannot rename over config.yaml")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_config)
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert live.read_bytes() == LIVE_CONFIG
    assert not list(home.glob(".config.yaml*"))
    assert applied["result"] == "partial"


def test_offline_boot_installs_the_last_good_root_config(monkeypatch):
    home = sync_module.HERMES_HOME
    root = sync_module.LAST_GOOD / "hermes" / "root"
    root.mkdir(parents=True)
    (root / "config.yaml").write_bytes(GOOD_CONFIG)
    sync_module.APPLIED.write_text(json.dumps({"ref": "oldsha", "image": "img:v1", "result": "ok"}))
    applied = _run_main(monkeypatch, lambda dest: None)
    assert (home / "config.yaml").read_bytes() == GOOD_CONFIG
    assert applied["ref"] == "oldsha"
