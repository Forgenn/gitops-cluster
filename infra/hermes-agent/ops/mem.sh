#!/usr/bin/env bash
# Usage: mem.sh [RANGE]   max working set of the hermes-agent container, e.g. 1h, 24h
. "$(dirname "$0")/lib.sh"
q="max(max_over_time(container_memory_working_set_bytes{namespace=\"hermes\",container=\"hermes-agent\"}[${1:-1h}]))"
kubectl get --raw "/api/v1/namespaces/monitoring/services/prometheus-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=$(python -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$q")" \
  | python -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; m=round(float(r[0]["value"][1])/2**20); print(f"max working set: {m} MiB", "(GATE FAILED: >= 2304 MiB)" if m >= 2304 else "(ok)")'
