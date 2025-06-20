# gitops-cluster

This repository contains the GitOps configuration for the 'revachol' k3s cluster. It follows the GitOps pattern using ArgoCD as the GitOps operator.

## Repository Structure

```
.
├── .github/workflows/     # CI/CD workflows (optional)
├── cluster-config/        # Cluster-wide configurations
│   ├── bootstrap/        # Bootstrap configurations (ArgoCD, etc.)
│   ├── core-services/    # Core cluster services (Ingress, Cert-Manager, etc.)
│   └── secrets-management/ # Secrets management configurations
└── applications/         # Application deployments
```

## Core Components

- **ArgoCD**: GitOps operator for continuous delivery
- **Sealed Secrets**: Kubernetes controller for encrypting secrets
- **Ingress-Nginx**: Ingress controller for managing external access
- **Cert-Manager**: Certificate management for TLS

## Getting Started

1. Ensure you have access to the k3s cluster
2. Follow the bootstrap process in `cluster-config/bootstrap/`
3. Core services will be deployed automatically via ArgoCD
4. Applications can be deployed by adding configurations to the `applications/` directory

## Prerequisites

- k3s cluster running on 'revachol'
- kubectl configured to access the cluster
- ArgoCD CLI (argocd) for management
- kubeseal for secrets management

## Security

- All sensitive data is encrypted using Sealed Secrets
- Access to the ArgoCD UI is restricted
- TLS certificates are managed via Cert-Manager

## Contributing

1. Create a new branch for your changes
2. Make your changes following the existing patterns
3. Submit a pull request for review

## License

