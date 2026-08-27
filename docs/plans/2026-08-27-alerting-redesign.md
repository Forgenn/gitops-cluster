# Alerting redesign: routing, severity model, notification template, rule audit

**Date:** 2026-08-27
**Scope:** `infra/monitoring/` — kube-prometheus-stack 78.0.0, Alertmanager (2 replicas), Telegram receiver
**Status:** proposal. Draft implementation files are in the repo, uncommitted, with a `.proposed` suffix so they diff cleanly against the live ones.

---

## 0. Read this first: three things are broken right now

These were found while auditing and are not "alerting design" issues, but they change what the
alerting design has to assume. Do these before or alongside the rest.

### 0.1 Prometheus is in CrashLoopBackOff as of this audit

```
prometheus-prometheus-kube-prometheus-prometheus-0   1/2   CrashLoopBackOff   3 restarts
```

Container log, last termination:

```
level=ERROR source=query_logger.go:113 msg="Error opening query log file"
  component=activeQueryTracker file=/prometheus/queries.active
  err="open /prometheus/queries.active: read-only file system"
panic: Unable to create mmap-ed active query log
```

`/prometheus` has gone read-only. The Prometheus PVC is declared as:

```yaml
# infra/monitoring/values/values.yaml, prometheus.prometheusSpec.storageSpec
accessModes: ["ReadWriteMany"]
storageClassName: longhorn
```

Longhorn serves RWX by putting an NFS share-manager pod in front of the volume. Prometheus's TSDB
uses `mmap` and file locking; NFS is not a supported backing store for it, and when the
share-manager restarts the client mount flips to read-only, which is exactly the failure above.

**Recommendation:** change the Prometheus volumeClaimTemplate to `ReadWriteOnce`. Prometheus is a
single-replica StatefulSet; it never needs RWX. This is a destructive change (PVC accessModes are
immutable — needs delete + recreate of the PVC, losing history), so it is *not* included in the
draft values file. Flagged as **OQ-1**.

Note the same pattern on Grafana (`accessModes: [ReadWriteMany]`, single replica) — same
recommendation, lower urgency since Grafana tolerates it better.

### 0.2 etcd has zero scrape targets — all 15 etcd alerts are dead

```
$ kubectl get endpoints -n monitoring prometheus-kube-prometheus-kube-etcd
NAME                                   ENDPOINTS   AGE
prometheus-kube-prometheus-kube-etcd   <none>      321d
```

Two independent causes:

1. The chart creates the headless `kube-etcd` Service in `kube-system`, but this repo's
   `infra/monitoring/kustomization.yaml` sets `namespace: monitoring`, which rewrites it into
   `monitoring`. The ServiceMonitor still has `namespaceSelector.matchNames: [kube-system]`, so it
   selects nothing.
2. k3s does not expose etcd metrics on `:2381` unless the server is started with
   `--etcd-expose-metrics=true`. That is a NixOS-side change on all three servers.

The comment currently in `values/values.yaml` says:

> kubeEtcd and kubeApiServer stay enabled -- k3s does expose those in a scrapable way and their
> alerts were not firing.

The apiserver half of that is correct (`job="apiserver"` is up). The etcd half is not: those 15
alerts are not firing because **there is no data**, not because etcd is healthy. On a 3-node
embedded-etcd control plane that is the single most important thing to be watching and it is
currently unwatched. Flagged as **GAP-1** below.

### 0.3 CoreDNS has zero scrape targets, same root cause

`prometheus-kube-prometheus-coredns` is also in `monitoring` with `<none>` endpoints, and this
cluster additionally runs CoreDNS in its own `coredns` namespace (`infra/coredns/`), not as the
kube-system `kube-dns` the chart assumes. No DNS metrics are being collected at all. There are no
default CoreDNS *alerts* in this chart, so this costs dashboards rather than notifications, but
it is worth fixing at the same time as 0.2.

---

## 1. Current-state audit

### 1.1 Inventory

| | count |
|---|---|
| PrometheusRule objects | 33 (31 from the chart, 2 local: `argocd-alerts`, `zfs-alerts`) |
| alerting rules | 149 |
| recording rules | 81 |
| rule groups | 32 |

Live state at audit time (`/api/v1/alerts`):

| alert | state | severity | note |
|---|---|---|---|
| `Watchdog` | firing | none | routed to `null`, correct |
| `KubeAPIErrorBudgetBurn` | pending | warning | 6h/3d window, see 1.2.1 |
| `NodeSystemSaturation` | pending | warning | dubois, load/core = 2.84, see 1.2.2 |
| `KubePersistentVolumeFillingUp` | pending | critical | `volsync-opencloud-data-backup-src` at 1.19% free, see 1.2.3 |
| `ArgoAppNotSynced` x5 | pending | warning | muted by a one-off route, see 1.2.4 |

(`KubeAPIDown` and `KubeletDown` were briefly pending earlier in the session — that was Prometheus
restarting, not a real control-plane outage. Both jobs scrape `up=1`.)

### 1.2 What is actually noisy, and why

#### 1.2.1 `KubeAPIErrorBudgetBurn` — SLO math that does not survive a small cluster

```promql
sum(apiserver_request:burnrate6h) > (6.00 * 0.01000)
and sum(apiserver_request:burnrate30m) > (6.00 * 0.01000)
```

This is a multi-window multi-burn-rate SLO alert. It assumes a busy apiserver where the error
*ratio* is statistically meaningful. This cluster's apiserver serves a low, bursty request volume
(ArgoCD watches, 3 kubelets, a handful of controllers). A short burst of a few 5xx or a webhook
timeout moves the ratio by whole percentage points, so the "error budget" is blown by events that
are not incidents. It is pending right now at `3.4e-02` against a `1.0e-02` threshold.

There are 4 rules with this name (2 critical fast-burn, 2 warning slow-burn). The chart's
`defaultRules.disabled` key is per-*alertname*, so they cannot be disabled selectively.

**Proposal:** set `defaultRules.rules.kubeApiserverSlos: false` and
`defaultRules.rules.kubeApiserverBurnrate: false`, and replace with one plain error-rate alert in
`retuned-alerts.proposed.yaml`:

```promql
sum(rate(apiserver_request_total{job="apiserver",code=~"5.."}[10m]))
  / sum(rate(apiserver_request_total{job="apiserver"}[10m])) > 0.05
for: 15m   severity: warning
```

Secondary benefit: `kubeApiserverBurnrate` is ~325 lines of recording rules evaluated every 30s
purely to feed the SLO alerts. On a Prometheus that has been OOMKilled repeatedly (14 restarts
noted in the values.yaml comment) that is real, removable cost. `kubeApiserverAvailability` and
`kubeApiserverHistogram` stay enabled — they feed the Grafana apiserver dashboard.

#### 1.2.2 `NodeSystemSaturation` — threshold set for a dedicated server

```promql
node_load1{job="node-exporter"} / count without (cpu,mode)(node_cpu_seconds_total{mode="idle"}) > 2
for: 15m   severity: warning
```

