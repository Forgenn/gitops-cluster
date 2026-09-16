#!/usr/bin/env bash
# Usage: profile-state.sh PROFILE [PROFILE...]
. "$(dirname "$0")/lib.sh"
homes=(); for p in "$@"; do homes+=("$(home_of "$p")"); done
ppy "${homes[@]}" < "$OPS_DIR/profile_state.py"
