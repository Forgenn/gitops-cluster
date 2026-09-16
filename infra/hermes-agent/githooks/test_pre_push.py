"""Real-git tests: bare remotes and clones under tmp_path, hook wired via a repo-local core.hooksPath."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import pre_push  # noqa: E402

HOOK = Path(__file__).with_name("pre_push.py")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


@pytest.fixture
def make_repo(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    wrapper = hooks / "pre-push"
    wrapper.write_bytes(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{HOOK.as_posix()}" "$@"\n'.encode())
    wrapper.chmod(0o755)

    def make(remote_name="gitops-cluster.git", seed=True):
        remote = tmp_path / remote_name
        git(tmp_path, "init", "-q", "--bare", str(remote))
        work = tmp_path / f"work-{remote_name}"
        git(tmp_path, "clone", "-q", str(remote), str(work))
        git(work, "symbolic-ref", "HEAD", "refs/heads/main")
        for key, value in (("user.name", "t"), ("user.email", "t@example.com"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", hooks.as_posix())):
            git(work, "config", key, value)
        if seed:
            commit(work, "README.md")
            git(work, "push", "-q", "origin", "main")
        return work
    return make


def commit(work, path):
    f = work / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("change\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", f"touch {path}")


def test_push_to_main_touching_hermes_agent_is_refused(make_repo):
    work = make_repo()
    commit(work, "infra/hermes-agent/deployment.yaml")
    res = git(work, "push", "origin", "main", check=False)
    assert res.returncode != 0
    assert "ESCALATE" in res.stderr and "infra/hermes-agent/deployment.yaml" in res.stderr
    remote_main = git(work, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
    assert remote_main != git(work, "rev-parse", "HEAD").stdout.strip()


def test_push_to_main_elsewhere_is_allowed(make_repo):
    work = make_repo()
    commit(work, "infra/loomie/deployment.yaml")
    assert git(work, "push", "origin", "main", check=False).returncode == 0


def test_push_to_a_hermes_branch_is_allowed(make_repo):
    work = make_repo()
    commit(work, "infra/hermes-agent/deployment.yaml")
    assert git(work, "push", "origin", "main:refs/heads/hermes/raise-limit", check=False).returncode == 0


def test_other_repositories_are_not_guarded(make_repo):
    work = make_repo("nixos-config.git")
    commit(work, "infra/hermes-agent/x.yaml")
    assert git(work, "push", "origin", "main", check=False).returncode == 0


def test_first_push_of_main_is_inspected_too(make_repo):
    work = make_repo(seed=False)
    commit(work, "infra/hermes-agent/deployment.yaml")
    res = git(work, "push", "origin", "main", check=False)
    assert res.returncode != 0 and "ESCALATE" in res.stderr


def test_branch_deletion_is_ignored():
    zero = "0" * 40
    stdin = io.StringIO(f"(delete) {zero} refs/heads/main {'a' * 40}\n")
    assert pre_push.main(["pre-push", "origin", "git@github.com:Forgenn/gitops-cluster.git"], stdin) == 0