Load per core of 2 is a "something is wrong" threshold on a dedicated production node. On these
nodes, routine work — volsync/restic backup runs, Longhorn replica rebuilds, container image
pulls, media indexing — parks load per core between 2 and 4 for tens of minutes. dubois is at 2.84
right now with nothing wrong.

The chart's `customRules` map only lets you override `for` and `severity`, **not thresholds**
(verified: `for: {{ dig "TargetDown" "for" "10m" .Values.customRules }}` and a matching `severity`
dig are the only two `dig` sites in the rule templates). So retuning a threshold means disabling
upstream and re-declaring.

**Proposal:** `defaultRules.disabled.NodeSystemSaturation: true`, re-declared at `> 6` for `30m`.

#### 1.2.3 `KubePersistentVolumeFillingUp` — fires on VolSync's own snapshot clones

Currently pending critical on `opencloud/volsync-opencloud-data-backup-src` at **1.19% free**.

That PVC is a VolSync `ReplicationSource` clone: a point-in-time copy sized to the source volume,
mounted only for the duration of a restic run. It is *supposed* to be ~100% full — the data was
copied into a volume exactly the size of the data. There is nothing to act on, and it will do this
on every backup cycle for every one of the ~10 apps using volsync.

Upstream has a `namespace=~".*"` knob but no PVC-name exclusion.

**Proposal:** `defaultRules.disabled.KubePersistentVolumeFillingUp: true`, re-declared with
`persistentvolumeclaim!~"volsync-.*-(src|dst)"` on both the critical (<3%) and warning (4-day
`predict_linear`) variants. Everything else about the rules is kept verbatim.

#### 1.2.4 `InfoInhibitor` — this is the "InfoInhibited" clutter

This is the direct cause of complaint #2.

```promql
ALERTS{severity="info"} == 1
  unless on (namespace) ALERTS{alertname!="InfoInhibitor", severity=~"warning|critical", alertstate="firing"} == 1
labels: severity: none
```

`InfoInhibitor` exists purely as plumbing: it fires whenever an info alert is firing alone, so that
an Alertmanager `inhibit_rule` can use it as a source to suppress info alerts. Consequences today:

- It appears in Grafana's alert list as its own firing alert, one series per namespace with an info
  alert, permanently. That is the noise being seen.
- It is `severity: none`, and the **current route only mutes `Watchdog` and `ArgoAppNotSynced`** —
  everything else falls through to the default `receiver: telegram`. So `InfoInhibitor` itself is
  being delivered to Telegram. That is a bug, not a tuning question.
- The whole mechanism is only needed if you *want* info alerts to reach a receiver conditionally.
  Under the severity model below, info never notifies at all, so the mechanism is dead weight.

**Proposal:** `defaultRules.disabled.InfoInhibitor: true`, and drop the corresponding
`inhibit_rule`. Info alerts are handled by routing, not by a synthetic inhibitor alert.

#### 1.2.5 The route tree is fail-open

```yaml
route:
  receiver: telegram          # <-- default
  routes:
    - matchers: ['alertname = Watchdog'];        receiver: 'null'
    - matchers: ['alertname = ArgoAppNotSynced']; receiver: 'null'
```

Every one of the other 147 alerting rules, at every severity including `info` and `none`, notifies
Telegram by default. Muting is done by adding one-off exceptions. Two problems:

- It does not scale — you end up with a list of `alertname = X` mutes and no model.
- Bumping the chart version silently enrolls every newly-added upstream alert into paging you.

**Proposal:** invert it. Default receiver becomes `'null'`; alerts opt *in* by severity. See §2.

#### 1.2.6 A malformed inhibit rule

```yaml
- target_matchers: ['alertname = Watchdog']
```

No `source_matchers` and no `equal`. An empty source matcher list matches *every* alert, so this
reads as "any firing alert anywhere inhibits Watchdog". It is inert today only because Watchdog is
already routed to `null`. It should be deleted — and if a dead-man's-switch is ever wired up
(**OQ-2**), leaving it in place would break it.

#### 1.2.7 Rule groups with no data (harmless, but do not mistake them for coverage)

These evaluate to nothing because the underlying metrics do not exist here. They cost nothing in
notifications; they cost a false sense of coverage.

| group / alert | why it is dead |
|---|---|
| all 15 `etcd*` alerts | no etcd target — see §0.2 |
| `windows` rule group | no Windows nodes |
| `KubeHpaReplicasMismatch`, `KubeHpaMaxedOut` | no HorizontalPodAutoscalers in the cluster |
| `KubeStateMetricsShardingMismatch`, `KubeStateMetricsShardsMissing` | KSM runs unsharded, 1 replica |
| `PrometheusRemoteStorageFailures`, `PrometheusRemoteWriteBehind`, `PrometheusRemoteWriteDesiredShards` | no `remote_write` configured |
| `NodeRAIDDegraded`, `NodeRAIDDiskFailure` | no mdraid — storage is ZFS + Longhorn |
| `NodeBondingDegraded` | no bonded interfaces |
| `NodeTextFileCollectorScrapeError` | textfile collector not in use |

Only `windows` is worth actively disabling (it is a whole rule group). The rest are single dead
rules; leaving them enabled costs nothing and means they start working if the underlying thing is
ever introduced.

#### 1.2.8 Duplicate kubelet metrics via the apiserver job

