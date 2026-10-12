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
    rbac.yaml             # ServiceAccount (no API access; token not mounted)
  frontend/
    deployment.yaml       # SvelteKit app
    service.yaml          # ClusterIP service
  database/
    cluster.yaml          # CNPG PostgreSQL cluster
  secrets/
    externalsecret.yaml   # TOKEN_ENCRYPTION_KEY, OIDC, FCM, Strava
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
| `/loomie/TOKEN_ENCRYPTION_KEY` | Encrypts agent-backend tokens at rest |
| `/pocket-id/loomie/client-id` | Pocket ID OAuth client ID |
| `/pocket-id/loomie/client-secret` | Pocket ID OAuth client secret |
| `/loomie/FCM_SA_KEY` | FCM push notifications |
| `/loomie/STRAVA_*` | Strava integration |

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
```

## Service Connectivity

```
frontend → backend (port 8080)
backend → hrld (port 8080, /api/agents proxy; AGENT_BACKENDS)
hrld → backend (port 8080, /mcp tools for bots)
```
