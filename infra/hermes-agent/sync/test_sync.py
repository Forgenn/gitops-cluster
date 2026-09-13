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
from pathlib import Path
from unittest.mock import MagicMock
from sync import should_skip, main
import shutil
import pytest
import sync as sync_module


def _write(tmp_path: Path, ref: str, image: str, result: str = "ok") -> Path:
    p = tmp_path / "applied"
    p.write_text(json.dumps({"ref": ref, "image": image, "result": result}))
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


def test_config_yaml_is_not_copied_onto_the_home(isolated_main, tmp_path, monkeypatch):
    """config.yaml stays ConfigMap-owned in this phase; SOUL.md is the only copy.

    Writing config.yaml here is at best a no-op (the main container bind-mounts
    the ConfigMap read-only over /opt/data/config.yaml via subPath, masking it)
    and at worst fatal to the skip gate: the copy target is the root-owned
    kubelet subPath stub, so copy2 as uid 10000 raises PermissionError,
    copy_root_files returns False and result becomes "partial" forever.
    """
    monkeypatch.setattr(sync_module, "REF", "abc123")
    monkeypatch.setattr(sync_module, "IMAGE", "img:v1")

    def mock_clone(dest):
        hermes_root = dest / "hermes" / "root"
        hermes_root.mkdir(parents=True, exist_ok=True)
        (hermes_root / "SOUL.md").write_text("# SOUL")
        (hermes_root / "config.yaml").write_text("config: value")
        return "abc123"

    monkeypatch.setattr(sync_module, "clone", mock_clone)
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))

    assert isolated_main() == 0

    assert (tmp_path / "SOUL.md").read_text() == "# SOUL"
    assert not (tmp_path / "config.yaml").exists(), "config.yaml must not be copied in this phase"
    assert json.loads(sync_module.APPLIED.read_text())["result"] == "ok"


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
