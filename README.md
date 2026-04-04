# GitOps Cluster

This repository contains the infrastructure for my home server, managed by ArgoCD.

## Structure

Each directory in the `infra` directory represents a separate application or component. These are automatically synced by an ArgoCD ApplicationSet.

## Observations

- Media management currently includes Navidrome for music streaming and Suwayomi for manga reading
- Infrastructure ready for *arr suite implementation with existing CNPG, storage, and networking stack

## Next Steps

- **beets integration**: Deploy beets in k8s with music NFS mounted, wire to Lidarr's "On Download" custom script hook. Lidarr handles download automation, beets handles post-processing (metadata, renaming, ReplayGain). Blocked on legacy server migration (beets currently runs there).
- **Legacy server migration**: Move remaining services from old server into the cluster.

## Observations

- Completed pods accumulate cluster-wide due to no TTL/GC policy; most visible in the `argocd` namespace (~200 haproxy/RS pods)
- 25/34 ArgoCD applications are persistently OutOfSync, masking real failures; root causes are ESO field drift and unresolved hook resources
- Several raw-manifest apps (kavita, lidarr, navidrome, etc.) have no resource requests/limits or health probes
- No automated image update tooling (Renovate/Dependabot) is configured

## Technical Debt

- **Music library NFS path**: Navidrome's music library lives inside OpenCloud's dynamically-provisioned PVC (`pvc-a2248f1d-...`), mounted directly via NFS by UUID path. Should be migrated to a dedicated named ZFS dataset (`revachol-pool/media/music`) like downloads and books. Lidarr currently also mounts this UUID path — both should be updated once migrated.