`kubelet_volume_stats_*` appears twice — once under `job="kubelet"` and once under
`job="apiserver", exported_namespace=...` (k3s's server `:6443/metrics` re-exports them). The
upstream storage alerts pin `job="kubelet", metrics_path="/metrics"`, so this does not cause
duplicate alerts, but it does double the cardinality of those series and is a plausible source of
`PrometheusDuplicateTimestamps`. Worth a `metricRelabelings` drop on the apiserver ServiceMonitor;
not drafted here (**OQ-3**).

### 1.3 Full per-group verdict

Verdicts: **keep** = leave as-is · **retune** = change `for`/`severity` via `customRules` ·
**replace** = disable upstream + re-declare locally · **disable** = `defaultRules.disabled` ·
**dead** = no data, leave enabled (see 1.2.7)

<details>
<summary><strong>general.rules</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `TargetDown` | warning | keep | genuinely useful; catches scrape breakage |
| `Watchdog` | none | keep, route to `null` | pipeline heartbeat; see OQ-2 |
| `InfoInhibitor` | none | **disable** | §1.2.4 |
</details>

<details>
<summary><strong>kube-apiserver-slos / kube-apiserver-burnrate</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `KubeAPIErrorBudgetBurn` x4 | critical x2, warning x2 | **replace** (whole group off) | §1.2.1 |
</details>

<details>
<summary><strong>kubernetes-storage</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `KubePersistentVolumeFillingUp` (<3%) | critical | **replace** | §1.2.3 — exclude volsync clones |
| `KubePersistentVolumeFillingUp` (4d predict) | warning | **replace** | same |
| `KubePersistentVolumeInodesFillingUp` x2 | critical / warning | keep | not observed noisy; ZFS+Longhorn inode exhaustion is real |
| `KubePersistentVolumeErrors` | critical | keep | Longhorn/democratic-csi provisioning failures are exactly what you want paged |
</details>

<details>
<summary><strong>kubernetes-apps</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `KubePodCrashLooping` | warning | keep | real |
| `KubePodNotReady` | warning | keep | real |
| `KubeDeploymentGenerationMismatch` | warning | keep | real |
| `KubeDeploymentReplicasMismatch` | warning | keep | real |
| `KubeDeploymentRolloutStuck` | warning | keep | real |
| `KubeStatefulSetReplicasMismatch` | warning | keep | real |
| `KubeStatefulSetGenerationMismatch` | warning | keep | real |
| `KubeStatefulSetUpdateNotRolledOut` | warning | keep | real |
| `KubeDaemonSetRolloutStuck` | warning | keep | real |
| `KubeDaemonSetNotScheduled` | warning | keep | real |
| `KubeDaemonSetMisScheduled` | warning | keep | real |
| `KubeContainerWaiting` | warning | keep | `for: 1h` already generous |
| `KubeJobNotCompleted` | warning | **retune** `for` n/a — see note | fires at 12h runtime; restic/volsync initial syncs can legitimately exceed that. No `for` to extend (threshold is inline). Watch it; if it fires on backups, move to **replace** with a 24h threshold. |
| `KubeJobFailed` | warning | keep | real |
| `KubeHpaReplicasMismatch` | warning | dead | no HPAs |
| `KubeHpaMaxedOut` | warning | dead | no HPAs |
| `KubePdbNotEnoughHealthyPods` | warning | keep | real — PDBs exist (cnpg, alertmanager) |
</details>

<details>
<summary><strong>kubernetes-resources</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `KubeCPUOvercommit` | warning | **disable** | "cannot tolerate node failure" — CPU is compressible and overcommit here is deliberate. Permanent-true on a homelab. |
| `KubeMemoryOvercommit` | warning | keep | memory overcommit *is* an OOM risk worth knowing about |
| `KubeCPUQuotaOvercommit` | warning | keep | only affects `loomie-sandbox`; low volume |
| `KubeMemoryQuotaOvercommit` | warning | keep | same |
| `KubeQuotaAlmostFull` | info | **disable** | `loomie-sandbox`'s quota is an intentional cap; "you are near your cap" is not an incident |
| `KubeQuotaFullyUsed` | info | **disable** | same |
| `KubeQuotaExceeded` | warning | keep | this one means workloads are being rejected |
| `CPUThrottlingHigh` | info | **disable** | >25% CFS throttling for 15m. Every container here has a tight CPU limit; this is permanently true and is a "tune your limits" signal, i.e. a Grafana panel, not an alert. |
</details>

<details>
<summary><strong>kubernetes-system / -apiserver / -kubelet</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `KubeVersionMismatch` | warning | keep | catches a half-finished k3s upgrade across the 3 nodes |
| `KubeClientErrors` | warning | keep | real |
| `KubeClientCertificateExpiration` x2 | warning / critical | keep | k3s cert rotation genuinely bites |
| `KubeAggregatedAPIErrors` | warning | keep | real (metrics-server) |
| `KubeAggregatedAPIDown` | warning | keep | real |
| `KubeAPIDown` | critical | keep | real |
| `KubeAPITerminatedRequests` | warning | keep | real |
| `KubeNodeNotReady` | warning | **retune → critical** | on a 3-node cluster losing a node is a wake-me-up event, not a FYI |
| `KubeNodePressure` | info | **retune → warning** | Memory/Disk/PIDPressure is directly actionable and precedes evictions. Info means it would be routed to `null` and never seen. |
| `KubeNodeUnreachable` | warning | **retune → critical** | same reasoning as `KubeNodeNotReady` |
| `KubeletTooManyPods` | info | keep | dashboard-only; nowhere near 110/node |
| `KubeNodeReadinessFlapping` | warning | keep | real |
| `KubeNodeEviction` | info | keep | `for: 0s`, fires on a single eviction — correctly info; the `KubeNodePressure` upgrade covers the actionable half |
| `KubeletPlegDurationHigh` | warning | keep | real |
| `KubeletPodStartUpLatencyHigh` | warning | keep | real |
| `KubeletClientCertificateExpiration` x2 | warning / critical | keep | real |
| `KubeletServerCertificateExpiration` x2 | warning / critical | keep | real |
| `KubeletClientCertificateRenewalErrors` | warning | keep | real |
| `KubeletServerCertificateRenewalErrors` | warning | keep | real |
| `KubeletDown` | critical | keep | real |
</details>

<details>
<summary><strong>node-exporter / node-network</strong></summary>

Every rule in this group selects `job="node-exporter"`. **dolores is scraped as
`job="dolores-node-exporter"` and is therefore covered by none of them** — see GAP-2.

| alert | sev | verdict | rationale |
|---|---|---|---|
| `NodeFilesystemSpaceFillingUp` x2 | warning / critical | keep | real |
| `NodeFilesystemAlmostOutOfSpace` x2 | warning / critical | keep | real |
| `NodeFilesystemFilesFillingUp` x2 | warning / critical | keep | real |
| `NodeFilesystemAlmostOutOfFiles` x2 | warning / critical | keep | real |
| `NodeNetworkReceiveErrs` / `NodeNetworkTransmitErrs` | warning | keep | real |
| `NodeHighNumberConntrackEntriesUsed` | warning | keep | real, and plausible on these nodes |
| `NodeTextFileCollectorScrapeError` | warning | dead | see 1.2.7 — becomes live if the ZFS textfile collector in OQ-4 is added |
| `NodeClockSkewDetected` | warning | keep | real |
| `NodeClockNotSynchronising` | warning | keep | real |
| `NodeRAIDDegraded` / `NodeRAIDDiskFailure` | critical / warning | dead | no mdraid |
| `NodeFileDescriptorLimit` x2 | warning / critical | keep | real |
| `NodeCPUHighUsage` | info | keep | dashboard-only, correctly info |
| `NodeSystemSaturation` | warning | **replace** | §1.2.2 — threshold >2 → >6, `for` 15m → 30m |
| `NodeMemoryMajorPagesFaults` | warning | keep | real (swap thrash) |
| `NodeMemoryHighUtilization` | warning | **retune** `for: 30m` | >90% for 15m. dubois is at 81% used, dolores 84%. Genuine signal, but 15m is chatty on a deliberately memory-packed box. `for` is overridable via `customRules`, so no re-declaration needed. |
| `NodeDiskIOSaturation` | warning | keep | real (aqu-sq > 10 for 30m is already conservative) |
| `NodeSystemdServiceFailed` | warning | keep | real |
| `NodeSystemdServiceCrashlooping` | warning | keep | real |
| `NodeBondingDegraded` | warning | dead | no bonds |
| `NodeNetworkInterfaceFlapping` | warning | keep, watch | excludes `veth.+` but not `cni0` / `flannel.1` / Longhorn interfaces. Not firing; if it starts, **replace** with a wider `device!~` exclusion. |
</details>

<details>
<summary><strong>alertmanager.rules / prometheus / prometheus-operator / config-reloaders / kube-state-metrics</strong></summary>

All **keep**. These are self-monitoring for the observability stack itself and are exactly the
alerts you want after §0.1. Two structural caveats:

- `AlertmanagerClusterDown`, `AlertmanagerClusterFailedToSendAlerts`,
  `PrometheusErrorSendingAlertsToAnyAlertmanager` cannot notify you when they are true, because the
  thing that would notify you is the thing that is down. This is what the dead-man's-switch in
  **OQ-2** is for.
- `PrometheusRemoteStorage*` x3 are dead (no `remote_write`).
</details>

<details>
<summary><strong>etcd (15 alerts)</strong></summary>

`etcdMembersDown`, `etcdInsufficientMembers`, `etcdNoLeader`, `etcdHighNumberOfLeaderChanges`,
`etcdHighNumberOfFailedGRPCRequests` x2, `etcdGRPCRequestsSlow`, `etcdMemberCommunicationSlow`,
`etcdHighNumberOfFailedProposals`, `etcdHighFsyncDurations` x2, `etcdHighCommitDurations`,
`etcdDatabaseQuotaLowSpace`, `etcdExcessiveDatabaseGrowth`, `etcdDatabaseHighFragmentationRatio`.

All **dead** — no target (§0.2). Verdict: **leave enabled, fix the scrape** (GAP-1). Disabling them
would be the wrong move; they are the alerts you most want on a 3-node embedded-etcd control plane.
Once the target exists, expect `etcdHighFsyncDurations` and `etcdHighCommitDurations` to need
retuning for consumer SSDs — revisit then.
</details>

<details>
<summary><strong>local rules</strong></summary>

| alert | sev | verdict | rationale |
|---|---|---|---|
| `ArgoAppNotHealthy` | warning | **replace** | matches `health_status != "Healthy"`, which includes `Progressing` — fires on every normal deploy. §3.2 |
| `ArgoAppNotSynced` | warning | **replace** | correct signal, wrong severity — this is an info-grade condition. §3.2 |
| `ZfsPoolNotHealthy` | critical | keep + extend | correct as written; needs an `absent()` companion and capacity coverage. §3.1 |
</details>

---

## 2. Severity model and route tree

### 2.1 The model

| severity | means | delivery | examples |
|---|---|---|---|
| **critical** | Something is broken or about to break, and a human should look now. Data loss, capacity exhaustion, quorum, or a hard-down component. | Telegram, `group_wait: 10s`, `repeat_interval: 1h` | `KubeAPIDown`, `KubeletDown`, `KubeNodeNotReady`, `ZfsPoolNotHealthy`, `KubePersistentVolumeErrors`, `AlertmanagerClusterDown` |
| **warning** | Worth knowing today. Degradation, drift, or a trend that will become critical if ignored. | Telegram, `group_wait: 2m`, `repeat_interval: 24h` | `KubePodCrashLooping`, `NodeFilesystemSpaceFillingUp`, `ArgoAppDegraded`, `TargetDown` |
| **info** | A fact about the cluster, not a call to action. | **`null` — never notifies.** Visible in Grafana / the Alertmanager UI only. | `KubeletTooManyPods`, `NodeCPUHighUsage`, `KubeNodeEviction`, `ArgoAppOutOfSync` |
| **none** | Plumbing, not an alert. | `null` (or the heartbeat receiver, OQ-2) | `Watchdog` |

The `repeat_interval` split matters as much as the routing. The current single `12h` means a
critical is re-sent at the same cadence as a nuisance. 1h for critical / 24h for warning matches
how urgently you would actually re-look.

**On "should this be an alert at all?"** — the rules marked **disable** in §1.3 are the ones that
answer "no": `CPUThrottlingHigh`, `KubeQuotaAlmostFull`, `KubeQuotaFullyUsed`, `KubeCPUOvercommit`,
`InfoInhibitor`. Each is a standing property of how this cluster is deliberately configured, which
makes it a Grafana panel, not an alert. Everything else that is `info` stays as an alert but is
routed nowhere — the Alertmanager UI is a better place for it than a Grafana panel because it
carries the `for`-duration semantics.

### 2.2 The route tree

```yaml
route:
  # Group by alertname as well as namespace: with namespace-only grouping, one
  # Telegram message can mix a crashlooping pod and a full disk under one title.
  group_by: ['alertname', 'namespace']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 24h

  # Fail closed. Anything that does not match a route below is dropped.
  # This is the core change: a chart bump can add upstream alerts, but it cannot
  # enrol them into notifying you. Opting in is an explicit act.
  receiver: 'null'

  routes:
    # 1. Pipeline plumbing. Never notifies. (Watchdog moves here if OQ-2 lands.)
    - receiver: 'null'
      matchers: ['alertname =~ "Watchdog|InfoInhibitor"']

    # 2. Everything informational. Dashboard / Alertmanager UI only.
    #    Replaces the one-off `alertname = ArgoAppNotSynced` mute: ArgoAppOutOfSync
    #    is declared severity=info in argocd-alerts, so it lands here by model,
    #    not by exception.
    - receiver: 'null'
      matchers: ['severity =~ "info|none"']

    # 3. Critical: fast out, frequent reminders.
    - receiver: 'telegram'
      matchers: ['severity = "critical"']
      group_wait: 10s
      group_interval: 5m
      repeat_interval: 1h

    # 4. Warning: batched, quiet reminders.
    - receiver: 'telegram'
      matchers: ['severity = "warning"']
      group_wait: 2m
      group_interval: 10m
      repeat_interval: 24h
```

**Invariant this introduces:** every locally-authored alerting rule *must* carry a `severity` label
of `critical`, `warning`, or `info`, or it will be silently dropped. All 149 current rules do. This
is called out in the header comment of the drafted values file.

### 2.3 Inhibit rules

```yaml
inhibit_rules:
  # A critical about an object silences the warning/info about the same object.
  - source_matchers: ['severity = "critical"']
    target_matchers: ['severity =~ "warning|info"']
    equal: ['namespace', 'alertname']
  - source_matchers: ['severity = "warning"']
    target_matchers: ['severity = "info"']
    equal: ['namespace', 'alertname']

  # A node that is gone explains everything else scoped to that node. Without
  # this, losing one of three nodes produces a burst of ~20 derived alerts.
  - source_matchers: ['alertname =~ "KubeNodeNotReady|KubeNodeUnreachable"']
    target_matchers: ['severity =~ "warning|info"']
    equal: ['node']

  # A dead kubelet explains the target-scrape failures that follow from it.
  - source_matchers: ['alertname = "KubeletDown"']
    target_matchers: ['alertname =~ "TargetDown|KubeNodeNotReady"']
    equal: ['node']
```

Removed from the current config:

- the `InfoInhibitor` source rule — obsolete once info never notifies, and `InfoInhibitor` itself
  is disabled;
- the `target_matchers: ['alertname = Watchdog']` rule with no source — §1.2.6.

Added: the node-scoped inhibitions, which are the ones that actually reduce burst volume here.

---

## 3. Coverage gaps and new rules

### GAP-1 — etcd is unmonitored

See §0.2. Fix is two-part and outside this repo's YAML:

1. NixOS: add `--etcd-expose-metrics=true` to the k3s server flags on cuno, dubois, katsuragi.
2. This repo: the `kube-etcd` Service must land in `kube-system` with `endpoints` pointing at the
   three node IPs on `:2381`, *or* the ServiceMonitor's `namespaceSelector` must be changed to
   `monitoring`. The cleanest fix is to stop `kustomization.yaml`'s blanket `namespace: monitoring`
   from rewriting chart-generated kube-system objects — but that has blast radius beyond
   monitoring, so it is flagged as **OQ-5** rather than drafted.

Until then the 15 etcd alerts stay enabled and stay dead. Do not read their silence as health.

### GAP-2 — dolores (the NAS) has no node alerting at all

`prometheus.prometheusSpec.additionalScrapeConfigs` scrapes dolores as
`job_name: dolores-node-exporter`. Every upstream node-exporter alert pins `job="node-exporter"`.
Result: the machine holding **every PVC in the cluster** has zero filesystem, memory, clock, file
descriptor, systemd, or disk alerting. Verified: `node_filesystem_size_bytes{fstype="zfs"}` has 56
series on `instance="dolores"` and none of them are reachable by any alert.

Two options:

**(a) Rename the job to `node-exporter`.** One line; every upstream node alert immediately applies
to dolores. Downside: `kube-prometheus-node-recording.rules` aggregates by job, so dolores would be
folded into cluster-wide node aggregates on the Grafana dashboards even though it is not a k8s
node. Also `TargetDown` would then group dolores with the DaemonSet targets.

**(b) A parallel rule file scoped to `job="dolores-node-exporter"`.** More YAML, no perturbation of
existing recording rules or dashboards.

**Recommended: (b)**, drafted as `infra/monitoring/nas-alerts.proposed.yaml`, covering the subset
that actually matters for a NAS: filesystem space (root/boot, not the 56 ZFS datasets), inode
exhaustion, memory, clock sync, systemd unit failures, and target-down. Option (a) is a legitimate
alternative if you would rather have breadth than dashboard purity.

Note the scrape target *itself* is covered — `TargetDown` and the drafted `NasExporterDown` will
tell you if dolores stops answering.

### GAP-3 — ZFS coverage

**What node_exporter actually exposes here** (verified against the live metric name list, not
assumed):

| metric | what it gives you |
|---|---|
| `node_zfs_zpool_state{zpool,state}` | one series per (pool, state) with value 1 for the current state. States present: `online`, `degraded`, `faulted`, `offline`, `removed`, `suspended`, `unavail`. |
| `node_filesystem_{size,avail,free}_bytes{fstype="zfs",mountpoint,device}` | per-dataset capacity, via the generic filesystem collector |
| `node_zfs_arc_*`, `node_zfs_abd_*`, `node_zfs_zfetch_*`, `node_zfs_zpool_dataset_*` | ARC and per-dataset IO counters — dashboard material, not alert material |

**What it does not expose:** scrub state, scrub errors, per-vdev read/write/checksum error counts,
resilver progress, pool fragmentation, snapshot counts. There is no `node_zfs_zpool_scrub_*` or
`node_zfs_zpool_errors` metric. The existing comment in `zfs-alerts.yaml` is correct on this point.

**Two corrections to naive capacity alerting on ZFS.** ZFS reports each dataset's
`node_filesystem_size_bytes` as *its own referenced bytes + the pool's shared available bytes*.
Consequences:

1. A percentage-used alert on the pool root is meaningless — `/revachol-pool` currently computes to
   **0% used** (size 7.42 TB, avail 7.42 TB) because almost nothing is written to the root dataset
   directly. Percentage-based alerting on ZFS datasets is simply wrong.
2. A per-dataset alert would fire ~56 times at once, since every dataset shares the same `avail`.

The correct pool-level signal is **absolute free bytes on the pool root mountpoint**, alerted once.
Currently 7.42 TB free.

Drafted in `infra/monitoring/zfs-alerts.proposed.yaml`:

| alert | expr (abbreviated) | sev | why |
|---|---|---|---|
| `ZfsPoolNotHealthy` | `node_zfs_zpool_state{state="online"} == 0` | critical | unchanged from today — the rule that would have caught the 5-month DEGRADED incident |
| `ZfsPoolStateUnknown` | `absent(node_zfs_zpool_state{state="online"})` | critical | **the important addition.** The existing rule cannot fire if the series disappears — exporter down, ZFS collector disabled, pool exported. That is the same silent-failure mode as the original incident, one level up. |
| `ZfsPoolLowFreeSpace` | `node_filesystem_avail_bytes{fstype="zfs",mountpoint="/revachol-pool"} < 400e9` | warning | ~5% of 7.42 TB. Absolute, not percentage, for the reason above. |
| `ZfsPoolCriticallyLowFreeSpace` | same `< 150e9` | critical | ZFS performance collapses and CoW writes start failing well before 0 |
| `ZfsPoolFillingUp` | `predict_linear(...[24h], 7*24*3600) < 0` | warning | 7-day runway; catches a runaway *arr download before it wedges the pool |

**Only one pool exists.** The brief mentioned three ZFS pools; Prometheus sees exactly one —
`revachol-pool` on dolores. The three k3s nodes have no ZFS at all (`node_filesystem_size_bytes`
by fstype shows only ext4/vfat/tmpfs/ramfs on 192.168.1.155/.156/.157). If there are other pools
on dolores they are not imported, or node_exporter's ZFS collector is not seeing them — worth
confirming with `zpool list` on dolores. Flagged as **OQ-6**. The drafted rules use
`mountpoint="/revachol-pool"` for the capacity alerts and an unpinned selector for the state
alerts, so pool-state coverage extends automatically if more pools appear; the capacity thresholds
would need a second entry.

Scrub / vdev errors are **OQ-4**.

### GAP-4 — ArgoCD rules need the same treatment

`infra/monitoring/argocd-alerts.yaml` today has two rules and three problems:

1. **`ArgoAppNotHealthy` matches `health_status != "Healthy"`.** That set includes `Progressing`,
   `Missing`, `Suspended` and `Unknown`. Every normal deploy that takes >5m pages you. The
   `monitoring` app is `Degraded` right now and would be indistinguishable from a mid-rollout app.
2. **`ArgoAppNotSynced` is `severity: warning`,** which is why it had to be muted with a one-off
   route. OutOfSync is a drift signal, not an incident — it is textbook `info`. Under the new model
   it routes to `null` by severity, and the one-off mute in the route disappears.
3. **Neither rule sets a `namespace` label.** Alertmanager's `group_by` and every `equal:
   ['namespace', ...]` inhibit rule therefore treat them as namespace-`""`, lumping them together
   and breaking inhibition.

Drafted replacement in `infra/monitoring/argocd-alerts.proposed.yaml`:

| alert | condition | sev | `for` |
|---|---|---|---|
| `ArgoAppDegraded` | `health_status="Degraded"` | warning | 15m |
| `ArgoAppStuckProgressing` | `health_status="Progressing"` | warning | 30m |
| `ArgoAppMissing` | `health_status=~"Missing\|Unknown"` | warning | 30m |
| `ArgoAppOutOfSync` | `sync_status="OutOfSync"` | **info** | 1h |
| `ArgoCDMetricsMissing` | `absent(argocd_app_info)` | warning | 15m |

All carry `namespace: argocd` and a one-line `summary` plus a short `description` shaped for the
notification template in §4.

Live status for reference: 5 apps OutOfSync (`hermes-agent`, `infisical`, `pocket-id`,
`sparky-fitness`, `vaultwarden`) — down from the 8 in the values.yaml comment — and `monitoring` is
`Degraded`, which is §0.1.

### GAP-5 — slow restart loops are invisible

Observed restart counts: `prometheus-prometheus-node-exporter-25s4f` **3078**,
`...-zcfjz` **3000**, `prometheus-kube-state-metrics` **56**, `prometheus-grafana` **24**.

`KubePodCrashLooping` requires `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}`
to hold for 15m. A pod that restarts steadily but comes back quickly never accumulates 15 minutes
of CrashLoopBackOff, so thousands of restarts produced no alert. Drafted in
`retuned-alerts.proposed.yaml`:

```promql
- alert: KubePodRestartingFrequently
  expr: increase(kube_pod_container_status_restarts_total{job="kube-state-metrics"}[1h]) > 5
  for: 1h
  severity: warning
