# GitOps Cluster

This repository contains the infrastructure for my home server, managed by ArgoCD.

## Structure

Each directory in the `infra` directory represents a separate application or component. These are automatically synced by an ArgoCD ApplicationSet.

## Observations

- Media management currently includes Navidrome for music streaming and Suwayomi for manga reading
- Infrastructure ready for *arr suite implementation with existing CNPG, storage, and networking stack
