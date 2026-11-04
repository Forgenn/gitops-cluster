#!/usr/bin/env bash
# autobump.sh — bump an app's manifests to a version its CI just published.
#
# Apps in this repo build in their own repos' CI and publish to zot
# (registry.monederobox.dev): loomie pushes images tagged with the git sha it
# built; hrld pushes an OCI chart (0.1.0-build.N) that pins the hrld and
# workbench images inside itself. Right after a build publishes, that repo's
# Actions calls this repo's composite action (.github/actions/bump) with
# {app, version}, which runs this script with exactly that version — the
# loomie pattern (its CI bumps its own manifests at build time) generalized:
# no polling, no discovery, the caller names the version.
#
# The version is verified to exist in the registry before any manifest moves:
# a bogus payload fails loudly and never reaches main.
#
# Deploy order: a chart-version track only moves forward. A version lower
# than the one deployed now (a re-run of an old build, or a run that finished
# after a newer one) is refused, unless --allow-older says the rollback is
# deliberate. image-tag tracks are git shas, which the registry cannot order:
# the calling CI checks that order itself (loomie's deploy job only bumps to
# a commit whose ancestor is the tag deployed now).
#
# Why not Renovate: this repo is public, so it cannot hold registry
# credentials in its config — internal-registry refs are Renovate's explicit
# blind spot (.github/renovate.json5 disables them). The workflow's repo
# secrets can, which is why the loop lives here.
#
# The script never pushes: it commits, and the action pushes main.
#
# Usage: ./scripts/autobump.sh [--allow-older] <app> <version>
#        (or: --set <app> <version>)
#   app      matches the first column of scripts/autobump-tracks.sh
#   version  exactly what the build published (sha tag, or x.y.z-build.N)
#   --allow-older  bump a chart-version track even to a lower version
# Requires: curl, jq, git. Credentials: REGISTRY_USERNAME / REGISTRY_PASSWORD
# (zot pull credentials — same values as Infisical /zot/*).
# REGISTRY_SCHEME (default https) and MANIFEST_REGISTRY (the registry host as
# written in the manifests, default registry.monederobox.dev) exist so the
# script can be exercised against a local mock registry in tests.
set -euo pipefail
export LC_ALL=C

REGISTRY_HOST="${REGISTRY_HOST:-registry.monederobox.dev}"
MANIFEST_REGISTRY="${MANIFEST_REGISTRY:-registry.monederobox.dev}"
REGISTRY_SCHEME="${REGISTRY_SCHEME:-https}"
TRACKS_FILE="scripts/autobump-tracks.sh"

APP='' VER='' ALLOW_OLDER=0
args=()
for a in "$@"; do
  case "$a" in
    --allow-older) ALLOW_OLDER=1 ;;
    --set) ;;
    *) args+=("$a") ;;
  esac
