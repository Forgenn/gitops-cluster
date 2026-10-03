#!/usr/bin/env python3
"""Apply infra/pocket-id/oidc-config/apis.yaml to Pocket ID (v2 admin API). Idempotent.

Creates missing APIs, sets each API's permission list, and sets the grant of every listed client.
Never deletes an API or an unlisted grant. Dry run by default; pass --apply to write.

The admin API key is read from the POCKET_ID_API_KEY environment variable and never printed, e.g.:
  POCKET_ID_API_KEY=$(infisical export --path /pocket-id --format json | jq -r '.[]|select(.key=="ADMIN_API_KEY").value') \
    scripts/pocket-id-apply.py --apply
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

BASE = os.environ.get("POCKET_ID_URL", "https://pocket-id.monederobox.dev") + "/api"
CONFIG = Path(__file__).resolve().parent.parent / "infra/pocket-id/oidc-config/apis.yaml"


def call(method: str, path: str, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-API-KEY": os.environ["POCKET_ID_API_KEY"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            raw = res.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: {e.code} {e.read().decode(errors='replace')[:300]}")


def paged(path: str):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        res = call("GET", f"{path}{sep}pagination%5Bpage%5D={page}&pagination%5Blimit%5D=100")
        out += res["data"]
        if page >= res["pagination"]["totalPages"]:
            return out
        page += 1


def main() -> None:
    apply = "--apply" in sys.argv
    want = yaml.safe_load(CONFIG.read_text())["apis"]
    clients = {c["name"]: c for c in paged("/oidc/clients")}
    existing = {a["resource"]: a for a in paged("/apis")}
    for spec in want:
        api = existing.get(spec["resource"])
        if api is None:
            print(f"create API {spec['name']} ({spec['resource']})")
            if not apply:
                continue
            api = call("POST", "/apis", {"name": spec["name"], "resource": spec["resource"]})
        perms = [{k: p[k] for k in ("key", "name", "description") if k in p} for p in spec.get("permissions", [])]
        have = sorted((p["key"], p["name"], p.get("description")) for p in api.get("permissions", []))
        if have != sorted((p["key"], p["name"], p.get("description")) for p in perms):
            print(f"set permissions of {spec['name']}: {[p['key'] for p in perms]}")
            if apply:
                api = call("PUT", f"/apis/{api['id']}/permissions", {"permissions": perms})
        ids = {p["key"]: p["id"] for p in api.get("permissions", [])}
        current = {g["client"]["id"]: g for g in (paged(f"/apis/{api['id']}/clients") if "id" in api else [])}
        for grant in spec.get("clients", []):
            client = clients.get(grant["name"])
            if client is None:
                sys.exit(f"unknown OIDC client {grant['name']!r}")
            body = {
                "userDelegatedAccess": bool(grant.get("userDelegated")),
                "clientAccess": bool(grant.get("clientCredentials")),
                "userDelegatedPermissionIds": [ids[k] for k in grant.get("userDelegated", [])],
                "clientPermissionIds": [ids[k] for k in grant.get("clientCredentials", [])],
            }
            have = current.get(client["id"])
            if have is not None and all(
                    (sorted(have.get(k) or []) if isinstance(v, list) else have.get(k)) == (sorted(v) if isinstance(v, list) else v)
                    for k, v in body.items()):
                continue
            print(f"grant {grant['name']} on {spec['name']}: user={grant.get('userDelegated', [])} client={grant.get('clientCredentials', [])}")
            if apply:
                call("PUT", f"/apis/{api['id']}/clients/{client['id']}", body)
    if not apply:
        print("(dry run; pass --apply to write)")


if __name__ == "__main__":
    main()
