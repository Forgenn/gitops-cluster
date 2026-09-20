#!/usr/bin/env bash
# Render and schema-validate every kustomization in infra/.
#
# This is the check that Renovate and the homelab-ops update-review job gate
# on. Before it existed this repo had no workflows at all, so Renovate's
# automergeType 'branch' waited forever for a status that never appeared and
# no dependency commit ever landed -- while ArgoCD auto-synced main to prod.
# Design: docs/plans/2026-09-20-fleet-update-automation.md
#
# Runnable locally: ./scripts/validate-manifests.sh [dir ...]
# Requires kustomize, helm and kubeconform on PATH.
set -uo pipefail

CRD_SCHEMA='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceApiVersion}}.json'
failed=0
count=0

# Bundled upstream charts carry their own templates and non-ASCII READMEs;
# they are excluded from Renovate too (see .github/renovate.json5 ignorePaths).
if [ "$#" -gt 0 ]; then
  targets=("$@")
else
  mapfile -t targets < <(
    find infra -name kustomization.yaml -not -path '*/charts/*' -printf '%h\n' | sort
  )
fi

for dir in "${targets[@]}"; do
  count=$((count + 1))
  if ! out=$(kustomize build --enable-helm "$dir" 2>&1); then
    echo "::error file=${dir}/kustomization.yaml::kustomize build failed"
    printf '%s\n' "$out" | tail -30
    failed=1
    continue
  fi
  # -ignore-missing-schemas: an exotic CRD absent from the catalog passes
  # rather than failing a gate the merge bot depends on. Deliberate v1 trade;
  # tightening it is a separate decision.
  if ! printf '%s\n' "$out" | kubeconform \
      -strict \
      -ignore-missing-schemas \
      -schema-location default \
      -schema-location "$CRD_SCHEMA" \
      -summary; then
    echo "::error file=${dir}/kustomization.yaml::kubeconform rejected the rendered manifests"
    failed=1
  fi
done

echo "checked ${count} kustomization(s)"
exit "$failed"