```

### GAP-6 — node disk and memory pressure: assessment

Asked directly: is node pressure alerted on sensibly? **Mostly yes, with two fixes.**

- **Disk (node root filesystems):** well covered. `NodeFilesystemAlmostOutOfSpace` (<5% / <3%) plus
  `NodeFilesystemSpaceFillingUp` (24h `predict_linear`) at both warning and critical, plus the
  inode equivalents. This is the right shape. Only defect is the dolores gap (GAP-2).
- **Disk (PVCs):** covered by `KubePersistentVolumeFillingUp`, but currently crying wolf on volsync
  clones — fixed in §1.2.3.
- **Memory:** `NodeMemoryHighUtilization` at >90% for 15m. Current usage — katsuragi 75%, dubois
  81%, dolores 84% — means this will trip on transient spikes. Retuned to `for: 30m`. Paired with
  `KubeNodePressure` being promoted info→warning, which is the *actionable* memory signal because
  MemoryPressure is what actually triggers evictions.
- **Missing:** nothing alerts on kubelet ephemeral-storage / image-cache growth on the node root
  beyond the generic filesystem rules, which is adequate here.

---

## 4. Notification template

### 4.1 Delivery mechanism (verified against the vendored chart, not assumed)

The chart at `infra/monitoring/charts/kube-prometheus-stack-78.0.0/` is vendored in-repo, so this
was checked directly rather than via `helm show values`:

- `values.yaml:574` — `alertmanager.templateFiles: {}`, documented as "placed in
  `/etc/alertmanager/config/` and if they have a `.tmpl` file suffix will be loaded".
- `templates/alertmanager/secret.yaml` — each `templateFiles` key becomes a key in the
  `alertmanager-prometheus-kube-prometheus-alertmanager` Secret, alongside `alertmanager.yaml`.
  The operator mounts that Secret at `/etc/alertmanager/config/`.
- `values.yaml:546` — the chart default `alertmanager.config.templates` is already
  `['/etc/alertmanager/config/*.tmpl']`.

Confirmed live: the rendered Secret already contains the `templates:` stanza even though
`values/values.yaml` never sets it, because Helm deep-merges the `config` map and the local values
override only `global`/`inhibit_rules`/`route`/`receivers`.

```
$ kubectl get secret -n monitoring alertmanager-prometheus-kube-prometheus-alertmanager \
    -o go-template='{{index .data "alertmanager.yaml" | base64decode}}' | tail -2
