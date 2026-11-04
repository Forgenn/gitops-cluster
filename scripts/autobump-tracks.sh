# Tracks for scripts/autobump.sh — one line per tracked ref.
#
# An app repo CI publishes a version and calls this repo's composite action
# (.github/actions/bump) with {app, version}; it verifies the version in zot
# and bumps every track of that app to it, committing straight to main.
# Adding an app that publishes to zot is one line here — nothing else.
#
#   image-tag      the file pins `image: registry.monederobox.dev/<repo>:<tag>`.
#                  Tags are git shas, which cannot be ordered here: the
#                  calling CI checks order (loomie's deploy job bumps only
#                  to a descendant of the deployed commit; it runs its own
#                  scripts/gitops-bump.sh, not this action).
#   chart-version  the file pins `version:` under helmCharts; the chart pins
#                  its own images (tag + digest) inside itself, so one bump
#                  moves everything the app ships (hrld + its workbenches).
#                  Versions are x.y.z-build.N: autobump.sh refuses one
#                  lower than the deployed version (--allow-older to roll
#                  back on purpose).
#
# app          kind           zot repository      manifest file
loomie        image-tag      loomie/backend      infra/loomie/backend/deployment.yaml
loomie        image-tag      loomie/frontend     infra/loomie/frontend/deployment.yaml
hrld          chart-version  hrld/charts/hrld    infra/hrld/kustomization.yaml
