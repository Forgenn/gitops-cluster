#!/usr/bin/env bash
# Usage: rehearse-sync.sh PLDER_SHA
# Runs THIS working tree's sync.py against a real plder clone at PLDER_SHA in a
# throwaway pod (same image, `sleep`, uid 10000, no volumes) into a scratch
# HERMES_HOME, then prints the capability surface of every home and the roster
# checks. Never touches /opt/data or the live container (s6 /run/service).
. "$(dirname "$0")/lib.sh"
ref=$1
syncdir=$(cd "$OPS_DIR/../sync" && pwd)
P="bot-rehearsal-$(date +%s)"
image=$(kubectl get deploy hermes-agent -n "$NS" -o jsonpath='{.spec.template.spec.containers[?(@.name=="hermes-agent")].image}')
cleanup() { kubectl delete pod "$P" -n "$NS" --ignore-not-found --wait=true >/dev/null; kubectl get pod "$P" -n "$NS" 2>&1 | tail -1; }
trap cleanup EXIT
kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $P
  namespace: $NS
  labels: {purpose: plan4-rehearsal}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 10000, runAsGroup: 10000}
  containers:
    - name: rehearsal
      image: $image
      command: ["sleep", "1800"]
      env:
        - {name: HOME, value: /tmp/home}
YAML
kubectl wait --for=condition=Ready "pod/$P" -n "$NS" --timeout=600s
rx() { kubectl exec -i -n "$NS" "$P" -- "$@"; }
tar cf - -C "$syncdir" sync.py cron_upsert.py | rx sh -c 'mkdir -p /tmp/sync /tmp/home /tmp/stg /tmp/fh && tar xf - -C /tmp/sync && md5sum /tmp/sync/*.py'
( cd "$syncdir" && md5sum sync.py cron_upsert.py )
kubectl get configmap hermes-config -n "$NS" -o jsonpath='{.data.ssh_config}' | rx sh -c 'cat > /tmp/ssh_config'
kubectl get secret hermes-secrets -n "$NS" -o jsonpath='{.data.PLDER_DEPLOY_KEY_READ}' | base64 -d | rx sh -c 'umask 077; cat > /tmp/k'
rx env HERMES_HOME=/tmp/fh AGENT_CONFIG_REF="$ref" AGENT_IMAGE="$image" PYTHONPATH=/tmp/sync \
  GIT_SSH_COMMAND="ssh -F /tmp/ssh_config -i /tmp/k -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/tmp/stg \
  /opt/hermes/.venv/bin/python -c 'import pathlib, sync; sync.STAGING = pathlib.Path("/tmp/stg"); raise SystemExit(sync.main())' </dev/null 2>&1 | tail -40
echo "--- applied record"; rx cat /tmp/fh/.agent-config/applied </dev/null; echo
homes=$(rx sh -c 'echo /tmp/fh; ls -d /tmp/fh/profiles/* 2>/dev/null' </dev/null | tr -d '\r' | tr '\n' ' ')
echo "--- capability surface (API_SERVER_KEY set, to prove platforms.api_server.enabled: false wins)"
rx env API_SERVER_KEY=rehearsal-dummy-key-0123456789abcdef /opt/hermes/.venv/bin/python - $homes < "$OPS_DIR/profile_state.py"
echo "--- image checks + roster (plan 3's CI step, against the clone)"
rx sh -c 'cp -r /tmp/stg/hermes /tmp/src-hermes && HERMES_HOME=/tmp/ci-home HOME=/tmp/ci-user sh /tmp/src-hermes/ci/image_checks.sh /tmp/src-hermes 2>&1 | tail -15' </dev/null
