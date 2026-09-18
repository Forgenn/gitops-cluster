"""git-credential 'get' protocol tests. No real PAT ever appears here or on disk;
DUMMY_TOKEN is a test-local placeholder, never read from a live secret."""
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).with_name("git_credential_developer_pat.py")
DUMMY_TOKEN = "test-only-placeholder-not-a-real-credential"


def run(stdin: str, env_token: str | None) -> subprocess.CompletedProcess:
    env = {"PATH": ""}
    if env_token is not None:
        env["DEVELOPER_GITHUB_PAT"] = env_token
    return subprocess.run([sys.executable, str(HELPER), "get"], input=stdin,
                           capture_output=True, text=True, env=env)


def test_emits_credentials_for_github_https_when_the_token_is_set():
    res = run("protocol=https\nhost=github.com\n\n", DUMMY_TOKEN)
    assert res.returncode == 0
    assert res.stdout == f"username=x-access-token\npassword={DUMMY_TOKEN}\n"


def test_emits_nothing_when_the_token_is_absent():
    res = run("protocol=https\nhost=github.com\n\n", None)
    assert res.returncode == 0 and res.stdout == ""


def test_emits_nothing_when_the_token_is_empty():
    res = run("protocol=https\nhost=github.com\n\n", "")
    assert res.returncode == 0 and res.stdout == ""


def test_emits_nothing_for_a_different_host():
    res = run("protocol=https\nhost=gitlab.com\n\n", DUMMY_TOKEN)
    assert res.returncode == 0 and res.stdout == ""


def test_emits_nothing_for_ssh():
    res = run("protocol=ssh\nhost=github.com\n\n", DUMMY_TOKEN)
    assert res.returncode == 0 and res.stdout == ""


def test_ignores_operations_other_than_get():
    res = subprocess.run([sys.executable, str(HELPER), "store"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, env={"PATH": "", "DEVELOPER_GITHUB_PAT": DUMMY_TOKEN})
    assert res.returncode == 0 and res.stdout == ""


def test_never_echoes_the_token_on_stderr_either():
    res = run("protocol=https\nhost=github.com\n\n", DUMMY_TOKEN)
    assert DUMMY_TOKEN not in res.stderr