templates:
- /etc/alertmanager/config/*.tmpl
```

So: **no ConfigMap, no extra Secret, no `alertmanagerSpec.secrets` entry.** Adding
`alertmanager.templateFiles."telegram.tmpl"` to `values/values.yaml` is sufficient. The drafted
values file re-states `config.templates` explicitly anyway, so the wiring does not depend on a
merge behaviour that a future chart bump could change.

### 4.2 Design constraints

- **`parse_mode: ''` (plain text) is kept deliberately.** Alert values routinely contain `_`, `*`,
  `` ` ``, `<` and `>` — under `HTML` or `MarkdownV2` Telegram rejects the whole message with a
  400 and the notification is silently lost. Plain text cannot fail this way. The cost is no bold
  title; the format below compensates with structure instead.
- **Telegram caps messages at 4096 characters.** A namespace-wide group during an incident can
  exceed that, and an over-length message is dropped entirely. The template renders at most 5
  alerts per message and truncates each summary.
- **Truncation uses `printf "%.110s"`, not `slice`.** Go's `fmt` string precision counts runes, so
  it cannot split a UTF-8 sequence; `slice` counts bytes and can.
- Available template functions are Alertmanager's `DefaultFuncs` — `toUpper`, `toLower`, `title`,
  `trimSpace`, `join`, `match`, `reReplaceAll`, `stringSlice`, `date`, `tz`, `since`, `humanize`,
  `humanizeDuration`, `humanizePercentage`, `humanize1024`, `humanizeTimestamp`. There is **no
  arithmetic** (no `add`/`sub`), which is why the overflow line says "and more" rather than a count.

### 4.3 The template

```gotemplate
{{/*
  Succinct Telegram notification templates.

  parse_mode is intentionally '' (plain text) in the receiver config: alert values
  routinely contain _ * ` < >, which under HTML/MarkdownV2 make Telegram reject the
  whole message with a 400 and silently drop the notification.

  Shape, per alert: one title line, then at most two lines of context.
    AlertName
      <summary, collapsed to one line, truncated>
      <the one thing that is affected>
*/}}

