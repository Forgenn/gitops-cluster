#!/usr/bin/env bash
# Usage: agent-probe.sh PROFILE NAME PROMPT [DELIVER] [-- extra `hermes cron create` args]
# Schedules an agent job ~3 minutes ahead so the GATEWAY's ticker runs it. Never
# `cron run`: that executes inline in the CLI and proves nothing about the gateway.
. "$(dirname "$0")/lib.sh"
profile=$1; name=$2; prompt=$3; deliver=${4:-local}
shift $(( $# < 4 ? $# : 4 )); if [ "${1:-}" = "--" ]; then shift; fi
when="$(date -u -d "@$(( $(date +%s) + 180 ))" +"%M %H") * * *"
pexec "$(home_of "$profile")" "$name" "$when" "$prompt" "$deliver" "$@" <<'SH'
home=$1; name=$2; when=$3; prompt=$4; deliver=$5; shift 5
/command/s6-setuidgid hermes env HERMES_HOME="$home" /opt/hermes/.venv/bin/hermes \
  cron create "$when" "$prompt" --name "$name" --deliver "$deliver" "$@" </dev/null >/dev/null 2>&1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python - "$home" "$name" <<'PY'
import json, sys
ids = [j["id"] for j in json.load(open(sys.argv[1] + "/cron/jobs.json"))["jobs"] if j.get("name") == sys.argv[2]]
print(ids[-1] if ids else "NOT-CREATED")
PY
SH
echo "fires at UTC ${when% \* \* \*} (now $(date -u +%H:%M))" >&2
