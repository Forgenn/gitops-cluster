# Sourced by the other ops scripts. Workstation side (Git Bash). See README.md.
set -euo pipefail
export MSYS_NO_PATHCONV=1
NS=hermes
VENV_PY=/opt/hermes/.venv/bin/python
CHAT_ID=7850573137
OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

pod() {
  kubectl get pods -n "$NS" -l app=hermes-agent --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
}

home_of() {
  case "$1" in
    default) echo /opt/data ;;
    *) echo "/opt/data/profiles/$1" ;;
  esac
}

# POSIX sh script on stdin, run as root in the main container; args become $1..$n.
# Inside, give every command that may read stdin </dev/null.
pexec() { kubectl exec -i -n "$NS" "$(pod)" -c hermes-agent -- sh -s -- "$@"; }

# Python script on stdin, run as uid 10000 in the main container.
ppy() { kubectl exec -i -n "$NS" "$(pod)" -c hermes-agent -- /command/s6-setuidgid hermes "$VENV_PY" - "$@"; }
