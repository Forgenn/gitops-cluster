#!/usr/bin/env bash
# Usage: noagent-probe.sh PROFILE NAME SCRIPT DELIVER   (SCRIPT relative to <home>/scripts/)
. "$(dirname "$0")/lib.sh"
profile=$1; name=$2; script=$3; deliver=$4
when="$(date -u -d "@$(( $(date +%s) + 180 ))" +"%M %H") * * *"
pexec "$(home_of "$profile")" "$name" "$when" "$script" "$deliver" <<'SH'
home=$1; name=$2; when=$3; script=$4; deliver=$5
/command/s6-setuidgid hermes env HERMES_HOME="$home" /opt/hermes/.venv/bin/hermes \
  cron create "$when" --name "$name" --script "$script" --no-agent --deliver "$deliver" </dev/null >/dev/null 2>&1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python - "$home" "$name" <<'PY'
import json, sys
ids = [j["id"] for j in json.load(open(sys.argv[1] + "/cron/jobs.json"))["jobs"] if j.get("name") == sys.argv[2]]
print(ids[-1] if ids else "NOT-CREATED")
PY
SH
echo "fires at UTC ${when% \* \* \*} (now $(date -u +%H:%M))" >&2
