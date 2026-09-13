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

Capture **before** declaring. The sync overwrites `config.yaml` on the pod; if the repo's copy is not the live one, the first deploy silently changes the agent's behaviour.

**Files:**
- Create: `plder/hermes/root/SOUL.md`, `plder/hermes/root/config.yaml`

**Interfaces:**
- Produces: the two files the sync script copies in Task 6 step 6.

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
version 12` appears on every boot. Pin it so the warning stops once the file is
writable:

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

```python
# infra/hermes-agent/sync/cron_upsert.py
"""Merge declared cron jobs into a live Hermes jobs.json.

Hermes rewrites jobs.json on every scheduler tick (next_run_at, last_run_at,
last_status, failure_streak, repeat.completed), and users create jobs
conversationally from Telegram. A declarative overwrite would therefore both
reset the scheduler and delete the user's own jobs. This merges by id instead.
"""
from __future__ import annotations

from typing import Any, Dict, List

MANAGED_BY = "plder"

# Fields Hermes owns at runtime; never clobbered by a redeclaration.
RUNTIME_FIELDS: tuple[str, ...] = (
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
    "failure_streak",
    "monitor_state",
    "paused_at",
    "paused_reason",
    "created_at",
)


def upsert_jobs(live: Dict[str, Any], declared: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a merged jobs document.

    - A declared job replaces its live namesake field-for-field, except that
      RUNTIME_FIELDS and repeat.completed carry over from the live copy.
    - ``state`` carries over too, unless the declaration sets it explicitly —
      that is how a job is paused or resumed from git.
    - A live job with no declaration survives untouched when it is not ours,
      and is retired when it is (it was declared once and has since been
      removed from the repo).
    """
    live_jobs = {j["id"]: j for j in (live or {}).get("jobs", [])}
    declared_ids = {d["id"] for d in declared}
    merged: List[Dict[str, Any]] = []

    for decl in declared:
        job = dict(decl)
        job["managed_by"] = MANAGED_BY
        prev = live_jobs.get(job["id"])
        if prev is not None:
            for field in RUNTIME_FIELDS:
                if field in prev:
                    job[field] = prev[field]
            if "state" not in decl and "state" in prev:
                job["state"] = prev["state"]
            if "completed" in (prev.get("repeat") or {}):
                job.setdefault("repeat", {})["completed"] = prev["repeat"]["completed"]
        merged.append(job)

    for job_id, job in live_jobs.items():
        if job_id in declared_ids:
            continue
        if job.get("managed_by") == MANAGED_BY:
            continue  # retired: we declared it once, the repo dropped it
        merged.append(job)

    return {"jobs": merged}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest test_cron_upsert.py -v
```

Expected: PASS, 8 passed.

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

```python
#!/usr/bin/env python3
# infra/hermes-agent/sync/sync.py
"""Apply declared Hermes config from a plder checkout onto /opt/data.

Runs as an initContainer before the gateway starts. It must NEVER fail the
pod: the agent is the operator's primary interface, and a config problem
must not take it offline. Every failure path logs and continues.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cron_upsert import upsert_jobs

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_DIR = HERMES_HOME / ".agent-config"
APPLIED = STATE_DIR / "applied"
LAST_GOOD = STATE_DIR / "last-good"
STAGING = Path("/staging")
REPO_URL = os.environ.get("AGENT_CONFIG_REPO", "git@github.com-plder:Forgenn/plder.git")
REF = os.environ.get("AGENT_CONFIG_REF", "")
IMAGE = os.environ.get("AGENT_IMAGE", "")
VENV_PY = "/opt/hermes/.venv/bin/python"


def log(msg: str) -> None:
    print(f"[profile-sync] {msg}", flush=True)


def should_skip(applied_path: Path, ref: str, image: str) -> bool:
    """True when the recorded ref AND image both match what we are asked for."""
    try:
        rec = json.loads(Path(applied_path).read_text())
    except Exception:
        return False
    return rec.get("ref") == ref and rec.get("image") == image


def clone(dest: Path) -> str | None:
    """Clone REPO_URL at REF into dest. Returns the applied ref, or None."""
    try:
        subprocess.run(["git", "clone", "--no-checkout", REPO_URL, str(dest)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "checkout", REF],
                       check=True, capture_output=True)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return REF
    except subprocess.CalledProcessError as exc:
        log(f"WARNING clone failed: {exc.stderr.decode(errors='replace').strip()[:300]}")
        return None


def apply_cron(home: Path, declared_file: Path) -> None:
    """Merge declared jobs into home/cron/jobs.json by id."""
    if not declared_file.is_file():
        return
    try:
        declared = json.loads(declared_file.read_text()).get("jobs", [])
        live_file = home / "cron" / "jobs.json"
        live = json.loads(live_file.read_text()) if live_file.is_file() else {}
        merged = upsert_jobs(live, declared)
        live_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = live_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2))
        tmp.replace(live_file)
        log(f"cron: {len(merged['jobs'])} job(s) in {live_file}")
    except Exception as exc:
        log(f"WARNING cron upsert failed for {home}: {exc}")