done
if [[ ${#args[@]} -eq 2 ]]; then
  APP=${args[0]} VER=${args[1]}
else
  echo "usage: $0 [--allow-older] <app> <version>   (app as in $TRACKS_FILE)" >&2
  exit 1
fi

# chart versions are x.y.z-build.N; returns 0 iff $1 sorts strictly below $2
CHART_VER_RE='^[0-9]+\.[0-9]+\.[0-9]+-build\.[0-9]+$'
version_lt() {
  [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$1" ]]
}

if [[ -z "${REGISTRY_USERNAME:-}" || -z "${REGISTRY_PASSWORD:-}" ]]; then
  echo "::error::REGISTRY_USERNAME / REGISTRY_PASSWORD are not set."
  echo "::error::Add the zot pull credentials as repo secrets (values of Infisical /zot/REGISTRY_USERNAME and /zot/REGISTRY_PASSWORD), then re-run."
  exit 1
fi

cd "$(dirname "$0")/.."

warn() { echo "::warning::$*" >&2; }
die() { echo "::error::$*"; exit 1; }

api() {
  curl -fsS --retry 3 --retry-delay 2 --connect-timeout 10 \
    -u "$REGISTRY_USERNAME:$REGISTRY_PASSWORD" "$@"
}

# Bad or missing credentials must fail once, up front — not as a per-request
# stream of 401s that looks like missing versions.
api "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/" >/dev/null \
  || die "registry auth failed: GET /v2/ on $REGISTRY_HOST rejected the credentials"

MANIFEST_ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'

ref_exists() { # <repo> <ref>: 0 iff the registry serves a manifest for it
  api -H "Accept: $MANIFEST_ACCEPT" \
    "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/$1/manifests/$2" >/dev/null 2>&1
}

# --- load tracks -------------------------------------------------------------

declare -a T_APP T_KIND T_REPO T_FILE
n=0
while read -r app kind repo file; do
  [[ "${app:-}" == \#* || -z "${app:-}" ]] && continue
  case "$kind" in
    image-tag|chart-version) ;;
    *) die "track '$app $kind ...': unknown kind '$kind' (image-tag | chart-version)" ;;
  esac
  [[ -f "$file" ]] || die "track '$app': file $file does not exist"
  T_APP[n]=$app T_KIND[n]=$kind T_REPO[n]=$repo T_FILE[n]=$file
  n=$((n + 1))
done < "$TRACKS_FILE"
(( n > 0 )) || die "no tracks in $TRACKS_FILE"

# --- verify the version exists, then bump --------------------------------------

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

declare -A APP_FILES APP_MSG      # app -> "file\nfile..." / commit message
declare -A APP_TAGS               # app -> tag summary for the notice
matched=0

for i in "${!T_APP[@]}"; do
  app=${T_APP[i]} kind=${T_KIND[i]} repo=${T_REPO[i]} file=${T_FILE[i]}
  [[ "$app" == "$APP" ]] || continue
  matched=1

  ref_exists "$repo" "$VER" \
    || die "$repo:$VER does not exist in the registry (refusing to bump)"
  new=$VER

  # the current value, straight from the file, with single-match asserts
  if [[ "$kind" == image-tag ]]; then
    m=$(grep -oE "image: $MANIFEST_REGISTRY/$repo:[a-zA-Z0-9._-]+" "$file")
    cur=${m##*:}
    [[ "$(grep -cE "image: $MANIFEST_REGISTRY/$repo:" "$file")" -eq 1 ]] \
      || die "$file: expected exactly one 'image: .../$repo:' line, found $(grep -cE "image: $MANIFEST_REGISTRY/$repo:" "$file")"
  else
    cur=$(sed -n -E 's/^[[:space:]]+version: (.+)$/\1/p' "$file")
    [[ "$(sed -n -E '/^[[:space:]]+version: /p' "$file" | wc -l)" -eq 1 ]] \
      || die "$file: expected exactly one indented 'version:' line to rewrite"
  fi

  if [[ "$cur" == "$new" ]]; then
    echo "$app $repo: already at $cur"
    continue
  fi

  # deploy order: chart versions only move forward unless told otherwise
  if [[ "$kind" == chart-version ]] && (( ! ALLOW_OLDER )); then
    [[ "$cur" =~ $CHART_VER_RE && "$new" =~ $CHART_VER_RE ]] \
      || die "$app $repo: cannot order $cur and $new (expected x.y.z-build.N); pass --allow-older to bump anyway"
    if version_lt "$new" "$cur"; then
      die "$app $repo: $new is older than the deployed $cur; not bumping (pass --allow-older for a deliberate rollback)"
    fi
  fi

  lineno=$(grep -nE "image: $MANIFEST_REGISTRY/$repo:|^[[:space:]]+version: " "$file" | head -1 | cut -d: -f1)
  if [[ "$kind" == image-tag ]]; then
    re="image: $MANIFEST_REGISTRY/$repo:[a-zA-Z0-9._-]+"
  else
    re="version: .+$"
  fi
  awk -v n="$lineno" -v re="$re" -v rep="image: $MANIFEST_REGISTRY/$repo:$new" \
      -v repv="version: $new" '
    NR == n {
      if (re ~ /^image:/) sub(re, rep); else sub(re, repv)
    }
    { print }
  ' "$file" > "$file.tmp" && mv "$file.tmp" "$file"

  if ! git diff --quiet -- "$file"; then
    APP_FILES[$app]+="$file"$'\n'
    APP_TAGS[$app]+="$repo:$cur -> $new"$'\n'
    if [[ "$kind" == image-tag ]]; then
      APP_MSG[$app]+="${repo##*/}=$new"$'\n'
    fi
    echo "$app $repo: $cur -> $new"
  else
    die "$file: edit for $repo:$new produced no diff (pattern mismatch)"
  fi
done

if [[ "$matched" -eq 0 ]]; then
  die "no track for app '$APP' in $TRACKS_FILE"
fi

changed=$(git status --porcelain | wc -l)
if (( changed == 0 )); then
  echo "$APP is already at $VER; nothing to commit"
  exit 0
fi

# One commit per app, so each app's bump is its own ArgoCD sync.
for app in "${!APP_FILES[@]}"; do
  files=${APP_FILES[$app]%$'\n'}
  if [[ -n "${APP_MSG[$app]:-}" ]]; then
    distinct_tags=$(printf '%s' "${APP_MSG[$app]}" | sed 's/^[^=]*=//' | sort -u)
    if [[ "$(printf '%s\n' "$distinct_tags" | wc -l)" -eq 1 ]]; then
      msg="chore($app): bump images to $distinct_tags"
    else
      msg="chore($app): bump images ($(printf '%s' "${APP_MSG[$app]}" | sort -u | paste -sd ' ' -))"
    fi
  else
    ver=$(printf '%s' "${APP_TAGS[$app]}" | sed -n -E 's/.*-> (.+)$/\1/p' | head -1)
    msg="chore($app): bump chart to $ver"
  fi
  # shellcheck disable=SC2086  # files is a newline list of paths, none spaced
  git add $files
  git commit -q -m "$msg" -- $files
  echo "::notice::$(git log --oneline -1 | cut -c1-7) $msg"
done

echo "$APP bumped; validation and push happen in the workflow"
