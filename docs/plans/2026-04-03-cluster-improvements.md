# Cluster Fix Implementation Plan — 2026-04-03

> 3-node k3s homelab (dubois/cuno/katsuragi, v1.32.7, NixOS, ArgoCD v3.1.8)

---

## Issue 1 — Vaultwarden CNPG replica-2 stuck in WAL recovery loop

### Root Cause

Replica-2 is stuck on **WAL timeline 9** while the primary is on **timeline 13** (after multiple failovers/switchovers). The replica keeps replaying WAL from the archive, reaches the end of timeline 9's WAL, then tries to find `0000000E.history` (timeline 14) in the object store — which doesn't exist. It restarts the recovery loop endlessly, never reaching a state where the startup probe passes.

The logs confirm:
```
"primary server contains no more WAL on requested timeline 9"
"terminating walreceiver process due to administrator command"
WAL file not found: 0000000E.history
```

This is a known CNPG scenario: the replica's local pgdata became stale across too many timeline jumps and can no longer catch up via streaming or archive recovery.

### Fix

**Delete the replica's PVC and let CNPG rebuild it via pg_basebackup from the primary.**

```bash
# 1. Fence replica-2 so CNPG stops managing it
kubectl annotate cluster vaultwarden-cnpg-cluster -n vaultwarden \
  cnpg.io/fencedInstances='["vaultwarden-cnpg-cluster-2"]'

# 2. Delete the pod
kubectl delete pod vaultwarden-cnpg-cluster-2 -n vaultwarden

# 3. Delete the stale PVC
kubectl delete pvc vaultwarden-cnpg-cluster-2 -n vaultwarden

# 4. Remove the fencing annotation — CNPG will recreate the PVC and run pg_basebackup
kubectl annotate cluster vaultwarden-cnpg-cluster -n vaultwarden \
  cnpg.io/fencedInstances-

# 5. Watch until the new replica is streaming
kubectl get pods -n vaultwarden -w
```

**Risk:** Low — the primary (cluster-1) and cluster-3 are healthy. Backups are running (last successful: April 3).

---

## Issue 2 — Immich library backup stalled since March 1st (33 days)

### Root Cause

Someone manually set `trigger.manual: suspend-for-cleanup` on the live ReplicationSource object (via `kubectl edit` or a Volsync CLI). This pauses the schedule permanently — Volsync won't run the cron until `trigger.manual` is changed to a new value or removed.

The **git manifest** (`infra/immich/backup.yaml`) does NOT have this field — it only has `trigger.schedule: "0 8 * * *"`. However, ArgoCD hasn't corrected this because:
1. The immich Application is OutOfSync for other reasons (ExternalSecrets).
2. ArgoCD's self-heal syncs the _full app_, not individual resources, and the sync is stuck on ExternalSecret drift.
3. The `manual` field in the live object is an _extra_ field not in the manifest. ArgoCD's diff sees the manifest as a subset of the live object and doesn't consider additional fields as drift (it uses server-side apply, which merges).

### Fix

**Option A (immediate — kubectl):** Remove the manual trigger from the live object.

```bash
kubectl patch replicationsource immich-library-backup -n immich --type=json \
  -p='[{"op": "remove", "path": "/spec/trigger/manual"}]'
```

Then clear the stale `lastManualSync` status by deleting and letting ArgoCD recreate:
```bash
# Force a fresh sync from ArgoCD
# (Or just trigger it manually once to unstick it)
kubectl patch replicationsource immich-library-backup -n immich --type=merge \
  -p='{"spec":{"trigger":{"manual":"run-now"}}}'
```

**Option B (proper — add safeguard to git):** Make sure the manifest is explicit so this can't happen silently again. The current manifest is already correct. The real fix is resolving the OutOfSync issue (Issue 5) so ArgoCD's self-heal can enforce the declared state.

**Post-fix:** Monitor the first backup run — the last successful one only backed up 78 bytes (6 files), which was the initial seed. The full 100GiB library has never been backed up. Expect the first real run to take hours.

---

## Issue 3 — ~130 completed pods in ArgoCD namespace

### Root Cause

**Two contributing factors:**

1. **ArgoCD Redis-HA haproxy pods:** The redis-ha StatefulSet uses an init container / sidecar health-check pattern where HAProxy pods frequently restart. Each restart leaves a Completed pod behind. Over 297 days, ~100 Completed haproxy pods accumulated.

