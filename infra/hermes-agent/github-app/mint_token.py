"""Mint GitHub App installation tokens into a shared directory (Hermes pod sidecar).

The App (forgenn-hermes) private key is mounted ONLY in this sidecar. The Hermes
container sees just the 1-hour token in /run/github-token (an in-memory emptyDir);
without shareProcessNamespace it cannot read this process's key or environment.
docs/plans/2026-09-29-phase2-github-identity.md Task 3.

Writes, every 30 minutes:
  token       the installation token (0440, gid 10000)
  expires_at  its expiry, ISO 8601 (hermes-watch alerts when it is stale)
  repos       the repos the App is installed on, lowercase owner/name, one per line:
              the git credential helper answers only for these, so pushes to other
              repos fall through to the next helper (the developer PAT)
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import jwt

API = "https://api.github.com"
REFRESH_SECONDS = 30 * 60
PER_PAGE = 100
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def app_jwt(app_id: str, private_key_pem: bytes, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    claims = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


def _post_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, method="POST", headers=headers, data=b"")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _get_json(url: str, headers: dict) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
        return json.load(resp)


def mint(app_id: str, installation_id: str, private_key_pem: bytes) -> tuple[str, str]:
    headers = dict(HEADERS, Authorization=f"Bearer {app_jwt(app_id, private_key_pem)}")
    body = _post_json(f"{API}/app/installations/{installation_id}/access_tokens", headers)
    return body["token"], body["expires_at"]


def installed_repos(token: str) -> list[str]:
    headers = dict(HEADERS, Authorization=f"token {token}")
    names, page = [], 1
    while True:
        body = _get_json(f"{API}/installation/repositories?per_page={PER_PAGE}&page={page}", headers)
        batch = body.get("repositories", [])
        names += [r["full_name"].lower() for r in batch]
        if not batch or len(names) >= body.get("total_count", 0):
            return sorted(names)
        page += 1


def write_files(out_dir: Path, files: dict[str, str], gid: int) -> None:
    out_dir = Path(out_dir)
    for name, value in files.items():
        tmp = out_dir / f"{name}.tmp"
        tmp.unlink(missing_ok=True)  # a crash may have left a read-only one
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.chown(tmp, -1, gid)
        os.chmod(tmp, 0o440)
        os.replace(tmp, out_dir / name)


def main() -> int:
    app_id = os.environ["GITHUB_APP_ID"]
    inst = os.environ["GITHUB_APP_INSTALLATION_ID"]
    key = Path(os.environ.get("GITHUB_APP_KEY_FILE", "/secrets/github-app/key.pem")).read_bytes()
    out = Path(os.environ.get("TOKEN_DIR", "/run/github-token"))
    gid = int(os.environ.get("TOKEN_GID", "10000"))
    while True:
        try:
            token, exp = mint(app_id, inst, key)
            files = {"token": token, "expires_at": exp}
            try:
                files["repos"] = "\n".join(installed_repos(token)) + "\n"
            except Exception as exc:  # keep the previous list; the token still matters most
                print(f"repo list failed: {type(exc).__name__}: {getattr(exc, 'code', '')}", file=sys.stderr, flush=True)
            write_files(out, files, gid)
            print(f"minted token, expires {exp}", flush=True)
            sleep = REFRESH_SECONDS
        except Exception as exc:  # never print the key or token; the class and HTTP status are enough
            print(f"mint failed: {type(exc).__name__}: {getattr(exc, 'code', '')}", file=sys.stderr, flush=True)
            sleep = 60
        time.sleep(sleep)


if __name__ == "__main__":
    sys.exit(main())
