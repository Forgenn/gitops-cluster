#!/usr/bin/env bash
# autobump.sh — move internal images/charts to what our CI last published.
#
# Apps in this repo build in their own repos' CI and publish to zot
# (registry.monederobox.dev): loomie pushes images tagged with the git sha it
# built; hrld pushes an OCI chart (0.1.0-build.N) that pins the hrld and
# workbench images inside itself. This script asks the registry what is newest
# and rewrites the manifests to it, one commit per app, using the message
# format the repo already has (`chore(loomie): bump images to <sha>`).
#
# Why not Renovate: this repo is public, so it cannot hold registry
# credentials in its config — internal-registry refs are Renovate's explicit
# blind spot (.github/renovate.json5 disables them). The workflow's repo
# secrets can, which is why the loop lives here.
#
# The script never pushes. .github/workflows/autobump.yml runs it, then gates
# the result on scripts/validate-manifests.sh, and only then pushes main.
#
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

# Bad or missing credentials must fail once, up front — not as a per-tag
# stream of 401s that looks like broken tags.
api "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/" >/dev/null \
  || die "registry auth failed: GET /v2/ on $REGISTRY_HOST rejected the credentials"

MANIFEST_ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'

all_tags() { # <repo>: every tag, following ?n pagination defensively
  local repo=$1 last='' page count t
  while :; do
    page=$(api "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/$repo/tags/list?n=1000${last:+&last=$last}") \
      || die "could not list tags for $repo"
    count=$(jq -r '.tags // [] | length' <<<"$page")
    jq -r '.tags // [] | .[]' <<<"$page"
    (( count < 1000 )) && return 0
    t=$(jq -r '.tags // [] | last // empty' <<<"$page")
    [[ -n "$t" && "$t" != "$last" ]] || die "tags/list pagination for $repo did not advance past '$last'"
    last=$t
  done
}

tag_created() { # <repo> <tag>: the image config's `created` (empty on failure)
  # A tag that cannot be read (e.g. a dangling ref) is skipped with no noise:
  # the newest-tag decision only needs the tags that do resolve.
  local repo=$1 tag=$2 m cdig mdig
  m=$(api -H "Accept: $MANIFEST_ACCEPT" "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/$repo/manifests/$tag" 2>/dev/null) || return 0
  cdig=$(jq -r '.config.digest // empty' <<<"$m")
  if [[ -z "$cdig" ]]; then
    # An index (multi-arch): descend to the amd64 — or first — manifest.
    mdig=$(jq -r '([.manifests[]? | select(.platform.architecture == "amd64")][0].digest // .manifests[0].digest // empty)' <<<"$m")
    [[ -n "$mdig" ]] || return 0
    m=$(api -H "Accept: $MANIFEST_ACCEPT" "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/$repo/manifests/$mdig" 2>/dev/null) || return 0
    cdig=$(jq -r '.config.digest // empty' <<<"$m")
  fi
  [[ -n "$cdig" ]] || return 0
  api "$REGISTRY_SCHEME://$REGISTRY_HOST/v2/$repo/blobs/$cdig" 2>/dev/null | jq -r '.created // empty' || true
}

newest_image_tag() { # <repo>: tag whose config has the latest `created`
  local repo=$1
  {
    while read -r t; do
      [[ -n "$t" ]] || continue
      c=$(tag_created "$repo" "$t")
      [[ -n "$c" ]] || { warn "$repo:$t has no readable created timestamp, skipping"; continue; }
      printf '%s\t%s\n' "$c" "$t"
    done < <(all_tags "$repo")
  } | sort | tail -1 | cut -f2
}

newest_chart_version() { # <repo>: highest x.y.z-build.N
  all_tags "$1" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+-build\.[0-9]+$' | sort -V | tail -1
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

# --- compute and apply bumps ---------------------------------------------------

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

declare -A APP_FILES APP_MSG      # app -> "file\nfile..." / commit message
declare -A APP_TAGS               # app -> tag summary for the notice

for i in "${!T_APP[@]}"; do
  app=${T_APP[i]} kind=${T_KIND[i]} repo=${T_REPO[i]} file=${T_FILE[i]}

  if [[ "$kind" == image-tag ]]; then
    new=$(newest_image_tag "$repo")
    [[ -n "$new" ]] || die "$repo: could not determine the newest tag"
    m=$(grep -oE "image: $MANIFEST_REGISTRY/$repo:[a-zA-Z0-9._-]+" "$file")
    cur=${m##*:}
    [[ "$(grep -cE "image: $MANIFEST_REGISTRY/$repo:" "$file")" -eq 1 ]] \
      || die "$file: expected exactly one 'image: .../$repo:' line, found $(grep -cE "image: $MANIFEST_REGISTRY/$repo:" "$file")"
  else
    new=$(newest_chart_version "$repo")
    [[ -n "$new" ]] || die "$repo: no x.y.z-build.N tags found"
    cur=$(sed -n -E 's/^[[:space:]]+version: (.+)$/\1/p' "$file")
    [[ "$(sed -n -E '/^[[:space:]]+version: /p' "$file" | wc -l)" -eq 1 ]] \
      || die "$file: expected exactly one indented 'version:' line to rewrite"
  fi

  if [[ "$cur" == "$new" ]]; then
    echo "$app $repo: up to date ($cur)"
    continue
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

changed=$(git status --porcelain | wc -l)
if (( changed == 0 )); then
  echo "all tracks up to date; nothing to commit"
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

echo "all tracks checked; $(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0) commit(s) made; validation and push happen in the workflow"
