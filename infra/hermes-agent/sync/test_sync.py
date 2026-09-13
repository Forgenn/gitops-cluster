import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from sync import should_skip, main
import shutil
import pytest
import sync as sync_module


def _write(tmp_path: Path, ref: str, image: str, result: str = "ok") -> Path:
    p = tmp_path / "applied"
    p.write_text(json.dumps({"ref": ref, "image": image, "result": result}))
    return p


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    """Fixture that isolates main() to use tmp_path instead of live system paths.

    Monkeypatches all module-level path globals so tests are hermetic and cannot
    accidentally modify the operator's live Hermes installation.
    """
    state_dir = tmp_path / ".agent-config"
    monkeypatch.setattr(sync_module, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(sync_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(sync_module, "APPLIED", state_dir / "applied")
    monkeypatch.setattr(sync_module, "LAST_GOOD", state_dir / "last-good")
    monkeypatch.setattr(sync_module, "STAGING", tmp_path / "staging")
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
