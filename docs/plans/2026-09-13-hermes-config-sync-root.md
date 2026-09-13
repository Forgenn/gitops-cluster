# Hermes Config Sync (Root) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declare the Hermes root agent's prompt, config and cron jobs in git and deliver them into the agent PVC by a resilient initContainer, restoring the daily cluster-monitor report lost on 2026-09-08.

**Architecture:** `plder` becomes a private monorepo holding agent config under `hermes/`. A `profile-sync` initContainer in `infra/hermes-agent/deployment.yaml` clones it at a pinned SHA, then applies config to `/opt/data`. Cron is merged by an upsert keyed on job id — never overwritten — because Hermes rewrites `jobs.json` every scheduler tick and users create jobs conversationally from Telegram. The sync logic lives in this repo (not plder) because it must run *before* the clone.

**Tech Stack:** Kubernetes (k3s) + Kustomize + ArgoCD; Python 3.13 (the image's `/opt/hermes/.venv`); pytest; External Secrets Operator + Infisical; GitHub deploy keys.

**Spec:** `docs/plans/2026-09-12-hermes-config-as-code.md`

## Global Constraints

- Hermes image is `nousresearch/hermes-agent:v2026.8.19`, hermes_cli 0.20.5. Do not bump it in this plan.
- `HERMES_HOME=/opt/data`. The initContainer must set it explicitly — env is not inherited from the main container.
- The initContainer runs `runAsUser: 10000`. `kubectl exec` lands as uid 0, so any manual in-pod write must be wrapped in `/command/s6-setuidgid hermes` or it leaves root-owned files on the PVC.
- The CLI entrypoint is `/opt/hermes/.venv/bin/hermes`. There is no `hermes_cli/__main__.py`; `python -m hermes_cli` does not work.
- **The initContainer must never fail the pod for a config problem.** Every failure path logs and continues. The agent is the operator's primary interface; a config error must not take it offline.
- User-owned paths are never written by this plan: `memories/`, `sessions/`, `logs/`, `workspace/`, `home/`, `state.db`, `auth.json`, `.env`, `backups/`, `cache/`.
- `plder`'s default branch is `master`, not `main`.
- Commit style in this repo is `component: lowercase description` (e.g. `hermes: add profile-sync initContainer`).

---

### Task 1: Restructure plder into a private monorepo

**Files:**
- Modify (in a plder clone): `package.json` — Pi manifest paths
- Create: `plder/pi/` (moved content), `plder/hermes/root/`
- Test: `plder/.github/workflows/` untouched; manifest paths validated by a script

**Interfaces:**
- Produces: the repo layout every later task writes into — `hermes/root/{SOUL.md,config.yaml,cron/jobs.json}`, and `pi/` holding what used to be at the repo root.

- [ ] **Step 1: Clone plder and record the current Pi manifest**

```bash
cd /c/Users/Pol/projects
git clone git@github.com:Forgenn/plder.git
cd plder
git rev-parse --abbrev-ref HEAD   # expect: master
cat package.json                   # note the "pi" block's paths
```

- [ ] **Step 2: Move Pi content under pi/**

`package.json` stays at the repo root — `pi install` reads it there. Only the directories move.

```bash
mkdir -p pi hermes/root/cron
git mv extensions skills themes docker install.sh init-gh.sh \
       settings.json settings.json.example \
       keybindings.json models.json models.json.example pi/
```

- [ ] **Step 3: Update the Pi manifest paths**

In `package.json`, the `pi` block's three paths gain the `pi/` prefix:

```json
"pi": {
  "extensions": ["./pi/extensions"],
  "skills": ["./pi/skills"],
  "themes": ["./pi/themes"]
}
```

- [ ] **Step 4: Verify every declared path exists**

```bash
python -c "
import json,os,sys
m=json.load(open('package.json'))['pi']
missing=[p for k in ('extensions','skills','themes') for p in m[k] if not os.path.isdir(p)]
print('MISSING:',missing) if missing else print('OK: all pi paths resolve')
sys.exit(1 if missing else 0)
"
```

Expected: `OK: all pi paths resolve`

- [ ] **Step 5: Commit and push**

```bash
git add -A
git commit -m "repo: split into pi/ and hermes/ monorepo layout"
git push origin master
```

- [ ] **Step 6: Make the repo private and verify**

```bash
gh repo edit Forgenn/plder --visibility private --accept-visibility-change-consequences
gh repo view Forgenn/plder --json isPrivate,defaultBranchRef
```

Expected: `{"isPrivate":true,"defaultBranchRef":{"name":"master"}}`

---

### Task 2: Capture live root config into the repo

Capture **before** declaring. If the repo's copy is not the live one, the first deploy silently changes the agent's behaviour.

**Files:**
- Create: `plder/hermes/root/SOUL.md`, `plder/hermes/root/config.yaml`

**Interfaces:**
- Produces: `SOUL.md`, which the sync script copies onto the PVC, and
  `config.yaml`, which it does **not** — see Task 5 Step 3. `config.yaml` is
  captured now anyway so the declaration is complete and correct on the day the
  ConfigMap mount is retired; until then the `hermes-config` ConfigMap in this
  repo remains the one the agent actually reads, and the two must be kept in
  step by hand.

- [ ] **Step 1: Copy the live files out of the pod**

```bash
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
cd /c/Users/Pol/projects/plder
kubectl exec -n hermes $POD -- cat /opt/data/SOUL.md    > hermes/root/SOUL.md
kubectl exec -n hermes $POD -- cat /opt/data/config.yaml > hermes/root/config.yaml
```

- [ ] **Step 2: Verify they are byte-identical to the live pod**

```bash
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
for f in SOUL.md config.yaml; do
  kubectl exec -n hermes $POD -- cat /opt/data/$f | diff - hermes/root/$f \
    && echo "OK: $f identical" || echo "MISMATCH: $f"
done
```

Expected: `OK: SOUL.md identical` and `OK: config.yaml identical`.

Note: root `SOUL.md` is currently byte-identical to the image's vendor default
(`/opt/hermes/docker/SOUL.md`). That is expected and is captured as-is; changing the
root persona is not part of this plan.

- [ ] **Step 3: Add `_config_version` if absent**

The live `config.yaml` comes from a read-only ConfigMap mount and has no
`_config_version`, which is why `[config-migrate] WARNING: This config predates
version 12` appears on every boot. Pin it in the declaration now so the warning
stops on the day the ConfigMap mount is retired and this file becomes the
writable one the agent reads. **It will not stop in this phase** — the sync does
not copy `config.yaml`, so the read-only ConfigMap is still what boots:

```bash
grep -q '^_config_version:' hermes/root/config.yaml || echo '_config_version: 12' >> hermes/root/config.yaml
grep '^_config_version:' hermes/root/config.yaml
```

Expected: `_config_version: 12`

- [ ] **Step 4: Commit**

```bash
git add hermes/root/SOUL.md hermes/root/config.yaml
git commit -m "hermes: capture live root SOUL and config"
git push origin master
```

---

### Task 3: Declare the recovered daily cron job

**Files:**
- Create: `plder/hermes/root/cron/jobs.json`

**Interfaces:**
- Produces: the declared-jobs file read by `cron_upsert.upsert_jobs()` in Task 4.

- [ ] **Step 1: Write the declared job**

Schema fields below were captured from a live `hermes cron create` during the
2026-09-12 spike. `managed_by` is our own marker, used by the upsert to know which
jobs it may retire. Runtime fields are deliberately absent — the upsert fills them.

```json
{
  "jobs": [
    {
      "id": "6270d3f018f2",
      "name": "[bot:monitor] daily exception report",
      "managed_by": "plder",
      "prompt": "You are the \"Cluster Monitor\" agent for this homelab k3s cluster. Run a health check and report ONLY things the user genuinely needs to act on or be aware of. Do NOT report normal/healthy status — silence is the goal when all is well.\n\nACCESS & TOOLS:\n- You run inside the hermes-agent pod in the \"hermes\" namespace. Use kubectl (at /usr/local/bin/kubectl) to query the cluster. You are read-only: you CAN get/list/watch pods, services, PVCs, events, and namespaces across the cluster, and you CAN reach monitoring ClusterIP services directly by resolving their service IPs. You CANNOT delete/patch/create, read secrets, see nodes, or access storageclasses.\n- Prometheus service IP (ClusterIP) in monitoring namespace: prometheus-kube-prometheus-prometheus, port 9090. Alertmanager: prometheus-kube-prometheus-alertmanager, port 9093. Query their HTTP JSON APIs with curl.\n- Both gitops repos are cloned under /opt/data/home/: gitops-cluster and nixos-config. You may read them.\n\nWHAT TO CHECK (in this order):\n1. PROMETHEUS VIEW OF ALERTS — the source of truth. Query http://<prom-svc>:9090/api/v1/alerts and list alerts with state \"firing\" (excluding the \"Watchdog\" meta alert, and excluding anything already silenced). Also note significant \"pending\" alerts that are about to fire. For firing alerts pull the alertname, severity, affected namespace/app, and the annotation summary/description.\n2. ALERTMANAGER — query http://<am-svc>:9093/api/v2/alerts to confirm what alertmanager is actually holding active/suppressed. If Prometheus is DOWN or unreachable, SAY SO PROMINENTLY — that itself is critical: it means no alerts can fire.\n3. STORAGE PRESSURE — check PVCs across all namespaces (kubectl get pvc -A) for anything near-full, and flag KubePersistentVolumeFillingUp alerts. Report a PVC only when it is genuinely a concern (e.g. under ~10% free), not every PVC.\n4. ARGOCD HEALTH/SYNC — check argo app status if reachable (kubectl get applications -n argocd, or via the argocd servicemonitor/alerts). Report apps that are Degraded, OutOfSync, or otherwise broken. Ignore apps that are merely Healthy+Synced.\n5. WORKLOAD ISSUES — kubectl get pods -A: report CrashLoopBackOff, ImagePullBackOff, pods not Ready for a long time, and 0/2+ containers. Ignore normal Running pods.\n6. GITOPS REPO DRIFT — only if quick: note if local clones diverge from origin/main. Skip unless trivial.\n\nREPORTING RULES (CRITICAL):\n- Report ONLY actionable/exceptions. If everything is healthy, send a short one-liner like: \"✅ Cluster healthy — no action needed. Prometheus up, no firing alerts, storage OK.\" and nothing more.\n- Prioritize by severity: critical (storage full, monitoring down, crashlooping) first, then warnings. Put a 🔴/🟡/✅ emoji prefix per finding.\n- For each finding give: what, where (namespace/app), and what the user likely needs to do (one line). Keep it terse and scannable — this is a morning alert, not a report.\n- Never dump every PVC, pod, or alert. Filter to real exceptions.\n- Ignore the watchdog alert and any alerts already marked as muted/suppressed in the rules.\n- If you cannot reach Prometheus or the cluster at all, say so explicitly — do not invent a healthy status.\n\nOutput is the message to send to the user on Telegram. Keep total output under ~30 lines.",
      "skills": [],
      "skill": null,
      "model": null,
      "provider": null,
      "script": null,
      "no_agent": false,
      "monitor_script": null,
      "monitor_url": null,
      "context_from": null,
      "schedule": { "kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *" },
      "schedule_display": "0 9 * * *",
      "repeat": { "times": null, "completed": 0 },
      "enabled": true,
      "deliver": "telegram",
      "workdir": null
    }
  ]
}
```

- [ ] **Step 2: Validate it parses and carries the expected fields**

```bash
python -c "
import json
j=json.load(open('hermes/root/cron/jobs.json'))['jobs'][0]
assert j['id']=='6270d3f018f2', j['id']
assert j['schedule']['expr']=='0 9 * * *', j['schedule']
assert j['managed_by']=='plder'
assert j['deliver']=='telegram'
assert 'Cluster Monitor' in j['prompt']
assert len(j['prompt'])>2000, len(j['prompt'])
print('OK: declared job valid,', len(j['prompt']), 'char prompt')
"
```

Expected: `OK: declared job valid, <n> char prompt` with n > 2000.

`deliver` is `telegram` (not a literal chat id) so the scheduler resolves
`TELEGRAM_HOME_CHANNEL` from env at fire time — the same channel the job used before.

- [ ] **Step 3: Commit**

```bash
git add hermes/root/cron/jobs.json
git commit -m "hermes: restore daily cluster monitor job lost in the 2026-09-08 corruption"
git push origin master
```

---

### Task 4: Cron upsert module (TDD)

This is the task most likely to cause data loss if wrong, so it is built test-first
and lives in this repo, unit-tested on the workstation with plain pytest.

**Files:**
- Create: `infra/hermes-agent/sync/cron_upsert.py`
- Test: `infra/hermes-agent/sync/test_cron_upsert.py`

**Interfaces:**
- Produces: `upsert_jobs(live: dict, declared: list[dict]) -> dict` — takes the parsed live `jobs.json` (`{"jobs": [...]}`) and the declared job list, returns the merged document. Pure function, no I/O. Task 5 calls it.
- Produces: `MANAGED_BY = "plder"` and `RUNTIME_FIELDS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# infra/hermes-agent/sync/test_cron_upsert.py
import pytest
from cron_upsert import upsert_jobs, MANAGED_BY


def _live(*jobs):
    return {"jobs": list(jobs)}


def test_declared_job_is_added_to_empty_store():
    out = upsert_jobs(_live(), [{"id": "a", "name": "A", "managed_by": MANAGED_BY}])
    assert [j["id"] for j in out["jobs"]] == ["a"]


def test_runtime_fields_survive_a_redeclaration():
    live = _live({
        "id": "a", "name": "old", "managed_by": MANAGED_BY,
        "next_run_at": "2026-09-14T09:00:00+00:00",
        "last_run_at": "2026-09-13T09:00:00+00:00",
        "last_status": "completed", "failure_streak": 2,
        "repeat": {"times": None, "completed": 7},
    })
    out = upsert_jobs(live, [{"id": "a", "name": "new", "managed_by": MANAGED_BY,
                              "repeat": {"times": None, "completed": 0}}])
    job = out["jobs"][0]
    assert job["name"] == "new"                                    # declared wins
    assert job["next_run_at"] == "2026-09-14T09:00:00+00:00"       # runtime preserved
    assert job["last_status"] == "completed"
    assert job["failure_streak"] == 2
    assert job["repeat"]["completed"] == 7                         # counter preserved


def test_undeclared_unmanaged_job_is_never_deleted():
    live = _live({"id": "telegram-made", "name": "renew cert"})
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY}])
    assert sorted(j["id"] for j in out["jobs"]) == ["a", "telegram-made"]


def test_previously_managed_job_absent_from_repo_is_retired():
    live = _live({"id": "gone", "managed_by": MANAGED_BY},
                 {"id": "mine", "name": "hand made"})
    out = upsert_jobs(live, [])
    assert [j["id"] for j in out["jobs"]] == ["mine"]


def test_declared_state_overrides_live_state():
    live = _live({"id": "a", "managed_by": MANAGED_BY, "state": "scheduled"})
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY, "state": "paused"}])
    assert out["jobs"][0]["state"] == "paused"


def test_live_state_is_kept_when_declaration_is_silent():
    live = _live({"id": "a", "managed_by": MANAGED_BY, "state": "paused"})
    out = upsert_jobs(live, [{"id": "a", "managed_by": MANAGED_BY}])
    assert out["jobs"][0]["state"] == "paused"


def test_declared_jobs_are_stamped_even_if_the_file_forgot():
    out = upsert_jobs(_live(), [{"id": "a"}])
    assert out["jobs"][0]["managed_by"] == MANAGED_BY


def test_missing_jobs_key_is_tolerated():
    out = upsert_jobs({}, [{"id": "a"}])
    assert [j["id"] for j in out["jobs"]] == ["a"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest test_cron_upsert.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'cron_upsert'`

- [ ] **Step 3: Write the implementation**

The implementation is **not reproduced here.** The shipped module has since
gained conflict detection (`upsert_jobs_with_conflicts`), deep-copying, and an
inverted merge — the version originally drafted in this plan built each job from
the declaration and copied back an allowlist of live fields, which silently
destroyed every field Hermes writes that the repo does not declare.

**Read the shipped files:** `infra/hermes-agent/sync/cron_upsert.py` and its
tests in `infra/hermes-agent/sync/test_cron_upsert.py`.

What the plan pins, and what must stay true of whatever that module contains:

- `upsert_jobs(live, declared) -> dict` stays available as the pure-function
  entry point; `upsert_jobs_with_conflicts(live, declared) -> (dict, list[str])`
  is what `sync.apply_cron` actually calls, so it can log the skipped ids.
- The merge starts from the LIVE job and overlays the declared keys. Never the
  other way round: an allowlist cannot anticipate fields a future image adds.
- Live scheduler state (`RUNTIME_FIELDS`) and live `repeat.completed` always win.
- A declaration silent about `state` leaves the live state alone; one that sets
  it explicitly wins — that is how a job is paused or resumed from git.
- A declared id colliding with a live job that is **not** `managed_by: plder` is
  refused outright: the hand-made job is left untouched and the declaration is
  skipped and reported. Adoption is a deliberate act (delete the live job first).
- A live `managed_by: plder` job with no declaration is retired; any other
  undeclared live job survives.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest test_cron_upsert.py -v
```

Expected: PASS. More tests exist now than this plan was drafted with — take
"0 failed" as the gate, not a number.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent/sync/cron_upsert.py infra/hermes-agent/sync/test_cron_upsert.py
git commit -m "hermes: add cron upsert module for config sync"
```

---

### Task 5: Sync entrypoint script

**Files:**
- Create: `infra/hermes-agent/sync/sync.py`
- Test: `infra/hermes-agent/sync/test_sync.py`

**Interfaces:**
- Consumes: `cron_upsert.upsert_jobs` from Task 4.
- Produces: `should_skip(applied_path, ref, image) -> bool` and `main()`. The container entrypoint is `python /sync/sync.py`.

- [ ] **Step 1: Write the failing test for the skip gate**

The skip gate must key on the image as well as the ref, or an image bump would
never re-sync profile skills.

```python
# infra/hermes-agent/sync/test_sync.py
import json
from pathlib import Path
from sync import should_skip


def _write(tmp_path: Path, ref: str, image: str) -> Path:
    p = tmp_path / "applied"
    p.write_text(json.dumps({"ref": ref, "image": image}))
    return p


def test_skips_when_ref_and_image_both_match(tmp_path):
    p = _write(tmp_path, "abc123", "img:v1")
    assert should_skip(p, "abc123", "img:v1") is True


def test_does_not_skip_when_ref_changed(tmp_path):
    p = _write(tmp_path, "abc123", "img:v1")
    assert should_skip(p, "def456", "img:v1") is False


def test_does_not_skip_when_image_changed(tmp_path):
    p = _write(tmp_path, "abc123", "img:v1")
    assert should_skip(p, "abc123", "img:v2") is False


def test_does_not_skip_when_no_record_exists(tmp_path):
    assert should_skip(tmp_path / "absent", "abc123", "img:v1") is False


def test_corrupt_record_does_not_skip(tmp_path):
    p = tmp_path / "applied"
    p.write_text("{not json")
    assert should_skip(p, "abc123", "img:v1") is False
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest test_sync.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sync'`

- [ ] **Step 3: Write the sync script**

The implementation is **not reproduced here.** It has moved on materially since
this plan was drafted — the skip gate now also requires `result == "ok"`, the
clone has timeouts, every step reports success/failure, the last-good fallback
passes `dirs_exist_ok=True` (without it `/staging`, an emptyDir mount point,
made the whole fallback dead code), `copy_root_files` no longer copies
`config.yaml`, and `sync_skills` is deliberately log-only. A stale listing in a
plan is worse than no listing: it is what a future reader copies.

**Read the shipped file:** `infra/hermes-agent/sync/sync.py`.

What the plan pins, and what must stay true of whatever that file contains:

- `should_skip(applied_path, ref, image) -> bool` and `main() -> int`; the
  container entrypoint is `python /sync/sync.py`.
- `main()` returns 0 on every path. The initContainer must never fail the pod.
- `applied_ref` is what actually *landed* — after a fallback it is the
  *previous* ref, so the next restart does not skip and will retry the new one.
- `config.yaml` is **not** copied onto the PVC in this phase. The main container
  bind-mounts the `hermes-config` ConfigMap read-only over `/opt/data/config.yaml`
  via `subPath`, so a copy there is masked and never read — and the copy target
  is the root-owned kubelet subPath stub, so `copy2` as uid 10000 raises
  PermissionError and pins `result` at `"partial"` forever. `config.yaml` stays
  ConfigMap-owned until this sync is proven in production; retiring that mount
  is tracked under "Out of scope" below, along with the reason it cannot happen
  yet (a soft-failed clone would leave the PVC with no config at all).
- `sync_skills` is log-only and must NOT vote on `result`. `docker/stage2-hook.sh`
  already runs the same command for the root home on every container start with
  `|| warn`. If a transient failure could set `result: "partial"`, `should_skip`
  (which requires `"ok"`) would never fire again and every restart would re-clone
  from GitHub.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest test_sync.py test_cron_upsert.py -v
```

Expected: PASS. The suite has grown well past the count this plan was drafted
with — take "0 failed" as the gate, not a number.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent/sync/sync.py infra/hermes-agent/sync/test_sync.py
git commit -m "hermes: add config sync entrypoint script"
```

---

### Task 6: Deploy key and ESO secret for plder

**Files:**
- Modify: `infra/hermes-agent/secrets/externalsecret.yaml`
- Modify: `infra/hermes-agent/configmap.yaml` — add the `github.com-plder` host alias

**Interfaces:**
- Produces: `PLDER_DEPLOY_KEY` in the `hermes-secrets` Secret, and an SSH host alias the sync script's `REPO_URL` resolves through.

- [ ] **Step 1: Generate a dedicated read-only key**

```bash
ssh-keygen -t ed25519 -C "hermes-plder-sync" -f /tmp/plder_deploy_key -N ""
cat /tmp/plder_deploy_key.pub
```

- [ ] **Step 2: Register it on GitHub as read-only**

```bash
gh repo deploy-key add /tmp/plder_deploy_key.pub --repo Forgenn/plder --title "hermes-sync (read-only)"
gh repo deploy-key list --repo Forgenn/plder
```

Expected: the key listed, **without** write access. This key is for the
initContainer only — the meta bot's future push credential must be separate.

- [ ] **Step 3: Store the private key in Infisical**

Add the contents of `/tmp/plder_deploy_key` at path `/hermes/PLDER_DEPLOY_KEY`
in project `revachol-cluster-a82f`, environment `prod`. Then remove the local copies:

> The path is `/hermes/…`, matching every sibling key in this ExternalSecret
> (`/hermes/GIT_DEPLOY_KEY`, `/hermes/NIXOS_DEPLOY_KEY`, …). Storing it under
> `/hermes-agent/…` puts it somewhere External Secrets never looks, so
> `PLDER_DEPLOY_KEY` never lands in the `hermes-secrets` Secret — which is
> precisely the condition Task 8 Step 1 gates on.

```bash
rm -f /tmp/plder_deploy_key /tmp/plder_deploy_key.pub
```

- [ ] **Step 4: Add the key to the ExternalSecret**

In `infra/hermes-agent/secrets/externalsecret.yaml`, add a data entry following the
existing `GIT_DEPLOY_KEY` / `NIXOS_DEPLOY_KEY` pattern exactly:

```yaml
    - secretKey: PLDER_DEPLOY_KEY
      remoteRef:
        key: /hermes/PLDER_DEPLOY_KEY
```

- [ ] **Step 5: Add the SSH host alias**

In `infra/hermes-agent/configmap.yaml`, inside the `ssh_config` block, after the
`github.com-nixos` stanza:

```
    Host github.com-plder
      HostName github.com
      User git
      IdentityFile /opt/data/home/.ssh/plder_deploy_key
      IdentitiesOnly yes
      StrictHostKeyChecking accept-new
```

- [ ] **Step 6: Extend the ssh-key-perms initContainer**

In `infra/hermes-agent/deployment.yaml`, in the `ssh-key-perms` initContainer's
script, add the new key — but **not** alongside the existing three. It is
optional and must be handled separately:

```sh
if cp /secrets/plder-deploy-key/PLDER_DEPLOY_KEY /ssh-keys/plder_deploy_key 2>/dev/null; then
  chown 10000:10000 /ssh-keys/plder_deploy_key
  chmod 600 /ssh-keys/plder_deploy_key
else
  echo "[ssh-key-perms] no plder key yet; config sync will soft-fail"
fi
exit 0
```

and add the matching `plder-deploy-key` volume (secret `hermes-secrets`, key
`PLDER_DEPLOY_KEY`, `defaultMode: 0400`) and its `volumeMounts` entry, mirroring
`nixos-deploy-key` **plus `optional: true`**, which `nixos-deploy-key` does not
have.

> **Why this one is different.** The other three keys are load-bearing for the
> agent's own git work and must fail loudly if absent — they stay under `set -e`.
> `PLDER_DEPLOY_KEY` only feeds the config clone, which is designed to
> soft-fail, and it is placed by hand in Infisical (Step 3), so "not there yet"
> is a normal state. Without `optional: true` on the volume, an `items:`
> selector naming a missing key makes kubelet fail `MountVolume.SetUp` and the
> pod never leaves `ContainerCreating`; with `strategy: Recreate` the healthy
> pod is already gone. Without the `2>/dev/null ||` guard, the bare `cp`
> crash-loops `ssh-key-perms` to the same effect. Either one takes the
> operator's only conversational interface offline over a config credential.
> See the shipped `deployment.yaml` for the exact text.

- [ ] **Step 7: Verify the manifests still build**

```bash
cd /c/Users/Pol/projects/gitops-check
kubectl kustomize infra/hermes-agent > /tmp/hermes-built.yaml && echo "BUILD OK"
grep -q "secretKey: PLDER_DEPLOY_KEY" /tmp/hermes-built.yaml && echo "OK: externalsecret entry"
grep -q "plder-deploy-key" /tmp/hermes-built.yaml && echo "OK: volume"
grep -q "github.com-plder" /tmp/hermes-built.yaml && echo "OK: ssh host alias"
```

Expected: `BUILD OK` followed by all three `OK:` lines.

- [ ] **Step 8: Commit**

```bash
git add infra/hermes-agent/secrets/externalsecret.yaml infra/hermes-agent/configmap.yaml infra/hermes-agent/deployment.yaml
git commit -m "hermes: add plder read-only deploy key for config sync"
```

---

### Task 7: Wire the profile-sync initContainer

**Files:**
- Modify: `infra/hermes-agent/kustomization.yaml`
- Modify: `infra/hermes-agent/deployment.yaml`

**Interfaces:**
- Consumes: `sync.py` and `cron_upsert.py` (Tasks 4–5), the `plder_deploy_key` and host alias (Task 6).
- Produces: a running initContainer that applies config at every pod start.

- [ ] **Step 1: Ship the sync scripts as a ConfigMap**

In `infra/hermes-agent/kustomization.yaml`, add a generator. A generator (not a plain
resource) is required here: its name carries a content hash, so editing the scripts
rolls the pod.

```yaml
configMapGenerator:
  - name: hermes-sync-scripts
    files:
      - sync/sync.py
      - sync/cron_upsert.py
```

- [ ] **Step 2: Add the initContainer**

In `infra/hermes-agent/deployment.yaml`, add **after** `ssh-key-perms` (it needs the
keys that container writes) and before `claude-install`:

```yaml
        - name: profile-sync
          image: nousresearch/hermes-agent:v2026.8.19
          command: ["/opt/hermes/.venv/bin/python", "/sync/sync.py"]
          securityContext:
            runAsUser: 10000
            runAsGroup: 10000
          env:
            - name: HERMES_HOME
              value: /opt/data
            - name: HOME
              value: /opt/data/home
            - name: AGENT_CONFIG_REF
              value: "REPLACE_WITH_PLDER_SHA"
            - name: AGENT_IMAGE
              value: "nousresearch/hermes-agent:v2026.8.19"
            - name: PYTHONPATH
              value: /sync
          volumeMounts:
            - name: data
              mountPath: /opt/data
            - name: sync-scripts
              mountPath: /sync
              readOnly: true
            - name: ssh-keys
              mountPath: /opt/data/home/.ssh/plder_deploy_key
              subPath: plder_deploy_key
              readOnly: true
            - name: ssh-config
              mountPath: /opt/data/home/.ssh/config
              subPath: ssh_config
              readOnly: true
```

and the matching volume:

```yaml
        - name: sync-scripts
          configMap:
            name: hermes-sync-scripts
            defaultMode: 0555
```

`HOME` is set so git resolves `github.com-plder` from `$HOME/.ssh/config`. Without it
the clone cannot authenticate.

- [ ] **Step 3: Pin the ref to the current plder HEAD**

```bash
cd /c/Users/Pol/projects/plder && git rev-parse --short HEAD
```

Replace `REPLACE_WITH_PLDER_SHA` in `deployment.yaml` with that value.

- [ ] **Step 4: Verify the manifests build and the ordering is right**

```bash
cd /c/Users/Pol/projects/gitops-check
kubectl kustomize infra/hermes-agent > /tmp/hermes-built.yaml
python -c "
import re
s=open('/tmp/hermes-built.yaml').read()
names=re.findall(r'^\s*- name: (kubectl-copy|ssh-key-perms|profile-sync|claude-install)\s*$', s, re.M)
print('initContainer order:', names)
assert names.index('ssh-key-perms') < names.index('profile-sync'), 'profile-sync must follow ssh-key-perms'
print('OK')
"
```

Expected: `profile-sync` appears after `ssh-key-perms`, then `OK`.

- [ ] **Step 5: Commit**

```bash
git add infra/hermes-agent/kustomization.yaml infra/hermes-agent/deployment.yaml
git commit -m "hermes: add profile-sync initContainer"
```

---

### Task 8: Deploy and verify live

**Gates:** two, both blocking, both checked *before* the merge in Step 3.

1. **`PLDER_DEPLOY_KEY` must already be in the `hermes-secrets` Secret** (Step 1).
   Merging without it takes the agent offline, it does not merely skip the sync.
2. **The `hermes-data` volsync backup must be working again** (Step 2) — a
   `lastSyncTime` newer than its `lastSyncStartTime`. This task restarts the pod,
   and the last good snapshot predates the 2026-09-08 corruption.

**Files:** none — verification only.

- [ ] **Step 1: Confirm the plder deploy key actually landed in the Secret**

```bash
kubectl get secret hermes-secrets -n hermes \
  -o jsonpath='{.data.PLDER_DEPLOY_KEY}' | wc -c
```

Expected: a number in the low thousands. **If it prints `0`, STOP.** The key is
not there — almost certainly because it was stored at the wrong Infisical path
(`/hermes-agent/…` instead of `/hermes/…`, see Task 6 Step 3, which is where
External Secrets actually looks). Go back and fix that before merging anything.

Why this is a hard gate and not a warning: with the key missing, the
`plder-deploy-key` volume has nothing to select. It carries `optional: true`
precisely so kubelet does not fail `MountVolume.SetUp` and strand the pod in
`ContainerCreating` — and `strategy: Recreate` means the healthy pod is already
gone by then, so that is a full outage of the operator's only conversational
interface, caused by a missing config credential. `optional: true` plus the
guarded `cp` in `ssh-key-perms` turn that outage into a soft failure, but a soft
failure still means this task verifies nothing: the clone cannot authenticate,
every sync step is skipped, and Steps 5–9 below have nothing to assert on.
Land the key first.

- [ ] **Step 2: Confirm the backup gate**

```bash
kubectl get replicationsource hermes-data -n hermes \
  -o custom-columns='LASTSYNC:.status.lastSyncTime,START:.status.lastSyncStartTime'
```

Expected: `LASTSYNC` is recent (within a day) and not older than `START`. If it is
still stale, stop — fix volsync first.

- [ ] **Step 3: Merge the branch and let ArgoCD sync**

```bash
cd /c/Users/Pol/projects/gitops-check
git checkout main && git merge hermes-config-as-code && git push origin main
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status}{"\n"}'
```

Expected: `Synced` within a few minutes.

- [ ] **Step 4: Watch the initContainer run**

```bash
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=300s
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
kubectl logs -n hermes $POD -c profile-sync
```

Expected, in order:

```
[profile-sync] copied SOUL.md
[profile-sync] cron: <n> job(s) in /opt/data/cron/jobs.json
[profile-sync] skills synced for /opt/data
[profile-sync] applied ref <sha> (result: ok)
```

No `ERROR`, and no `WARNING` except possibly the skills one.

Note there is **no** `copied config.yaml` line, and there should not be: the sync
copies `SOUL.md` only (Task 5 Step 3). `result: ok` is the line that matters — it
is what Step 7's skip gate depends on.

Also confirm the optional-key path did the right thing:

```bash
kubectl logs -n hermes $POD -c ssh-key-perms
```

Expected: empty. `[ssh-key-perms] no plder key yet; config sync will soft-fail`
means Step 1's gate was skipped or the key has since gone.

- [ ] **Step 5: Verify the cron job is restored**

```bash
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron list
```

Expected: `[bot:monitor] daily exception report`, schedule `0 9 * * *`, next run
tomorrow at 09:00.

- [ ] **Step 6: Verify user data survived**

```bash
kubectl exec -n hermes $POD -- sh -c 'ls /opt/data/memories/ | head; ls /opt/data/profiles/'
```

Expected: memories present; `monitor` profile still listed.

- [ ] **Step 7: Verify a restart is a no-op (the skip gate)**

```bash
kubectl exec -n hermes $POD -- cat /opt/data/.agent-config/applied
kubectl delete pod -n hermes $POD
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=300s
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
kubectl logs -n hermes $POD -c profile-sync
```

Expected: `ref <sha> + image already applied; skipping` — and nothing else. This
proves an unplanned restart never depends on GitHub.

The `applied` record printed first is the reason this works, and it must show
**all three** of `ref`, `image` and `"result": "ok"`. `should_skip` requires the
result too, not just a ref+image match. If `result` is `"partial"` the skip will
never fire and every restart will re-clone from GitHub — read the `WARNING` lines
in Step 4's log to find which step failed, and fix that rather than accepting the
re-clone.

- [ ] **Step 8: Verify the two upsert safety rules that protect the operator's data**

The point of the upsert is not that unmanaged jobs survive in general — that is
trivially true for any job the declaration never names. The two behaviours worth
proving are the ones that can destroy or silently drop data.

First create a hand-made job to stand in for one made from Telegram. Use a
prompt-based job: `--script` requires a file under `<home>/scripts/`, which
nothing in this plan creates, so a `--script` job would fail for the wrong reason.

```bash
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron create "0 4 * * *" \
  --name "scratch handmade probe" --prompt "say hi" --deliver local
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron list
```

Note the id the CLI assigned to `scratch handmade probe` — call it `$HANDMADE`.

**(a) Tombstone retirement.** A job that was declared, synced, then removed from
the repo must disappear on the next sync, while the hand-made job must not.

In the plder clone, add a second throwaway job to `hermes/root/cron/jobs.json`
(`"id": "scratch-decl-probe"`, `"managed_by": "plder"`, any schedule), push, bump
`AGENT_CONFIG_REF` in `deployment.yaml` to the new SHA, push, and wait for the
sync. `hermes cron list` must now show three jobs. Then **remove** that entry
from `jobs.json`, push, bump `AGENT_CONFIG_REF` again, and wait.

Expected after the second sync: `scratch-decl-probe` is **gone** (it carried
`managed_by: plder`, so the upsert retires it), the real `[bot:monitor]` job is
still there, and `scratch handmade probe` is **untouched** — same id, same
schedule, same `next_run_at`. A hand-made job must never be collateral damage of
a repo deletion.

**(b) Id-collision refusal.** A declaration whose id matches a hand-made job must
leave that job alone and skip itself, rather than adopting it.

Add an entry to `jobs.json` using `$HANDMADE` as its `"id"` with a deliberately
different `"name"` (e.g. `"name": "SHOULD NOT APPEAR"`), push, bump
`AGENT_CONFIG_REF`, wait for the sync, then:

```bash
kubectl logs -n hermes $POD -c profile-sync | grep "id already used by a hand-made job"
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron list
```

Expected: the `WARNING … declared job(s) skipped — id already used by a hand-made
job: <HANDMADE>` line is present, and the job still shows its original name, not
`SHOULD NOT APPEAR`. The operator adopts a job deliberately, by deleting the live
one first — never implicitly by id collision.

Then clean up: remove the colliding entry from `jobs.json`, push, bump
`AGENT_CONFIG_REF` back to a clean SHA, and delete the probe:

```bash
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron remove "scratch handmade probe" -y
```

- [ ] **Step 9: Verify the offline / last-good path**

This is the other half of "the initContainer must never fail the pod": when
GitHub is unreachable the agent must still boot, on the last config that worked.

First confirm a last-good tree exists (Step 4's successful sync writes it):

```bash
kubectl exec -n hermes $POD -- sh -c 'ls -la /opt/data/.agent-config/last-good/hermes/root/'
```

Expected: `SOUL.md` and `cron/` present. If this is empty the rest of the step
proves nothing.

Then force the clone to fail while leaving everything else intact. The least
invasive way is to point the sync at a ref that does not exist: set
`AGENT_CONFIG_REF` in `deployment.yaml` to `0000000`, push, and wait for the roll.
(Breaking the SSH alias or the key would also work but risks leaving the agent's
own git broken.)

```bash
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=300s
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
kubectl logs -n hermes $POD -c profile-sync
```

Expected:

```
[profile-sync] WARNING clone failed: …
[profile-sync] falling back to last-good tree
[profile-sync] copied SOUL.md
[profile-sync] cron: <n> job(s) in /opt/data/cron/jobs.json
[profile-sync] applied ref <PREVIOUS sha, not 0000000>
```

and — the part that actually matters — **the pod reaches Ready**. The
initContainer exits 0 on every failure path; a `CrashLoopBackOff` or an
`Init:Error` here is a release blocker, not a config nuisance.

The recorded ref being the *previous* one is deliberate: it means the next
restart does not skip and will retry the intended ref. Confirm that, then restore
`AGENT_CONFIG_REF` to the real SHA, push, and verify Step 4's log again.

- [ ] **Step 10: Check the config-migrate warning (expected to still be present)**

```bash
kubectl logs -n hermes $POD -c hermes-agent | grep -c "config-migrate"
```

Expected: **non-zero**, and that is correct for this phase. The warning comes
from `config.yaml` being a read-only ConfigMap mount with no `_config_version`,
and this plan deliberately leaves that mount in place — the sync copies `SOUL.md`
only (Task 5 Step 3). `_config_version: 12` is already pinned in the declared
`config.yaml` (Task 2 Step 3), so the warning disappears on the day the ConfigMap
mount is retired. That retirement is listed under "Out of scope" below and is
gated on this sync being proven first.

---

## Out of scope for this plan

Tracked in the spec, delivered by later plans: gateway multiplex and the monitor as a
real bot; the `update-gitops` CI job and its validation gate; the remaining six bots;
`skills/custom/` and meta authoring into the repo.

**Retiring the root `config.yaml` ConfigMap mount.** Deliberately not in this
phase, and the sequencing matters. The mount must come out of `deployment.yaml`
*and* `config.yaml` must be added back to `sync.ROOT_FILES` in the same change —
they are one atomic swap of who owns the file. Doing it early is unsafe: if the
clone soft-fails on a fresh PVC (no deploy key, GitHub unreachable, no last-good
tree) the agent would boot with no `config.yaml` at all. The precondition is this
sync running clean across several restarts, with `result: "ok"` and the last-good
fallback exercised (Task 8 Steps 7 and 9). Removing the mount is also what
finally silences the `[config-migrate]` warning (Task 8 Step 10).
