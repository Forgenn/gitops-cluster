"""Tests for git_credential_github_app.py. Run: python -m pytest -q test_git_credential_github_app.py"""
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).with_name("git_credential_github_app.py")


def run(stdin: str, token_dir: Path, action: str = "get") -> str:
    return subprocess.run([sys.executable, str(HELPER), action], input=stdin, text=True,
                          capture_output=True, env={"TOKEN_DIR": str(token_dir)}).stdout


def setup(tmp_path, repos="forgenn/plder\nforgenn/gitops-cluster\n"):
    (tmp_path / "token").write_text("ghs_x")
    (tmp_path / "repos").write_text(repos)
    return tmp_path


def test_answers_for_an_installed_repo(tmp_path):
    out = run("protocol=https\nhost=github.com\npath=Forgenn/plder.git\n\n", setup(tmp_path))
    assert "username=x-access-token\n" in out and "password=ghs_x\n" in out


def test_repo_match_ignores_case_and_dot_git(tmp_path):
    out = run("protocol=https\nhost=github.com\npath=forgenn/GitOps-Cluster\n\n", setup(tmp_path))
    assert "password=ghs_x" in out


def test_falls_through_for_a_repo_the_app_is_not_installed_on(tmp_path):
    # The next helper (the developer PAT) must get its turn: answer nothing.
    assert run("protocol=https\nhost=github.com\npath=Forgenn/career-ops.git\n\n", setup(tmp_path)) == ""


def test_answers_without_a_path(tmp_path):
    assert "password=ghs_x" in run("protocol=https\nhost=github.com\n\n", setup(tmp_path))


def test_answers_when_the_repo_list_is_missing(tmp_path):
    (tmp_path / "token").write_text("ghs_x")
    assert "password=ghs_x" in run("protocol=https\nhost=github.com\npath=Forgenn/x.git\n\n", tmp_path)


def test_ignores_other_hosts(tmp_path):
    assert run("protocol=https\nhost=gitlab.com\npath=Forgenn/plder.git\n\n", setup(tmp_path)) == ""


def test_store_and_erase_are_noops(tmp_path):
    setup(tmp_path)
    assert run("protocol=https\nhost=github.com\n\n", tmp_path, "store") == ""
    assert run("protocol=https\nhost=github.com\n\n", tmp_path, "erase") == ""


def test_missing_token_answers_nothing(tmp_path):
    assert run("protocol=https\nhost=github.com\npath=Forgenn/plder.git\n\n", tmp_path) == ""
