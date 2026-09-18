#!/opt/hermes/.venv/bin/python3
# infra/hermes-agent/githooks/git_credential_developer_pat.py
"""git-credential helper for the developer bot's fine-grained PAT.

Registered process-wide via /etc/gitconfig (credential.https://github.com.helper),
so it runs for EVERY profile's git subprocess -- but it only ever emits anything
when $DEVELOPER_GITHUB_PAT is a non-empty string in that specific subprocess's
environment. That variable reaches a subprocess's environment only through
Hermes' terminal.env_passthrough mechanism, which resolves its VALUE from the
CURRENTLY ACTIVE profile's secret scope (agent.secret_scope, tools/env_passthrough.py
resolve_passthrough_value) -- so only a developer-scoped terminal call, whose
secrets.command helper was extended to read
/etc/hermes-profile-secrets-developer/DEVELOPER_GITHUB_PAT, ever sees it set.
Every other profile's terminal subprocess has it unset, and this helper is then a
silent no-op (git falls through with no credentials for that push).

This is a git-credential 'get' handler only (store/erase are ignored -- a fine-
grained PAT is never persisted to a credential cache in this pod). Never logs or
echoes the token value anywhere, on any exit path.

Design: gitops-cluster docs/plans/2026-09-15-hermes-developer-meta-docs.md, Task 3.
"""
from __future__ import annotations

import os
import sys


def handle_get(stdin_text: str, env: dict) -> str:
    fields = dict(
        line.split("=", 1) for line in stdin_text.splitlines() if "=" in line
    )
    token = env.get("DEVELOPER_GITHUB_PAT", "")
    if not token:
        return ""
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return ""
    return f"username=x-access-token\npassword={token}\n"


def main(argv: list[str]) -> int:
    op = argv[1] if len(argv) > 1 else ""
    stdin_text = sys.stdin.read()
    if op == "get":
        sys.stdout.write(handle_get(stdin_text, os.environ))
    # 'store' and 'erase': deliberately no-op, nothing to persist or clear.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
