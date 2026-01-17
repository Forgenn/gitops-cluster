# Loomie - Kubernetes Deployment

GitOps manifests for Loomie deployment on K8s.

## Architecture

```
loomie/
  namespace.yaml          # Namespace definition
  httproute.yaml          # Ingress via Envoy Gateway
  kustomization.yaml      # Kustomize root
  backend/
    deployment.yaml       # Go API server
    service.yaml          # ClusterIP service
    rbac.yaml             # ServiceAccount + Role for K8s API
  frontend/
    deployment.yaml       # SvelteKit app
    service.yaml          # ClusterIP service
  database/
    cluster.yaml          # CNPG PostgreSQL cluster
  secrets/
    externalsecret.yaml   # ANTHROPIC_API_KEY, JWT_SECRET, OIDC, NTFY
    registry-secret.yaml  # Docker registry auth
```

## CI/CD Flow

1. Push to `loomie` repo (code)
2. GitHub Actions builds images → `registry.monederobox.dev`
3. GitHub Actions updates this repo with new image tags (automated via `update-gitops` job)
4. ArgoCD watches this repo → syncs manifests
5. K8s pulls new images with specific SHA tags

**Required secret**: `GITOPS_TOKEN` - GitHub PAT with write access to this repo

## Required Secrets in Infisical

Project: `revachol-cluster-a82f`, Environment: `prod`

| Path | Description |
|------|-------------|
| `/loomie/ANTHROPIC_API_KEY` | Anthropic API key |
| `/loomie/JWT_SECRET` | JWT signing secret |
| `/loomie/OIDC_CLIENT_ID` | Pocket ID OAuth client ID |
| `/loomie/OIDC_CLIENT_SECRET` | Pocket ID OAuth client secret |
| `/loomie/NTFY_TOKEN` | ntfy.sh push notification token |

## Troubleshooting

```bash
# Check backend logs
kubectl logs -n loomie deployment/loomie-backend

# Check secrets
kubectl get externalsecret -n loomie

# Force restart
kubectl rollout restart deployment loomie-backend -n loomie
```

## Image Registry

```
registry.monederobox.dev/loomie/backend:latest
registry.monederobox.dev/loomie/frontend:latest
registry.monederobox.dev/loomie/env-node-20:latest
registry.monederobox.dev/loomie/env-python-3.12:latest
registry.monederobox.dev/loomie/env-go-1.22:latest
```
