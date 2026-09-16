#!/usr/bin/env python3
"""Serve one more bot: allowlist it and route its Telegram topic in plder's root config.

    python add_route.py <hermes/root/config.yaml> <profile> <chat_id> <thread_id>

Edits the text (comments and CRLF/LF survive), then re-parses both versions and
refuses to write unless exactly the allowlist entry and the route were added.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import yaml

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _block_end(lines: list[str], header: str) -> int:
    idx = next((i for i, line in enumerate(lines) if line.rstrip() == header), None)
    if idx is None:
        raise ValueError(f"{header.strip()} not found")
    indent = len(header) - len(header.lstrip())
    end = idx + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        end += 1
    while end > idx + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def add_route(text: str, profile: str, chat_id: str, thread_id: str) -> str:
    if not _PROFILE_RE.match(profile):
        raise ValueError(f"invalid profile name {profile!r}")
    if not thread_id.isdigit() or not chat_id.lstrip("-").isdigit():
        raise ValueError("chat_id and thread_id must be numeric")
    before = yaml.safe_load(text) or {}
    gateway = before.get("gateway") or {}
    if profile in (gateway.get("multiplex_profile_allowlist") or []):
        raise ValueError(f"{profile} is already in multiplex_profile_allowlist")
    for route in gateway.get("profile_routes") or []:
        if str(route.get("thread_id")) == thread_id:
            raise ValueError(f"thread {thread_id} is already routed to {route.get('profile')}")

    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    lines.insert(_block_end(lines, "  multiplex_profile_allowlist:"), f"    - {profile}")
    end = _block_end(lines, "  profile_routes:")
    lines[end:end] = [
        f"    - name: {profile}-topic",
        "      platform: telegram",
        f'      chat_id: "{chat_id}"',
        f'      thread_id: "{thread_id}"',
        f"      profile: {profile}",
    ]
    new = eol.join(lines)

    expected = copy.deepcopy(before)
    expected["gateway"]["multiplex_profile_allowlist"].append(profile)
    expected["gateway"]["profile_routes"].append({
        "name": f"{profile}-topic", "platform": "telegram",
        "chat_id": chat_id, "thread_id": thread_id, "profile": profile})
    if yaml.safe_load(new) != expected:
        raise ValueError("the edit changed more than the allowlist and the routes; refusing")
    return new


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        new = add_route(path.read_bytes().decode("utf-8"), argv[2], argv[3], argv[4])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path.write_bytes(new.encode("utf-8"))
    print(f"added {argv[2]} -> thread {argv[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
