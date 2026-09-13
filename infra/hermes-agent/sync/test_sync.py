import json
from pathlib import Path
from unittest.mock import patch
from sync import should_skip, main
import shutil


def _write(tmp_path: Path, ref: str, image: str, result: str = "ok") -> Path:
    p = tmp_path / "applied"
    p.write_text(json.dumps({"ref": ref, "image": image, "result": result}))
    return p


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


def test_main_returns_zero_on_mkdir_failure(tmp_path, monkeypatch):
    """main() must return 0 even when STATE_DIR.mkdir() raises an exception."""
    def mock_mkdir(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", mock_mkdir)
    # Even though mkdir fails, main() should return 0, not raise
    result = main()
    assert result == 0


def test_main_returns_zero_on_copytree_failure(tmp_path, monkeypatch):
    """main() must return 0 even when shutil.copytree() raises an exception."""
    # This test verifies the copytree error handling in the fallback path
    call_count = [0]
    original_copytree = shutil.copytree

    def mock_copytree(src, dst, **kwargs):
        call_count[0] += 1
        # Only fail on the last-good restore attempt
        if "last-good" in str(dst):
            raise OSError("Disk full")
        return original_copytree(src, dst, **kwargs)

    monkeypatch.setattr("shutil.copytree", mock_copytree)
    # main() should return 0 despite copytree failure
    result = main()
    assert result == 0