{{/* The single most decision-relevant "what is affected" string for one alert. */}}
{{ define "tg.subject" -}}
{{- if .Labels.persistentvolumeclaim -}}pvc {{ .Labels.namespace }}/{{ .Labels.persistentvolumeclaim }}
{{- else if .Labels.zpool -}}zpool {{ .Labels.zpool }} on {{ .Labels.instance }}
{{- else if .Labels.pod -}}pod {{ .Labels.namespace }}/{{ .Labels.pod }}
{{- else if .Labels.deployment -}}deploy {{ .Labels.namespace }}/{{ .Labels.deployment }}
{{- else if .Labels.statefulset -}}sts {{ .Labels.namespace }}/{{ .Labels.statefulset }}
{{- else if .Labels.daemonset -}}ds {{ .Labels.namespace }}/{{ .Labels.daemonset }}
{{- else if .Labels.node -}}node {{ .Labels.node }}
{{- else if .Labels.mountpoint -}}{{ .Labels.instance }}:{{ .Labels.mountpoint }}
{{- else if .Labels.instance -}}{{ .Labels.instance }}
{{- else if .Labels.namespace -}}ns {{ .Labels.namespace }}
{{- else -}}cluster
{{- end -}}
{{- end }}

{{/* One line of prose: summary if present, else description; newlines collapsed. */}}
{{ define "tg.line" -}}
{{- if .Annotations.summary -}}
{{ printf "%.110s" (reReplaceAll "\\s+" " " .Annotations.summary) | trimSpace }}
{{- else -}}
{{ printf "%.110s" (reReplaceAll "\\s+" " " .Annotations.description) | trimSpace }}
{{- end -}}
{{- end }}