2. **Old ReplicaSet pods from ArgoCD upgrades:** The upgrade from v3.0.5 → v3.1.8 created new ReplicaSets. The old ReplicaSets are scaled to 0 (`DESIRED: 0`) but their terminated pods remain in `Completed` state. This affects `argocd-server`, `argocd-repo-server`, `argocd-applicationset-controller`, and `argocd-notifications-controller` — each contributing 5-10 ghost pods.

3. **k3s's terminated pod GC threshold** defaults to 12,500 — far too high for a homelab. With only ~80 running pods cluster-wide, the GC never kicks in.

### Fix

**Step 1 — Clean up now:**

```bash
# Delete all completed pods in argocd namespace
kubectl delete pods -n argocd --field-selector=status.phase=Succeeded

# Delete old zero-replica ReplicaSets
kubectl get rs -n argocd --no-headers | awk '$2==0 && $3==0 && $4==0 {print $1}' | \
  xargs kubectl delete rs -n argocd
```

**Step 2 — Prevent recurrence (NixOS k3s config):**

Set `--kube-controller-arg=terminated-pod-gc-threshold=50` in the k3s server configuration. This is in NixOS configuration, not in this GitOps repo.

**Step 3 — Eliminate the root source (see Issue 6):** Switch ArgoCD from redis-ha to single redis to stop the HAProxy pod churn entirely.

---

## Issue 4 — Control plane node (dubois) at 80% memory

### Root Cause

`dubois` (control-plane node) runs 10.3GiB / ~12.8GiB. The breakdown:

| Consumer | Memory | Note |
|----------|--------|------|
| opencloud | 2,387 Mi | Largest app, expected |
| suwayomi | 1,915 Mi | Java/Kotlin app, no `-Xmx` / no k8s limits |
| Longhorn instance-managers (×3) | ~3,400 Mi total | One per node, expected |
| kavita | 1,013 Mi | .NET app, no memory limit |
| prometheus | 907 Mi | Expected for 34 apps |
| infisical (×2 replicas) | 1,271 Mi total | Expected |
| ArgoCD (all components) | ~1,000 Mi total | High due to HA redis + constant sync loops |
| 130 completed pods metadata | ~50-100 Mi | etcd overhead |

The main actionable items are **suwayomi** and **kavita** being unconstrained, and **ArgoCD's HA redis** being unnecessarily heavy.

### Fix

