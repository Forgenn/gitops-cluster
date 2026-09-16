#!/opt/hermes/.venv/bin/python3
"""git pre-push guard for the Hermes agent pod (installed via /etc/gitconfig core.hooksPath).

A push that updates main/master of Forgenn/gitops-cluster with changes under
infra/hermes-agent/ is refused. That directory is the agent's own deployment: its
credentials, RBAC, memory and config sync. The agent must escalate instead: push a
branch hermes/<topic> and send the operator the compare link.

Why a hook: Hermes approvals only review commands its detector flags, and a plain
`git push` is not flagged (tools/approval.py DANGEROUS_PATTERNS, v2026.8.19), so
approvals.smart_policy can never see it. This is a guardrail, not a security
boundary; approvals.deny blocks the obvious bypasses (--no-verify, hooksPath,
GIT_CONFIG_NOSYSTEM, GIT_CONFIG_SYSTEM). Design: gitops-cluster
docs/plans/2026-09-14-hermes-bot-roster.md, Task 5.
"""
from __future__ import annotations

import subprocess
import sys

PROTECTED_REMOTE = "gitops-cluster"
PROTECTED_PATH = "infra/hermes-agent/"
PROTECTED_REFS = frozenset({"refs/heads/main", "refs/heads/master"})
ZERO = "0" * 40
COMPARE = "https://github.com/Forgenn/gitops-cluster/compare/main...hermes/<topic>"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def changed_paths(local_sha: str, remote_sha: str) -> list[str]:
    if remote_sha != ZERO and _git("cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        out = _git("diff", "--name-only", remote_sha, local_sha)
    else:  # new branch, or a remote tip we have never fetched
        out = _git("log", "--format=", "--name-only", local_sha, "--not", "--remotes")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return sorted({line.strip() for line in out.stdout.splitlines() if line.strip()})


def blocked_updates(remote_url: str, updates: list[list[str]]) -> list[tuple[str, list[str]]]:
    if PROTECTED_REMOTE not in remote_url:
        return []
    blocked = []
    for _local_ref, local_sha, remote_ref, remote_sha in updates:
        if remote_ref not in PROTECTED_REFS or local_sha == ZERO:
            continue
        hits = [p for p in changed_paths(local_sha, remote_sha) if p.startswith(PROTECTED_PATH)]
        if hits:
            blocked.append((remote_ref, hits))
    return blocked


def main(argv: list[str], stdin) -> int:
    remote_url = argv[2] if len(argv) > 2 else ""
    updates = [line.split() for line in stdin.read().splitlines() if len(line.split()) == 4]
    try:
        blocked = blocked_updates(remote_url, updates)
    except Exception as exc:
        print(f"ESCALATE: the pre-push guard could not inspect this push ({exc}); refusing. "
              "Ask the operator.", file=sys.stderr)
        return 1
    for ref, hits in blocked:
        print(f"ESCALATE: this push to {ref} changes {len(hits)} file(s) under {PROTECTED_PATH} "
              f"({', '.join(hits[:3])}). That is the agent's own deployment and needs the operator. "
              f"Push a branch named hermes/<topic> instead and send the operator {COMPARE}.",
              file=sys.stderr)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