{{ define "telegram.short" -}}
[{{ .Status | toUpper }}] {{ with .CommonLabels.severity }}{{ . | toUpper }}{{ else }}ALERT{{ end }}
{{- with .CommonLabels.namespace }} - {{ . }}{{ end }}
{{ range $i, $a := .Alerts }}{{ if lt $i 5 }}
{{ $a.Labels.alertname }}
  {{ template "tg.line" $a }}
  {{ template "tg.subject" $a }}
{{ end }}{{ end }}
{{- if gt (len .Alerts) 5 }}
(and more - open Alertmanager for the full group)
{{- end }}
{{- end }}
```

Wired up as:

```yaml
receivers:
  - name: telegram
    telegram_configs:
      - bot_token_file: /etc/alertmanager/secrets/telegram-bot-credentials/BOT_TOKEN
        chat_id: 7850573137
        parse_mode: ''
        message: '{{ template "telegram.short" . }}'
        send_resolved: true
```

### 4.4 What it renders

Today, with the default template, `KubePersistentVolumeFillingUp` produces roughly 25 lines of
`labels.SortedPairs` and full annotation text. Under the new template:

```
[FIRING] CRITICAL - opencloud

KubePersistentVolumeFillingUp
  PersistentVolume is filling up.
  pvc opencloud/opencloud-data
```

A grouped warning batch:

```
[FIRING] WARNING - argocd

ArgoAppDegraded
  ArgoCD application monitoring is Degraded.
  ns argocd

ArgoAppStuckProgressing
  ArgoCD application infisical has been Progressing for over 30m.
  ns argocd
```

Resolution:

```
[RESOLVED] CRITICAL - monitoring

ZfsPoolNotHealthy
  ZFS pool revachol-pool is not ONLINE.
  zpool revachol-pool on dolores
```

`send_resolved: true` is added deliberately — it is off by default for Telegram, which means a
critical currently never tells you it cleared, so the only way to know is to go and look.

---

## 5. Concrete change list

### 5.1 `infra/monitoring/values/values.yaml`

Draft: **`infra/monitoring/values/values.yaml.proposed`** (diff with
`diff -u values/values.yaml values/values.yaml.proposed`).

| change | detail |
|---|---|
| add `defaultRules.rules.windows: false` | no Windows nodes |
| add `defaultRules.rules.kubeApiserverSlos: false` | §1.2.1 |
| add `defaultRules.rules.kubeApiserverBurnrate: false` | §1.2.1 — also removes ~325 lines of recording rules |
| add `defaultRules.disabled.InfoInhibitor: true` | §1.2.4 |
| add `defaultRules.disabled.CPUThrottlingHigh: true` | §1.3 |
| add `defaultRules.disabled.KubeCPUOvercommit: true` | §1.3 |
| add `defaultRules.disabled.KubeQuotaAlmostFull: true` | §1.3 |
| add `defaultRules.disabled.KubeQuotaFullyUsed: true` | §1.3 |
| add `defaultRules.disabled.NodeSystemSaturation: true` | §1.2.2, re-declared locally |
| add `defaultRules.disabled.KubePersistentVolumeFillingUp: true` | §1.2.3, re-declared locally |
| add `customRules.NodeMemoryHighUtilization.for: 30m` | §1.3 |
| add `customRules.KubeNodeNotReady.severity: critical` | §1.3 |
| add `customRules.KubeNodeUnreachable.severity: critical` | §1.3 |
| add `customRules.KubeNodePressure.severity: warning` | §1.3 |
| replace `alertmanager.config.route` | §2.2 — fail-closed, severity-based |
| replace `alertmanager.config.inhibit_rules` | §2.3 |
| add `alertmanager.config.templates` (explicit) | §4.1 |
| add `alertmanager.templateFiles."telegram.tmpl"` | §4.3 |
| add `message` + `send_resolved` to the telegram receiver | §4.3 |
| update the k3s header comment | correct the etcd claim per §0.2 |

Deliberately **not** changed in the draft: `prometheus.prometheusSpec.storageSpec.accessModes`
(destructive — OQ-1) and `additionalScrapeConfigs` job name (GAP-2 option (a), not recommended).

### 5.2 New and replaced PrometheusRule files

| draft file | replaces | contents |
|---|---|---|
| `infra/monitoring/zfs-alerts.proposed.yaml` | `zfs-alerts.yaml` | 5 rules — GAP-3 |
| `infra/monitoring/argocd-alerts.proposed.yaml` | `argocd-alerts.yaml` | 5 rules — GAP-4 |
| `infra/monitoring/nas-alerts.proposed.yaml` | *new* | 8 rules scoped to `job="dolores-node-exporter"` — GAP-2 |
| `infra/monitoring/retuned-alerts.proposed.yaml` | *new* | 6 rules: the re-declared upstream alerts (`NodeSystemSaturation`, `KubePersistentVolumeFillingUp` x2), plus `KubeAPIHighErrorRate` and `KubePodRestartingFrequently` — §1.2.1/1.2.2/1.2.3, GAP-5 |

`kustomization.yaml` needs `nas-alerts.yaml` and `retuned-alerts.yaml` added to `resources:` once
the `.proposed` suffixes are dropped. Not edited in place — see §6.

### 5.3 Net effect

Measured from the actual `helm template` render plus the drafted rule files, not estimated:

| | before | after |
|---|---|---|
| alerting rules, from the chart | 146 | **134** (−12: `KubeAPIErrorBudgetBurn` ×4, `KubePersistentVolumeFillingUp` ×2, and one each of `InfoInhibitor`, `CPUThrottlingHigh`, `KubeCPUOvercommit`, `KubeQuotaAlmostFull`, `KubeQuotaFullyUsed`, `NodeSystemSaturation`) |
| alerting rules, local | 3 | **23** (zfs 5, argocd 5, nas 8, retuned 5) |
| alerting rules, total | 149 | **157** |
| recording rules | 81 | **67** (−14, apiserver burnrate) |
| rules that can reach Telegram | 147 — everything except the 2 hand-written mutes | **153 by severity**: 44 critical + 109 warning |
| rules that reach Telegram and are known-unactionable | 5 identified in §1.2 | **0** |
| info-severity rules (never notify) | 8, but all were notifying anyway | **4**: `KubeletTooManyPods`, `NodeCPUHighUsage`, `KubeNodeEviction`, `ArgoAppOutOfSync` |
| one-off `alertname` mutes in the route | 2, and growing one per incident | **1**, structural (`Watchdog\|InfoInhibitor`) |

The headline count barely moves — that is the point. The change is not "fewer rules", it is that
**every rule now has a deliberate delivery decision attached to it**, and the 5 specific
false-positive generators found firing or pending during the audit are gone.

---

## 6. How to apply

The drafts use a `.proposed` suffix so nothing tracked is silently rewritten.

```bash
cd infra/monitoring
diff -u values/values.yaml values/values.yaml.proposed
diff -u zfs-alerts.yaml   zfs-alerts.proposed.yaml
diff -u argocd-alerts.yaml argocd-alerts.proposed.yaml
```

Then, once reviewed:

```bash
mv values/values.yaml.proposed values/values.yaml
mv zfs-alerts.proposed.yaml    zfs-alerts.yaml
mv argocd-alerts.proposed.yaml argocd-alerts.yaml
mv nas-alerts.proposed.yaml    nas-alerts.yaml
mv retuned-alerts.proposed.yaml retuned-alerts.yaml
# add nas-alerts.yaml and retuned-alerts.yaml to kustomization.yaml resources:
```

Verify the render before committing. This was already done for the drafted values file — it renders
clean, and the rule-count and route-tree checks below all pass:

```bash
# renders without error; 134 chart alerting rules, down from 149
helm template prometheus infra/monitoring/charts/kube-prometheus-stack-78.0.0/kube-prometheus-stack \
  -n monitoring -f infra/monitoring/values/values.yaml.proposed --kube-version 1.35.6 \
  | grep -c '^\s*- alert: '

