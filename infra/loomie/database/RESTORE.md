# Loomie DB — Restore Runbook

Continuous WAL + nightly base backups are shipped to Hetzner S3 at
`s3://revachol/backups/loomie-cnpg-db` (60-day retention) by the
`loomie-db` CNPG cluster (see `cluster.yaml`) and the
`loomie-db-backup` ScheduledBackup (08:00 UTC nightly).

This runbook covers the three restore scenarios we care about.

---

## 1. Verify backups exist

```bash
kubectl get backup -n loomie
# → expect at least one recent "completed" Backup CR

# Inspect the backup catalogue stored in S3:
kubectl cnpg status loomie-db -n loomie | grep -A20 "Continuous Backup"
```

If nothing is listed, the WAL archiver is broken — fix that first;
restore is impossible without a recoverable WAL stream.

## 2. Point-in-time recovery (PITR) — most common case

Use when you need to roll the DB back to a moment in time (a bad
deploy, a deletion you want to undo) without destroying the cluster.

1. Pick the target timestamp (UTC):

   ```
   TARGET="2026-05-25T03:45:00Z"
   ```

2. Create a new Cluster bootstrapping from the backup object store.
   **Restore into a NEW cluster name** (`loomie-db-restored`) — do not
   overwrite the live primary until you've validated the data.

   ```yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Cluster
   metadata:
     name: loomie-db-restored
     namespace: loomie
   spec:
     instances: 1
     postgresql:
       shared_preload_libraries:
         - vector
     bootstrap:
       recovery:
         source: loomie-db-archive
         recoveryTarget:
           targetTime: "2026-05-25 03:45:00.000000+00"
     externalClusters:
       - name: loomie-db-archive
         barmanObjectStore:
           endpointURL: "https://hel1.your-objectstorage.com"
           destinationPath: "s3://revachol/backups/loomie-cnpg-db"
           s3Credentials:
             accessKeyId:
               name: hetzner-backup-bucket-credentials
               key: ACCESS_KEY
             secretAccessKey:
               name: hetzner-backup-bucket-credentials
               key: ACCESS_SECRET_KEY
     storage:
       storageClass: longhorn
       size: 10Gi
   ```

3. `kubectl apply -f restore-cluster.yaml`, wait for the new cluster
   to reach `Cluster in healthy state`.

4. Validate by port-forwarding into `loomie-db-restored-rw` and
   running the queries the incident requires (`SELECT COUNT(*)`,
   `\dt`, application smoke).

5. If the data is correct, **promote the restored cluster** — repoint
   the app's `DATABASE_URL` (`loomie-db-app` secret) to the new
   cluster's `-rw` service, then garbage-collect the original cluster
   when you're sure.

## 3. Full cluster wipe + restore from latest base backup

Use when the live cluster is irrecoverable (all 3 instances lost, PVCs
corrupted, etc.).

Same procedure as PITR but **omit `recoveryTarget`** — CNPG will
recover to the end of WAL (i.e. as recent as the last shipped WAL
segment). Then promote as above.

## 4. Quarterly restore drill (CRITICAL — run every 90 days)

A backup you've never restored is not a backup.

1. Pick yesterday 12:00 UTC as the target.
2. Apply the PITR cluster spec above with name `loomie-db-drill`.
3. Wait for healthy state, port-forward, run:

   ```sql
   SELECT COUNT(*) FROM users;
   SELECT MAX(created_at) FROM messages;
   ```

4. Confirm the row count and timestamp are sensible.
5. `kubectl delete cluster loomie-db-drill -n loomie`.
6. Log the drill result in `docs/plans/` (date + duration + any issues).

If the drill fails, **page yourself** — F1 is "shipped" only as long as
restore actually works.
