# Media Automation Stack (*arr Suite)

**Date**: 2026-03-31  
**Status**: Approved, ready to implement

---

## Overview

Deploy a full media automation stack for music and books, plus migrate the existing Calibre library. No VPN required (cluster in Spain, low enforcement risk).

---

## Stack

| App | Role | Namespace | Port |
|---|---|---|---|
| **qBittorrent** | Torrent download client | `qbittorrent` | 8080 (WebUI), 6881 (torrent) |
| **Prowlarr** | Indexer manager (feeds Lidarr + Readarr) | `prowlarr` | 9696 |
| **Lidarr** | Music collection automation | `lidarr` | 8686 |
| **Readarr** | Book collection automation (EPUB-only quality profile) | `readarr` | 8787 |
| **Kavita** | Book library reader (modern UI, OPDS, Kobo sync) | `kavita` | 5000 |

---

## Architecture Decisions

### Download client: qBittorrent (no VPN)
- No Gluetun sidecar — plain qBittorrent pod
- Spanish IP, low copyright enforcement risk
- Standard *arr integration via categories

### Book frontend: Kavita over Calibre-Web Automated
- Modern .NET app, significantly better reading UI
- Readarr configured to EPUB-only → format conversion not needed
- Simpler setup (no Calibre binary dependency)
- Existing Calibre library migrated: Kavita scans the book folder directly (no DB migration needed)

### Shared storage: NFS direct mounts (not cross-namespace PVCs)
Same pattern as Navidrome's existing music mount. Each namespace mounts the NFS path it needs by path, avoiding cross-namespace PVC complexity.

```
dolores.home:/revachol-pool/
  media/
    downloads/     ← qBittorrent writes, Lidarr + Readarr read/move
    music/         ← existing Navidrome path, Lidarr writes here
    books/         ← existing Calibre library path, Readarr writes, Kavita reads
```

> **Note**: `music/` NFS path is already known from Navidrome config.  
> `downloads/` and `books/` paths must be confirmed/created on dolores.home before deploy.  
> If the Calibre library lives on the legacy server, migrate it to dolores.home first.

### Config/database storage: Longhorn PVC per app
Each app gets a small Longhorn PVC for its own config and internal database (separate from media files).

### Networking: Internal-only for all apps
All five apps restricted to Tailscale + LAN CIDRs via `SecurityPolicy` (same pattern as suwayomi). No public exposure.

### Backups: volsync per app
Config PVCs backed up via volsync restic → Hetzner S3, same pattern as suwayomi/sparky-fitness. Media files on NFS are the responsibility of dolores.home's own backup.

### Secrets: Infisical ExternalSecret per app
API keys and passwords via `infisical-cluster-secret-store` ClusterSecretStore.

---

## Container Images

| App | Image |
|---|---|
| qBittorrent | `lscr.io/linuxserver/qbittorrent:latest` |
| Prowlarr | `lscr.io/linuxserver/prowlarr:latest` |
| Lidarr | `lscr.io/linuxserver/lidarr:latest` |
| Readarr | `lscr.io/linuxserver/readarr:latest` |
| Kavita | `ghcr.io/kareadita/kavita:latest` |

All LinuxServer images use `PUID=1000` / `PGID=1000`.

---

## File Structure (per app, consistent pattern)

```
infra/<app>/
  kustomization.yaml       # namespace set here, lists all resources
  deployment.yaml
  service.yaml
  pvc.yaml                 # Longhorn, config data only
  httproute.yaml           # internal SecurityPolicy + HTTPRoute
  externalsecret.yaml      # app password/API key from Infisical
  restic-config.yaml       # volsync backup credentials
  replicationsource.yaml   # volsync backup schedule
```

---

## Secrets to create in Infisical (before deploy)

| Path | Description |
|---|---|
| `/qbittorrent/admin-password` | qBittorrent WebUI password |
| `/prowlarr/api-key` | Prowlarr API key (set after first boot) |
| `/lidarr/api-key` | Lidarr API key (set after first boot) |
| `/readarr/api-key` | Readarr API key (set after first boot) |
| `/kavita/jwt-token` | Kavita JWT secret |

---

## Infisical Secret Keys for restic (already exist, shared)

| Path | Description |
|---|---|
| `/volsync/restic-password` | Shared restic encryption password |
| `/hetzner/revachol-bucket/ACCESS_KEY` | Hetzner S3 access key |
| `/hetzner/revachol-bucket/ACCESS_SECRET_KEY` | Hetzner S3 secret key |

---

## Build Order

1. **qBittorrent** — standalone, no dependencies
2. **Prowlarr** — standalone, configure indexers via UI after deploy
3. **Lidarr** — depends on qBittorrent + Prowlarr (configured via UI)
4. **Readarr** — depends on qBittorrent + Prowlarr (configured via UI)
5. **Kavita** — standalone, point at books NFS path

### Pre-deploy checklist
- [ ] Create `downloads` NFS share on dolores.home
- [ ] Confirm/create `books` NFS share on dolores.home (or migrate from legacy server)
- [ ] Note exact NFS paths for both (needed in deployment.yaml)
- [ ] Create Infisical secrets (qBittorrent password, Kavita JWT)

---

## Calibre Migration

Kavita does not use Calibre's `.db` format — it scans the book folder directly. Migration steps:
1. Copy book files from legacy Calibre library to the `books` NFS share on dolores.home
2. Kavita scans and builds its own metadata index on first run
3. Old Calibre-Web instance can be retired

No database migration required.