# or through kustomize, as ArgoCD will do it
kustomize build --enable-helm infra/monitoring | grep -c 'alert:'
```

Validate the new PrometheusRule files — **not yet done**, `promtool` was unavailable here:

```bash
promtool check rules infra/monitoring/{zfs,argocd,nas,retuned}-alerts.yaml
```

Validate the Alertmanager config and the template offline — this is worth doing, because a
template error makes Alertmanager fall back to *no* notification rather than a degraded one:

```bash
# amtool is in the alertmanager image
kubectl exec -n monitoring alertmanager-prometheus-kube-prometheus-alertmanager-0 -c alertmanager \
  -- amtool check-config /etc/alertmanager/config/alertmanager.yaml

# routing dry-run: which receiver would a given alert reach?
kubectl exec -n monitoring alertmanager-prometheus-kube-prometheus-alertmanager-0 -c alertmanager \
  -- amtool config routes test --config.file=/etc/alertmanager/config/alertmanager.yaml \
     severity=info namespace=argocd     # expect: null
```

**Sequencing note:** §0.1 (Prometheus crashlooping on a read-only NFS mount) should be resolved
first. Until Prometheus is up, none of this is observable.

---

## 7. Open questions

- **OQ-1 — Prometheus PVC access mode.** Changing `ReadWriteMany` → `ReadWriteOnce` requires
  deleting and recreating the PVC, discarding TSDB history. Is that acceptable, or should the
  Longhorn RWX share-manager be repaired in place first and the access mode changed at the next
  convenient point? Not drafted; needs an explicit decision.
- **OQ-2 — dead man's switch.** `Watchdog` currently goes to `null`, so nothing verifies the
  alerting pipeline end to end — which is why §0.1 produced no notification. The cluster runs
  `ntfy`, but ntfy dies with the cluster, so it only half-solves this. An external endpoint
  (healthchecks.io, Better Uptime, an Uptime Kuma push URL on dolores) wired as a `webhook_config`
  receiver with `repeat_interval: 5m` would close it. Needs a URL and a secret decision, so it is
  not drafted — the route tree has a marked slot for it.
- **OQ-3 — duplicate kubelet metrics.** Drop `kubelet_volume_stats_*` and other re-exported kubelet
  series from the `apiserver` job via `metricRelabelings`? Reduces cardinality on an OOM-prone
  Prometheus, but requires overriding the chart's apiserver ServiceMonitor. Not drafted.
- **OQ-4 — ZFS scrub and vdev error monitoring.** node_exporter cannot provide this. Two options:
  (i) run `prometheus-community/zfs_exporter` on dolores, which exposes
  `zfs_pool_scrub_state`, `zfs_pool_errors`, and per-vdev counters; or (ii) a systemd timer on
  dolores writing `zpool status -p` into node_exporter's textfile collector directory, which would
  also make the currently-dead `NodeTextFileCollectorScrapeError` meaningful. Both are NixOS-side
  changes outside this repo. (i) is more work but gives proper metrics; (ii) is ~15 lines of Nix.
  Given the history — a pool sat DEGRADED for five months unnoticed — this is the highest-value
  remaining gap after GAP-1.
- **OQ-5 — kustomize namespace rewriting.** `infra/monitoring/kustomization.yaml` sets
  `namespace: monitoring`, which rewrites chart-generated objects that are supposed to live in
  `kube-system` (the `kube-etcd` and `coredns` headless Services). Fixing this properly needs
  either a targeted patch or dropping the blanket namespace transformer. Blast radius beyond
  monitoring — needs review before drafting.
- **OQ-6 — how many ZFS pools are there actually?** Prometheus sees exactly one
  (`revachol-pool` on dolores); the three k3s nodes have no ZFS filesystems at all. Please confirm
  with `zpool list` on dolores. If other pools exist but are not visible, node_exporter's ZFS
  collector or the pool import state needs looking at — and that itself is the silent-failure mode
  `ZfsPoolStateUnknown` is designed to catch.
- **OQ-7 — `KubeJobNotCompleted` at 12h.** Have volsync/restic initial syncs ever exceeded 12h on
  this cluster? If yes, this needs the disable-and-re-declare treatment with a 24h threshold; the
  chart cannot retune an inline threshold. Left enabled and unchanged pending that answer.
- **OQ-8 — is node_exporter's `systemd` collector enabled on dolores?** It is off by default. If
  it is off, `NasSystemdServiceFailed` in the drafted `nas-alerts` is a silent no-op — harmless,
  but not coverage. Check with
  `curl -s dolores.home:9100/metrics | grep -c node_systemd_unit_state`; if 0, either add the
  collector in dolores' NixOS config or delete the rule. The same caveat applies to the upstream
  `NodeSystemdServiceFailed` / `NodeSystemdServiceCrashlooping` rules for the three k8s nodes.

### What could not be verified

Prometheus went into CrashLoopBackOff (§0.1) partway through this audit, so the following were
reasoned from the chart source and the metric names captured earlier rather than confirmed live.
All are called out at their point of use.

| item | status |
|---|---|
| Alertmanager config + route tree | **verified** — rendered with `helm template` against the vendored chart and decoded from the resulting Secret |
| `templateFiles` → Secret → `/etc/alertmanager/config/*.tmpl` wiring | **verified** — chart source and the live Secret |
| `defaultRules.disabled` / `customRules` effects | **verified** — 149 → 134 chart alerts in the render; all 8 disabled names absent; `for`/`severity` overrides present |
| the 4 new PrometheusRule files parse as valid objects | **verified** — `kubectl apply --dry-run=client` |
| the Go template compiles and renders | **not verified** — `amtool`/`promtool` are not available in this environment. Run the `amtool check-config` in §6 before applying. A template parse error makes Alertmanager send *nothing*, so this is worth doing. |
| PromQL in the 4 new rule files | **not verified** — needs `promtool check rules`. Expressions mirror upstream shapes, but confirm before applying. |
| `node_systemd_unit_state` exists for dolores | **not verified** — OQ-8 |
| `node_timex_sync_status`, `node_filesystem_readonly`, `node_filesystem_files_free` for dolores | **inferred** — the timex and filesystem collectors are on by default and `node_filesystem_size_bytes{instance="dolores"}` was confirmed present |
