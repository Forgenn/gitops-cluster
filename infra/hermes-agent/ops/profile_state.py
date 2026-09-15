"""Print the effective capability surface of Hermes homes.

    python - HOME [HOME...] < profile_state.py      (in the pod or a rehearsal pod)

Each home is inspected in a fresh interpreter with HERMES_HOME set: Hermes caches
config and the skill index per process. Read-only; run it as uid 10000.
"""
import os
import subprocess
import sys

PROBE = r'''
import json
from hermes_cli.config import load_config_readonly
from hermes_cli.tools_config import _get_platform_tools
from tools.skills_tool import _find_all_skills
from gateway.config import load_gateway_config, Platform
cfg = load_config_readonly() or {}
api = load_gateway_config().platforms.get(Platform.API_SERVER)
print(json.dumps({
    "model": (cfg.get("model") or {}).get("default"),
    "toolsets": {p: sorted(_get_platform_tools(cfg, p)) for p in ("telegram", "cli", "cron")},
    "skills_enabled": sorted(s["name"] for s in _find_all_skills()),
    "skills_disabled_count": len((cfg.get("skills") or {}).get("disabled") or []),
    "api_server_enabled": bool(api and api.enabled),
    "aux_vision": (cfg.get("auxiliary") or {}).get("vision"),
    "mcp_servers": sorted((cfg.get("mcp_servers") or {}).keys()),
}, indent=1))
'''

for home in sys.argv[1:]:
    r = subprocess.run(["/opt/hermes/.venv/bin/python", "-c", PROBE], cwd="/opt/hermes",
                       env={**os.environ, "HERMES_HOME": home},
                       capture_output=True, text=True, timeout=180)
    print(f"== {home} (exit {r.returncode})")
    print(r.stdout.strip() if r.returncode == 0 else r.stderr.strip()[-1500:])
