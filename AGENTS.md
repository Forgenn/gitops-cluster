In this repository there is the configuration for a kubernetes cluster managed with ArgoCD via the GitOps pattern. The initialization of ArgoCD is generated via ApplicationSets poiting to `infra` in another repository.

Whenever we find another observation about this repos state, and it's not declared here, make a very short comment in README.md. This only concerns global observations about the state of the cluster configuration, not about a particular application.

## Conventions

*   **External Secrets**: Secrets are managed using External Secrets Operator (v1). The `key` in the `ExternalSecret` resource must be the full path to the secret in the secret store (e.g., `/app-name/secret-key`).
*   **Backups**: PersistentVolumeClaims are backed up using `volsync`.
