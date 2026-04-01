# GitOps Cluster

This repository contains the infrastructure for my home server, managed by ArgoCD.

## Structure

Each directory in the `infra` directory represents a separate application or component. These are automatically synced by an ArgoCD ApplicationSet.

## Observations

- Media management currently includes Navidrome for music streaming and Suwayomi for manga reading
- Infrastructure ready for *arr suite implementation with existing CNPG, storage, and networking stack

## Technical Debt

- **Music library NFS path**: Navidrome's music library lives inside OpenCloud's dynamically-provisioned PVC (`pvc-a2248f1d-...`), mounted directly via NFS by UUID path. Should be migrated to a dedicated named ZFS dataset (`revachol-pool/media/music`) like downloads and books. Lidarr currently also mounts this UUID path — both should be updated once migrated.
