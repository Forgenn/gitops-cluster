# Restoring Hermes data

Three layers (docs/plans/2026-09-28-loomie-on-hermes.md, decision 12):

| Situation | Use |
|---|---|
| A node died | Nothing: Longhorn's other replicas take over. |
| A database is corrupt, or chats were deleted by mistake | Litestream, point in time (below). 7 days of history. |
| The whole volume is lost, or you need an older state | volsync restic snapshot (`hermes-data`, nightly, Hetzner `revachol/backups/hermes-agent`). |

## Litestream point-in-time restore of one database

Litestream replicates every `*.db` under `/opt/data` to
`s3://revachol/litestream/hermes-agent/<path relative to /opt/data>`.
Restore **by replica URL**: with the `dir:` config, `litestream restore -config … <db path>`
fails with "database not found in config".

1. Stop writers: scale `hermes-agent` to 0. ArgoCD self-heal scales it back, so first
   disable self-heal for the app in the ArgoCD UI, or commit `replicas: 0`.
2. Start a one-off pod mounting `hermes-data` with the `litestream/litestream:0.5.17`
   image, the `hermes-litestream` Secret as env (`envFrom`), running as uid 10000.
3. In it:
   ```
   litestream restore -o /opt/data/<path>.restored [-timestamp 2026-09-28T10:00:00Z] \
     "s3://revachol/litestream/hermes-agent/<path>?endpoint=https://hel1.your-objectstorage.com&region=hel1&forcePathStyle=true"
   ```
4. Check it: `pragma integrity_check` must return `ok`.
5. Move the old file aside (`<path>` → `<path>.bad`, delete its `-wal`/`-shm`), then rename
   `<path>.restored` → `<path>`.
6. Delete the one-off pod, scale Hermes back (revert step 1), and check the gateway log.

## Quick check without stopping anything

From the running pod, restore to a scratch file with a `.sqlite` extension (so the `*.db`
watch never replicates it), compare, delete:
```
kubectl -n hermes exec <pod> -c litestream -- litestream restore -o /opt/data/.litestream/drill.sqlite "s3://revachol/litestream/hermes-agent/state.db?endpoint=https://hel1.your-objectstorage.com&region=hel1&forcePathStyle=true"
```

## Drill

Monthly. Last drill: 2026-09-30, `state.db` live vs restored: integrity `ok`, 127 sessions /
5773 messages on both. Point-in-time (24 h back) not yet possible on that date (replication
started the same day); do it at the next drill.
