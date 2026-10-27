# Tracks for scripts/autobump.sh (run by .github/workflows/autobump.yml).
#
# One track per line:  <app> <kind> <zot repository> <manifest file>
# Adding an app that publishes to zot is one line here — nothing else.
#
#   image-tag      the file pins `image: registry.monederobox.dev/<repo>:<tag>`.
#                  loomie tags are git shas, so "newest" is decided by the
#                  `created` timestamp in each tag's config blob, not by the
#                  tag string.
#   chart-version  the file pins `version:` under helmCharts; the chart pins
#                  its own images (tag + digest) inside itself, so one bump
#                  moves everything the app ships (hrld + its workbenches).
#                  Versions are x.y.z-build.N: newest is the highest N.
#
# app          kind           zot repository      manifest file
loomie        image-tag      loomie/backend      infra/loomie/backend/deployment.yaml
loomie        image-tag      loomie/frontend     infra/loomie/frontend/deployment.yaml
hrld          chart-version  hrld/charts/hrld    infra/hrld/kustomization.yaml
