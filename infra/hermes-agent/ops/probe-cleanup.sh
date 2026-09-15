#!/usr/bin/env bash
# Usage: probe-cleanup.sh PROFILE ID [ID...]
. "$(dirname "$0")/lib.sh"
profile=$1; shift
pexec "$(home_of "$profile")" "$@" <<'SH'
home=$1; shift
for id in "$@"; do
  /command/s6-setuidgid hermes env HERMES_HOME="$home" /opt/hermes/.venv/bin/hermes cron remove "$id" </dev/null 2>&1 | tail -1
done
SH
