#!/usr/bin/env bash
# Usage: wait-deploy.sh PLDER_FULL_SHA
# Waits for plder CI on that commit, the gitops pin, ArgoCD and the rollout, then
# prints the profile-sync log. Run with run_in_background if foreground sleep is blocked.
. "$(dirname "$0")/lib.sh"
sha=$1
until RUN=$(gh run list --repo Forgenn/plder --workflow hermes-config.yml --json databaseId,headSha \
             --jq ".[] | select(.headSha==\"$sha\") | .databaseId" | head -1) && [ -n "$RUN" ]; do sleep 10; done
gh run watch "$RUN" --repo Forgenn/plder --exit-status
until [ "$(kubectl get deploy hermes-agent -n "$NS" -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}')" = "${sha:0:12}" ]; do sleep 15; done
GITOPS_SHA=$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} rev={.status.sync.revision} conditions={.status.conditions}{"\n"}'
echo "gitops main: $GITOPS_SHA"
kubectl rollout status deployment/hermes-agent -n "$NS" --timeout=900s
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n "$NS" --timeout=600s
kubectl logs -n "$NS" "$(pod)" -c profile-sync
