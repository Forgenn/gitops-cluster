#!/usr/bin/env bash
# Usage: probe-result.sh PROFILE ID [MARKER]
. "$(dirname "$0")/lib.sh"
ppy "$(home_of "$1")" "$2" "${3:-}" <<'PY'
import glob, json, os, sys
home, jid, marker = sys.argv[1:4]
job = {j["id"]: j for j in json.load(open(f"{home}/cron/jobs.json"))["jobs"]}.get(jid)
if job is None:
    sys.exit(f"{jid}: no such job in {home}")
outs = sorted(glob.glob(f"{home}/cron/output/{jid}/*.md"), key=os.path.getmtime)
text = open(outs[-1], encoding="utf-8").read() if outs else ""
body = text.split("## Response", 1)[1] if "## Response" in text else text
print(f"{jid} last_status={job.get('last_status')} last_error={job.get('last_error')} "
      f"delivery_error={job.get('last_delivery_error')} outputs={len(outs)} "
      f"monitor_state={'set' if job.get('monitor_state') else None}")
if marker:
    print("marker_in_response:", marker in body)
print("response:", body.strip()[:300].replace("\n", " | "))
PY
