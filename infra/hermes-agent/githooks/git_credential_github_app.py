#!/opt/hermes/.venv/bin/python3
"""git credential helper: answer github.com HTTPS requests with the GitHub App token.

The token is written by the github-app-token sidecar (forgenn-hermes: a 1-hour
installation token with no Workflows or Administration permission). It answers only
for the repos the App is installed on (/run/github-token/repos; needs
credential.useHttpPath), so git falls through to the next helper, the developer
PAT, for any other repo. Never prints the token anywhere but git's stdin.
docs/plans/2026-09-29-phase2-github-identity.md Task 3.
"""
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "get":
        return 0
    fields = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line)
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return 0
    token_dir = Path(os.environ.get("TOKEN_DIR", "/run/github-token"))
    path = fields.get("path", "").strip("/").lower().removesuffix(".git")
    if path:
        try:
            installed = set((token_dir / "repos").read_text().split())
        except OSError:
            installed = None  # no list yet: answer, as before the list existed
        if installed is not None and path not in installed:
            return 0
    try:
        token = (token_dir / "token").read_text().strip()
    except OSError:
        return 0
    sys.stdout.write(f"username=x-access-token\npassword={token}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
