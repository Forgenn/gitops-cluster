# Tracks for scripts/autobump.sh (run by .github/workflows/autobump.yml).
#
# One track per line:  <app> <kind> <zot repository> <manifest file>
# An app's CI publishes a version and fires repository_dispatch 'bump'
# {app, version}; the workflow bumps every track of that app to it.
# Adding an app that publishes to zot is one line here — nothing else.
#
#   image-tag      the file pins `image: registry.monederobox.dev/<repo>:<tag>`
#                  (tags are the git shas loomie builds).
#   chart-version  the file pins `version:` under helmCharts; the chart pins
#                  its own images (tag + digest) inside itself, so one bump
#                  moves everything the app ships (hrld + its workbenches).
#                  Versions are x.y.z-build.N.
#
# app          kind           zot repository      manifest file
loomie        image-tag      loomie/backend      infra/loomie/backend/deployment.yaml
loomie        image-tag      loomie/frontend     infra/loomie/frontend/deployment.yaml
hrld          chart-version  hrld/charts/hrld    infra/hrld/kustomization.yaml