Not a single fix — this is resolved by a combination of:
- **Issue 3**: Clean completed pods (saves etcd/apiserver memory)
- **Issue 5**: Fix OutOfSync loop (stops ArgoCD's 216+ heal retry cycles burning CPU/memory)
- **Issue 6**: Downgrade ArgoCD redis-ha to single redis (saves ~6 pods worth of memory)
- **Issue 8**: Add resource limits (cap suwayomi and kavita)

If still tight after all the above, consider:
- Moving opencloud to `cuno` or `katsuragi` via nodeAffinity (they're at 53% and 43% respectively)
- Reducing ArgoCD replicas: `server: 1`, `repoServer: 1`, `applicationSet: 1` (homelab doesn't need HA ArgoCD)

---

## Issue 5 — 25/34 ArgoCD apps permanently OutOfSync

### Root Cause

**The `ignoreDifferences` jsonPointers are pointing to the wrong paths.** The ApplicationSet has:

```yaml
ignoreDifferences:
  - group: external-secrets.io
    kind: ExternalSecret
    jsonPointers:
      - /spec/conversionStrategy
      - /spec/decodingStrategy
      - /spec/metadataPolicy
```

But the actual defaulted fields ESO injects are at:
```
/spec/data/0/remoteRef/conversionStrategy
/spec/data/0/remoteRef/decodingStrategy
/spec/data/0/remoteRef/metadataPolicy
/spec/data/1/remoteRef/conversionStrategy   (for each data entry)
...
```

Plus two more ESO defaults not covered at all:
```
/spec/refreshInterval        → defaults to "1h"
/spec/target/deletionPolicy  → defaults to "Retain"
```

The jsonPointers at `/spec/conversionStrategy` match nothing because that path doesn't exist in ExternalSecret — the fields are nested inside each `data[*].remoteRef`.

**Additional non-ExternalSecret OutOfSync resources:**
- **StatefulSets** (karakeep, seaweedfs, zot, pocket-id): Kubernetes adds default fields like `persistentVolumeClaimRetentionPolicy`, `podManagementPolicy`, `revisionHistoryLimit` that aren't in the manifests.
- **navidrome BackendTrafficPolicy**: Envoy Gateway adds default status/conditions fields.
- **loomie HTTPRoutes**: Gateway API controller adds `status.parents` conditions.
- **vaultwarden Secret/HTTPRoute**: Likely related to Helm template drift or Envoy status fields.

### Fix

**Option A (recommended) — Use `jqPathExpressions` for wildcards + add missing defaults to manifests:**

Update the ApplicationSet `ignoreDifferences` in the source repo that defines it (not this repo — it's in the infra ApplicationSet definition):

```yaml
ignoreDifferences:
  - group: external-secrets.io
    kind: ExternalSecret
    jqPathExpressions:
      - .spec.data[]?.remoteRef.conversionStrategy
      - .spec.data[]?.remoteRef.decodingStrategy
      - .spec.data[]?.remoteRef.metadataPolicy
      - .spec.dataFrom[]?.extract.conversionStrategy
      - .spec.dataFrom[]?.extract.decodingStrategy
      - .spec.dataFrom[]?.extract.metadataPolicy
      - .spec.refreshInterval
      - .spec.target.deletionPolicy
  - group: apps
    kind: StatefulSet
    jqPathExpressions:
      - .spec.persistentVolumeClaimRetentionPolicy
      - .spec.podManagementPolicy
      - .spec.revisionHistoryLimit
  - group: apps
    kind: Deployment
    jsonPointers:
      - /spec/template/metadata/annotations/updatedAt
    name: infisical-infisical-standalone-infisical
    namespace: infisical
```

**Option B (alternative) — Explicitly set all ESO defaults in every ExternalSecret manifest:**

Add to every ExternalSecret in the repo:
```yaml
spec:
  refreshInterval: 1h
  target:
    deletionPolicy: Retain
    ...
  data:
    - secretKey: foo
      remoteRef:
        key: /path
        conversionStrategy: Default
        decodingStrategy: None
        metadataPolicy: None
```

This eliminates drift entirely but is verbose. Best combined with a kustomize transformer or shared base.

**Recommendation:** Option A for the `ignoreDifferences` fix (handles all apps at once), plus individually fix the few non-ExternalSecret drifts (StatefulSets, BackendTrafficPolicy) by adding the missing default fields to their manifests.

---

## Issue 6 — ArgoCD Redis-HA is overkill for a homelab

### Root Cause

`redis-ha: enabled: true` in `infra/argocd/values/values.yaml` deploys:
- 3 Redis Sentinel pods (StatefulSet `argocd-redis-ha-server`)
- 3 HAProxy pods (Deployment `argocd-redis-ha-haproxy`)
- Associated Services, ConfigMaps, RBAC

This is designed for production ArgoCD with dozens of clusters. For a 3-node homelab with 34 apps, it's pure overhead and is the primary source of completed-pod garbage (Issue 3).

### Fix

Edit `infra/argocd/values/values.yaml`:

```yaml
redis-ha:
  enabled: false
```

ArgoCD will fall back to its built-in single-replica Redis. The old redis-ha StatefulSet and HAProxy Deployment will be pruned by ArgoCD's automated sync (prune is enabled).

**Risk:** During the switchover there will be a brief ArgoCD cache loss (a few minutes of "cold" reconciliation). No impact on running workloads.

**Also consider** reducing ArgoCD replicas since this is a homelab:

```yaml
server:
  replicas: 1    # was 2
repoServer:
  replicas: 1    # was 2
applicationSet:
  replicas: 1    # was 2
```

---

## Issue 7 — Orphaned PVCs from old Infisical Helm chart

### Root Cause

Infisical was originally deployed via a Helm chart that bundled its own PostgreSQL and Redis subcharts. These created PVCs in both the `default` and `infisical` namespaces. Infisical was later migrated to CNPG for PostgreSQL. The old Helm-chart PVCs were never cleaned up.

Affected PVCs (all with no pods referencing them):
- `default/data-postgresql-0` — 8Gi, local-path, 307d
- `default/redis-data-redis-master-0` — 8Gi, local-path, 307d
- `infisical/data-postgresql-0` — 8Gi, local-path, 307d
- `infisical/redis-data-redis-master-0` — 8Gi, local-path, 307d

Also orphaned NetworkPolicies from the old chart:
- `infisical/postgresql`
- `infisical/redis`

### Fix

```bash
# Verify nothing is using them
kubectl get pods -n default -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
# (should be empty)

# Delete orphaned PVCs
kubectl delete pvc data-postgresql-0 redis-data-redis-master-0 -n default
kubectl delete pvc data-postgresql-0 redis-data-redis-master-0 -n infisical

# Delete orphaned NetworkPolicies
kubectl delete networkpolicy postgresql redis -n infisical
```

**Risk:** None — these PVCs are 307 days old with no consumers. The data was migrated to CNPG long ago.

---

## Issue 8 — No resource requests/limits on 7 app containers

### Root Cause

These apps were deployed as raw `Deployment` manifests without resource constraints. Since k3s doesn't enforce `LimitRange` by default, the pods run unbounded.

Current observed usage (from `kubectl top pods`):

| App | Current CPU | Current Memory | Notes |
|-----|------------|----------------|-------|
| suwayomi | 2m | 1,915 Mi | JVM/Kotlin, uncapped heap |
| kavita | 7m | 1,013 Mi | .NET, grows with library size |
| lidarr | 1m | 233 Mi | .NET *arr app |
| readarr | 3m | 159 Mi | .NET *arr app |
| prowlarr | 1m | 147 Mi | .NET *arr app |
| slskd | 18m | 126 Mi | .NET |
| navidrome | not shown | ~64 Mi typical | Go, lightweight |
| qbittorrent | not shown | ~100 Mi typical | C++, spikes during I/O |

### Fix

Add `resources` to each app's `deployment.yaml`. Requests should be close to observed steady-state; limits should allow headroom for spikes:

**`infra/suwayomi/` (values or deployment):**
```yaml
resources:
  requests:
    cpu: 50m
    memory: 1Gi
  limits:
    memory: 2Gi
```

**`infra/kavita/deployment.yaml`:**
```yaml
resources:
  requests:
    cpu: 50m
    memory: 512Mi
  limits:
    memory: 1.5Gi
```

**`infra/lidarr/deployment.yaml`, `infra/readarr/deployment.yaml`, `infra/prowlarr/deployment.yaml`:**
```yaml
resources:
  requests:
    cpu: 50m
    memory: 128Mi
  limits:
    memory: 512Mi
```

**`infra/slskd/deployment.yaml`:**
```yaml
resources:
  requests:
    cpu: 50m
    memory: 128Mi
  limits:
    memory: 256Mi
```

**`infra/navidrome/navidrome-deployment.yaml`:**
```yaml
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    memory: 256Mi
```

**`infra/qbittorrent/deployment.yaml`:**
```yaml
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    memory: 1Gi
```

**Note on CPU limits:** Intentionally omitted. CPU limits cause throttling which hurts latency; CPU requests are sufficient for scheduling. Memory limits are what prevents OOM situations.

---

## Issue 9 — No liveness/readiness probes on raw-deployment apps

### Root Cause

Same as Issue 8 — the raw Deployment manifests were created without probes. Without them:
- Kubernetes routes traffic immediately at pod start (before the app is ready)
- A deadlocked-but-running process is never restarted
- Rolling updates have no way to know the new pod is healthy

### Fix

Add probes to each app's container spec. These are the correct endpoints per app:

**Lidarr / Readarr / Prowlarr** (all *arr apps use the same pattern):
```yaml
readinessProbe:
  httpGet:
    path: /ping
    port: 8989       # lidarr=8686, readarr=8787, prowlarr=9696
  initialDelaySeconds: 10
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /ping
    port: 8989
  initialDelaySeconds: 30
  periodSeconds: 30
  failureThreshold: 5
```

**Kavita:**
```yaml
readinessProbe:
  httpGet:
    path: /api/health
    port: 5000
  initialDelaySeconds: 10
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /api/health
    port: 5000
  initialDelaySeconds: 30
  periodSeconds: 30
```

**Navidrome:**
```yaml
readinessProbe:
  httpGet:
    path: /ping
    port: 4533
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /ping
    port: 4533
  initialDelaySeconds: 15
  periodSeconds: 30
```

**qBittorrent:**
```yaml
readinessProbe:
  httpGet:
    path: /
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /
    port: 8080
  initialDelaySeconds: 30
  periodSeconds: 30
```

**slskd:**
```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 5030
  initialDelaySeconds: 10
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /health
    port: 5030
  initialDelaySeconds: 30
  periodSeconds: 30
```

**ntfy** (already has resource limits, but no probes):
```yaml
readinessProbe:
  httpGet:
    path: /v1/health
    port: 80
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /v1/health
    port: 80
  initialDelaySeconds: 10
  periodSeconds: 30
```

---

## Issue 10 — Volsync backups stuck for lidarr, prowlarr, qbittorrent, readarr

### Root Cause

All four backups ran on March 31st and **failed with `result: Failed`** due to:

```
error: /data/asp/key-*.xml: permission denied
```

(Or for qbittorrent: `/data/qBittorrent/logs/qbittorrent.log: permission denied`)

The Volsync mover pod runs as a **non-root user** (Volsync default) but the *arr apps write ASP.NET data protection keys (`asp/key-*.xml`) as a different UID, making them unreadable by the mover. qBittorrent's log file has the same permission mismatch.

After failure, the ReplicationSource status gets stuck with `reason: SyncInProgress` and never advances `nextSyncTime`, so no further backup attempts are scheduled.

### Fix

**Step 1 — Unstick the controllers** (clear the stuck state):

```bash
for ns in lidarr prowlarr qbittorrent readarr; do
  kubectl patch replicationsource "${ns}-config" -n "$ns" --type=merge \
    -p='{"spec":{"trigger":{"manual":"unstick-'"$(date +%s)"'"}}}'
done
```

Wait for them to run and fail again, then:

**Step 2 — Fix the permission issue** by adding `moverSecurityContext` to each ReplicationSource so the mover runs as root (matching the app's effective UID):

Edit `infra/{lidarr,prowlarr,readarr,qbittorrent}/replicationsource.yaml`:

```yaml
spec:
  restic:
    copyMethod: Direct
    moverSecurityContext:
      runAsUser: 0     # root, to read all files in the PVC
      runAsGroup: 0
    # ... rest of existing config
```

Alternatively, use `copyMethod: Snapshot` (if the storage class supports it) which creates a point-in-time snapshot and mounts it separately, avoiding the permission issue entirely. Longhorn supports snapshots, so this would be:

```yaml
spec:
  restic:
    copyMethod: Snapshot
    storageClassName: longhorn
    volumeSnapshotClassName: longhorn-snapshot
    # ... rest stays the same
```

**Recommendation:** `copyMethod: Snapshot` is cleaner — it doesn't require root access, doesn't need the PVC to support RWX, and takes a consistent point-in-time backup without competing with the running app for file locks.

---

## Issue 11 — `tmp_longhorn_chart/` leftover directory in repo root

### Root Cause

A temporary directory from a Longhorn chart operation that was never cleaned up from git.

### Fix

```bash
git rm -r tmp_longhorn_chart/
git commit -m "chore: remove leftover tmp_longhorn_chart directory"
```

---

## Issue 12 — Navidrome music NFS path uses UUID-based PVC path

### Root Cause

Already documented in README as tech debt. Navidrome and Lidarr mount music from OpenCloud's dynamically-provisioned PVC at:
```
/revachol-pool/k8s-data/main/pvc-a2248f1d-c3a0-4d9a-89f1-e0d42103f27d
```

This is fragile — the path is coupled to a specific PVC UUID from the ZFS CSI provisioner.

### Fix

Blocked on legacy server migration. When ready:

1. Create dedicated ZFS dataset: `zfs create revachol-pool/media/music`
2. Copy data: `rsync -avP /revachol-pool/k8s-data/main/pvc-a2248f1d-.../projects/10042e7c-.../  /revachol-pool/media/music/`
3. Update `infra/navidrome/navidrome-deployment.yaml`:
   ```yaml
   volumes:
     - name: music
       nfs:
         server: dolores.home
         path: /revachol-pool/media/music
         readOnly: true
   ```
   (Remove the `subPath` since data is now at the root.)
4. Update `infra/lidarr/deployment.yaml` similarly.

---

## Execution Order

| Priority | Issue | What | Effort | Risk |
|----------|-------|------|--------|------|
| 1 | #1 | Fix vaultwarden CNPG replica-2 (fence + delete PVC) | 10 min | Low |
| 2 | #2 | Unblock immich backup (patch manual trigger) | 2 min | Low |
| 3 | #3 | Delete completed pods + old ReplicaSets in argocd | 5 min | Low |
| 4 | #7 | Delete orphaned PVCs and NetworkPolicies | 5 min | Low |
| 5 | #11 | Remove `tmp_longhorn_chart/` from git | 1 min | Low |
| 6 | #6 | Switch ArgoCD to single redis + reduce replicas | 1 commit | Medium |
| 7 | #5 | Fix ApplicationSet `ignoreDifferences` (jqPathExpressions) | 1 commit | Medium |
| 8 | #10 | Fix volsync permission errors (moverSecurityContext or Snapshot) | 4 files | Low |
| 9 | #8 | Add resource limits to 7 apps | 7 files | Low |
| 10 | #9 | Add health probes to 7 apps | 7 files | Low |
| 11 | #4 | Re-evaluate memory after 1-10 are done | Observe | — |
| 12 | #12 | Navidrome NFS path migration | Blocked | — |