def sync_skills(home: Path) -> None:
    """Run the bundled-skill sync for one home (root or a profile)."""
    try:
        subprocess.run(
            [VENV_PY, "-c", "from tools.skills_sync import sync_skills; sync_skills()"],
            env={**os.environ, "HERMES_HOME": str(home)},
            cwd="/opt/hermes", check=True, capture_output=True, timeout=120,
        )
        log(f"skills synced for {home}")
    except Exception as exc:
        log(f"WARNING skills_sync failed for {home}: {exc}")


def copy_root_files(staged: Path) -> None:
    for name in ("SOUL.md", "config.yaml"):
        src = staged / "hermes" / "root" / name
        if src.is_file():
            shutil.copy2(src, HERMES_HOME / name)
            log(f"copied {name}")


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if should_skip(APPLIED, REF, IMAGE):
        log(f"ref {REF} + image already applied; skipping")
        return 0

    if STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)
    applied_ref = clone(STAGING)

    if applied_ref:
        shutil.rmtree(LAST_GOOD, ignore_errors=True)
        shutil.copytree(STAGING, LAST_GOOD)
    elif LAST_GOOD.is_dir():
        log("falling back to last-good tree")
        shutil.rmtree(STAGING, ignore_errors=True)
        shutil.copytree(LAST_GOOD, STAGING)
        try:
            applied_ref = json.loads(APPLIED.read_text()).get("ref")
        except Exception:
            applied_ref = None
    else:
        log("ERROR no clone and no last-good tree; leaving config untouched")
        return 0

    copy_root_files(STAGING)
    apply_cron(HERMES_HOME, STAGING / "hermes" / "root" / "cron" / "jobs.json")
    sync_skills(HERMES_HOME)

    APPLIED.write_text(json.dumps({
        "ref": applied_ref, "image": IMAGE,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "profiles": [], "result": "ok",
    }, indent=2))
    log(f"applied ref {applied_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note `applied_ref` is what actually landed — after a fallback it is the *previous*
ref, so the next restart does not skip and will retry the new one.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest test_sync.py test_cron_upsert.py -v
```

Expected: PASS, 13 passed.

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

Add the contents of `/tmp/plder_deploy_key` at path `/hermes-agent/PLDER_DEPLOY_KEY`
in project `revachol-cluster-a82f`, environment `prod`. Then remove the local copies:

```bash
rm -f /tmp/plder_deploy_key /tmp/plder_deploy_key.pub
```

- [ ] **Step 4: Add the key to the ExternalSecret**

In `infra/hermes-agent/secrets/externalsecret.yaml`, add a data entry following the
existing `GIT_DEPLOY_KEY` / `NIXOS_DEPLOY_KEY` pattern exactly:

```yaml
    - secretKey: PLDER_DEPLOY_KEY
      remoteRef:
        key: /hermes-agent/PLDER_DEPLOY_KEY
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
script, add the new key alongside the existing three:

```sh
cp /secrets/plder-deploy-key/PLDER_DEPLOY_KEY /ssh-keys/plder_deploy_key
chown 10000:10000 /ssh-keys/plder_deploy_key
chmod 600 /ssh-keys/plder_deploy_key
```

and add the matching `plder-deploy-key` volume (secret `hermes-secrets`, key
`PLDER_DEPLOY_KEY`, `defaultMode: 0400`) and its `volumeMounts` entry, mirroring
`nixos-deploy-key`.

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

**Gate:** do not start this task until the `hermes-data` volsync ReplicationSource has
a `lastSyncTime` newer than its `lastSyncStartTime` — i.e. the backup works again.
This task restarts the pod, and the current last good snapshot predates the 2026-09-08
corruption.

**Files:** none — verification only.

- [ ] **Step 1: Confirm the backup gate**

```bash
kubectl get replicationsource hermes-data -n hermes \
  -o custom-columns='LASTSYNC:.status.lastSyncTime,START:.status.lastSyncStartTime'
```

Expected: `LASTSYNC` is recent (within a day) and not older than `START`. If it is
still stale, stop — fix volsync first.

- [ ] **Step 2: Merge the branch and let ArgoCD sync**

```bash
cd /c/Users/Pol/projects/gitops-check
git checkout main && git merge hermes-config-as-code && git push origin main
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status}{"\n"}'
```

Expected: `Synced` within a few minutes.

- [ ] **Step 3: Watch the initContainer run**

```bash
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=300s
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
kubectl logs -n hermes $POD -c profile-sync
```

Expected: `copied SOUL.md`, `copied config.yaml`, `cron: 1 job(s) …`,
`skills synced …`, `applied ref <sha>`. No `ERROR`.

- [ ] **Step 4: Verify the cron job is restored**

```bash
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron list
```

Expected: `[bot:monitor] daily exception report`, schedule `0 9 * * *`, next run
tomorrow at 09:00.

- [ ] **Step 5: Verify user data survived**

```bash
kubectl exec -n hermes $POD -- sh -c 'ls /opt/data/memories/ | head; ls /opt/data/profiles/'
```

Expected: memories present; `monitor` profile still listed.

- [ ] **Step 6: Verify a restart is a no-op (the skip gate)**

```bash
kubectl delete pod -n hermes $POD
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=300s
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
kubectl logs -n hermes $POD -c profile-sync
```

Expected: `ref <sha> + image already applied; skipping` — and nothing else. This
proves an unplanned restart never depends on GitHub.

- [ ] **Step 7: Verify the upsert preserves a hand-made job**

Create a job from Telegram (or via the CLI), restart, and confirm it survives:

```bash
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron create "0 4 * * *" \
  --name "scratch upsert probe" --script probe.sh --no-agent --deliver local
kubectl delete pod -n hermes $POD
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=300s
POD=$(kubectl get pods -n hermes -o name | head -1 | sed 's|pod/||')
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron list
```

Expected: **both** jobs listed. Then remove the probe:

```bash
kubectl exec -n hermes $POD -- /command/s6-setuidgid hermes \
  env HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes cron remove "scratch upsert probe" -y
```

- [ ] **Step 8: Confirm the config-migrate warning is gone**

```bash
kubectl logs -n hermes $POD -c hermes-agent | grep -c "config-migrate" || echo "0 — warning gone"
```

Expected: `0 — warning gone`, because `config.yaml` is now a writable file carrying
`_config_version` rather than a read-only ConfigMap mount.

---

## Out of scope for this plan

Tracked in the spec, delivered by later plans: gateway multiplex and the monitor as a
real bot; the `update-gitops` CI job and its validation gate; the remaining six bots;
`skills/custom/` and meta authoring into the repo; retiring the root `config.yaml`
ConfigMap mount once the copied file is proven.
