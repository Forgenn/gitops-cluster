# Hermes CI and Single-Source Root Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make plder the only source of the Hermes root `config.yaml`, and make a plder push deploy itself: CI validates the declared config and pins the pushed commit into gitops-cluster, with no human step. Then run the live checks that plan 1 wrote but never ran.

**Architecture:** `sync.py` installs `hermes/root/config.yaml` onto the PVC in the profile-sync initContainer, before the gateway starts. It checks the declaration first and keeps the live file if the declaration is bad. The `hermes-config` ConfigMap loses its `config.yaml` key and its subPath mount in the same change, so no mirror can drift. In plder, a GitHub Actions workflow runs a pure-Python validator plus a dry-run `hermes profile install` inside the pinned image. On master pushes that touch `hermes/**` it then commits `AGENT_CONFIG_REF=<sha>` into gitops-cluster over an SSH deploy key. ArgoCD rolls the pod from there.

**Tech Stack:** Python 3.12/3.13 + pytest + PyYAML; GitHub Actions (ubuntu-24.04, docker); Kubernetes (k3s) + Kustomize + ArgoCD v3.1.8; Hermes Agent `nousresearch/hermes-agent:v2026.8.19` (hermes_cli 0.20.5); `gh` CLI; `cryptography` 44.0.2 on the workstation.

**Spec:** `docs/plans/2026-09-12-hermes-config-as-code.md` (Design step 6, "Pinning and CI", Verification). Builds on `docs/plans/2026-09-13-hermes-config-sync-root.md` (plan 1) and `docs/plans/2026-09-13-hermes-monitor-bot.md` (plan 2). Plan 2's "Verified facts", "Incident 2026-09-13" and "Follow-up 2026-09-14" are binding here.

---

## Verified facts this plan relies on (read from the live pod / GitHub, 2026-09-14)

Re-verify if the image changes.

- **Boot order.** The image ENTRYPOINT is `/opt/hermes/docker/entrypoint-dispatch.sh`. As PID 1 it execs s6-overlay `/init`, which runs `docker/stage2-hook.sh` as a cont-init hook, then `main-wrapper.sh`. When it is not PID 1 it runs `stage2-hook.sh` directly, then `main-wrapper.sh`. The Dockerfile ends `USER root`. `stage2-hook.sh` exits 1 if the container starts as a uid that is neither 0 nor `hermes` (10000). All of this runs in the main container, **after** every initContainer. So profile-sync's copy of `config.yaml` is always in place before stage2 and before the gateway.
- **stage2 and config.yaml.** Relevant stage2 steps, in order:
  - `seed_one "config.yaml" "cli-config.yaml.example"` copies the vendor example **only if `/opt/data/config.yaml` does not exist**.
  - It then `chown hermes:hermes` and `chmod 640` the file.
  - It then runs `scripts/docker_config_migrate.py` as hermes.
- **`[config-migrate]`.** `docker_config_migrate.py` reads `check_config_version()`, which returns `(raw _config_version or 0, DEFAULT_CONFIG["_config_version"])`. **Latest is 38** (`hermes_cli/config_defaults.py:3630`). Three cases:
  - `current >= latest`: it does nothing.
  - `current < SUPPORT_FLOOR_VERSION` (12): it prints the warning seen on every boot today and does not write. An unversioned file counts as 0, which is the case for today's root config.
  - Anything from 12 to 37: it **backs up config.yaml and .env and rewrites config.yaml** via `migrate_config()`.

  Once config.yaml is a writable synced file, it must therefore carry `_config_version: 38` exactly. A lower value makes the file drift from git on every boot, and an absent one keeps the warning.
- **The file under the mount.** The current mount is visible in `/proc/self/mountinfo`: `.../configmap/config/..2026_09_14_16_39_40.../config.yaml /opt/data/config.yaml ro`. The PVC file underneath is masked. Plan 1 says it is the kubelet subPath stub owned by root, which uid 10000 cannot write through. `/opt/data` itself is `drwxrws--- hermes hermes` (setgid), so uid 10000 **can** rename over that stub. The copy must be write-temp-then-`os.replace`, never `copy2` onto the path.
- **Runtime writers of config.yaml exist.** Examples: `tools/approval.py:2955` (`save_config(load_config())` for the command allowlist), `gateway/slash_commands.py:2088,2414` (`/model` persistence), and the dashboard web routers. Today they fail silently against the read-only mount. Afterwards they succeed until the next **applied** sync, which reverts them. A plain restart with an unchanged ref skips the sync and keeps them.
- **`hermes profile install`** exits 1 on `DistributionError`/`ValueError` (`hermes_cli/main.py` ~10778). `plan_install` validates the target name. `install_distribution(force=True)` copies `config.yaml` verbatim (`preserve_config=False`).
- **`validate_config_structure(cfg)`** (`hermes_cli/config.py:2042`) returns `[]` for both the current root and monitor configs (checked read-only against `last-good` in the live pod).
- **Cron:**
  - Schedule kinds are `once | interval | cron`. Declarable states are `scheduled | paused`.
  - `hermes cron pause ID` sets `enabled: false, state: paused, paused_at` (`cron/jobs.py:2256`).
  - A job id only has to be a safe single path component (`cron/jobs.py:463`).
  - `deliver` is a comma-separated string. Each part is one of `local`, `origin`, `all`, `<platform>`, or `<platform>:<chat>[:<thread>]`. Platform must be in `_KNOWN_DELIVERY_PLATFORMS` (`cron/scheduler.py:500`).
- **`cron_upsert` already keeps a paused managed job paused.** If the declaration is silent about `state`, live `enabled` wins (rule 1b in `cron_upsert.py`). Task 11 proves this live.
- **Workstation:**
  - plder is checked out with `core.autocrlf=true`. Working-tree files are CRLF, while git blobs and the pod are LF. **Always hash `git show <sha>:<path> | md5sum`, never the working-tree file.** Today, for example, the working tree hashes `eb3d98…` against the blob and pod `6a5c9a…`.
  - Python 3.12.0 has `cryptography` 44.0.2 and PyYAML 6.0.3. `gh` resolves to `C:\Program Files\GitHub CLI\gh.EXE`. The Docker daemon is **not running**, so image rehearsals use a throwaway pod.
- **GitHub:**
  - plder has no `.github/`, no Actions secrets, and Actions are enabled (`allowed_actions: all`).
  - gitops-cluster `main` is unprotected. Its deploy keys are `161417504 "Hermes Revachol Cluster"` (RW, the agent's) and `163152504` (RW, reserved).
  - plder's deploy keys are `163153110` (RO sync) and `163152504` (RW, "hermes deploy").
  - GitHub's published ed25519 host key is `AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl` (`gh api meta`).
  - loomie's `update-gitops` job clones with a PAT and `sed`s tags. This plan uses a deploy key and a tested pattern edit instead.
- **Live state at plan time:**
  - applied `{ref: cb6f405, result: ok}`, with no `sync_version` field.
  - `last-good/hermes/root/config.yaml` present (md5 `6a5c9a…`).
  - Root cron store 0 jobs; monitor store 1 job (`6270d3f018f2`).
  - `gateway_state.json` `served_profiles: ["default","monitor"]`.
  - `hermes-data` volsync lastSync 2026-09-14T02:06Z.

## Global Constraints

- Image `nousresearch/hermes-agent:v2026.8.19`. Do not bump it. `HERMES_HOME=/opt/data`. CLI `/opt/hermes/.venv/bin/hermes`.
- **`sync.py` must never fail the pod.** `main()` returns 0 on every path, and **config must never take the gateway down.** A bad declared root config keeps the live file.
- **Controller-only steps.** Subagents must not run them. They are: creating credentials (Task 8); every `git push` to `Forgenn/plder` or `Forgenn/gitops-cluster`; PR create/merge/close; anything reading a Secret into a pod; and every step that touches the live pod or rolls it (Tasks 3 Step 5, 4, 7 Step 5, 9, 10, 11). Implementation subagents do Tasks 1, 2 (Steps 2–4), 3 (Steps 1–4), 5, 6 and 7 (Steps 1–4), and commit locally only.
- **Tests stay hermetic.** The workstation `HERMES_HOME` is a live Hermes Desktop install. `test_sync.py`'s autouse `isolated_main` fixture must stay. plder's tests write only under `tmp_path`.
- `kubectl exec` lands as uid 0: wrap in-pod writes in `/command/s6-setuidgid hermes`. Inside `kubectl exec -i … sh -s <<'SH'`, give any command that may read stdin `</dev/null`. Git Bash needs `export MSYS_NO_PATHCONV=1`.
- **Never run `hermes profile install` or sync rehearsals in the live container.** Use a throwaway pod: same image, `sleep` command, `runAsUser/runAsGroup 10000`, deleted by `trap`. Each rehearsal step below is **one** shell invocation, because shell state does not persist between tool calls.
- Private keys never touch the workstation disk. Print only key ids, fingerprints and booleans.
- Real-path probes are **scheduled** (`M5=$(date -u -d @$(( $(date +%s) + 180 )) +"%M %H")`) and checked for `last_status` **and** the `## Response` section. `hermes cron run` executes inline and proves nothing about the gateway.
- **ArgoCD:** "Synced" can hide a ComparisonError. Always check `.status.conditions` and the controller logs.
- **Windows:** no pod rolls between 08:45 and 09:15 UTC (the monitor's 09:00 report). Before any task that rolls the pod: `kubectl get replicationsource hermes-data -n hermes -o jsonpath='{.status.lastSyncTime}'` must be within 36h.
- **Waiting.** If foreground `sleep` is blocked in your shell tool, run the poll loops below with `run_in_background` or the Monitor tool. `gh run watch` and `kubectl wait` block without sleeping.
- plder default branch `master`, gitops-cluster `main`. Commit style `component: lowercase description`. Append the attribution trailers your session requires.
- Once Task 9 merges CI, **every push to plder master touching `hermes/**` (except `hermes/ci/**`) restarts the agent within ~10 minutes.** Before any manual gitops commit, `git pull --ff-only` in the gitops clone: the CI bot commits to `main` too.

### Throwaway-pod manifest (used by every rehearsal)

Paste at the top of a rehearsal step, replacing `P`:

```bash
export MSYS_NO_PATHCONV=1
NS=hermes; P=REPLACE_POD_NAME
cleanup() { kubectl delete pod "$P" -n "$NS" --ignore-not-found --wait=true; kubectl get pod "$P" -n "$NS" 2>&1 | tail -1; }
trap cleanup EXIT
kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $P
  namespace: hermes
  labels: {purpose: plan3-rehearsal}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 10000, runAsGroup: 10000}
  containers:
    - name: rehearsal
      image: nousresearch/hermes-agent:v2026.8.19
      command: ["sleep", "1800"]
      env:
        - {name: HOME, value: /tmp/home}
YAML
kubectl wait --for=condition=Ready "pod/$P" -n "$NS" --timeout=600s
```

The trap's last line must print `Error from server (NotFound)`.

### Agent-probe kit (used by Tasks 4, 10, 11)

**Create** (one invocation):

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -i -n hermes "$POD" -c hermes-agent -- sh -s <<'SH'
H=/opt/hermes/.venv/bin/hermes
M5=$(date -u -d @$(( $(date +%s) + 180 )) +"%M %H")
for M in /opt/data /opt/data/profiles/monitor; do
  /command/s6-setuidgid hermes env HERMES_HOME=$M $H cron create "$M5 * * *" \
    "Reply with exactly AGENT_PROBE_OK and nothing else." \
    --name "plan3 agent probe" --deliver local </dev/null 2>&1 | tail -2
done
echo "fires at UTC 'minute hour' = $M5; now $(date -u +%H:%M:%S)"
SH
```

**Check.** Run at least 2 minutes after the printed fire time:

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -i -n hermes "$POD" -c hermes-agent -- sh -s <<'SH'
for M in /opt/data /opt/data/profiles/monitor; do
/opt/hermes/.venv/bin/python - "$M" <<'PY'
import glob, json, os, sys
home = sys.argv[1]
jobs = [j for j in json.load(open(home + "/cron/jobs.json"))["jobs"] if j.get("name") == "plan3 agent probe"]
if not jobs:
    print(home, "| NO PROBE JOB"); sys.exit()
job = jobs[0]
outs = sorted(glob.glob(f"{home}/cron/output/{job['id']}/*.md"), key=os.path.getmtime)
text = open(outs[-1], encoding="utf-8").read() if outs else ""
response = text.split("## Response", 1)[1] if "## Response" in text else ""
print(home, "| id", job["id"], "| last_status", job.get("last_status"),
      "| last_error", job.get("last_error"), "| response ok:", "AGENT_PROBE_OK" in response)
PY
done
SH
```

Expected for **both** homes: `last_status ok | last_error None | response ok: True`. If `last_status` is still `None`, wait another minute and re-check. **If either home fails after 5 minutes, STOP: the operator's agent is likely down. Roll back (see the task's rollback note) first, diagnose second.**

**Remove:**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -i -n hermes "$POD" -c hermes-agent -- sh -s <<'SH'
H=/opt/hermes/.venv/bin/hermes
for M in /opt/data /opt/data/profiles/monitor; do
  for ID in $(/opt/hermes/.venv/bin/python -c "import json,sys; print(' '.join(j['id'] for j in json.load(open(sys.argv[1]+'/cron/jobs.json'))['jobs'] if j.get('name')=='plan3 agent probe'))" "$M" </dev/null); do
    /command/s6-setuidgid hermes env HERMES_HOME=$M $H cron remove "$ID" </dev/null 2>&1 | tail -1
  done
done
SH
```

### Job-table printer (used by Tasks 4, 11)

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes "$POD" -c hermes-agent -- /opt/hermes/.venv/bin/python -c "
import json
for label, h in (('root', '/opt/data'), ('monitor', '/opt/data/profiles/monitor')):
    for j in json.load(open(h + '/cron/jobs.json'))['jobs']:
        print(label, j['id'], repr(j.get('name')), 'managed_by=%s' % j.get('managed_by'), 'state=%s' % j.get('state'),
              'enabled=%s' % j.get('enabled'), 'next=%s' % j.get('next_run_at'), 'paused_at=%s' % j.get('paused_at'))
"
```

---

## Part A: plder is the single source of root config

### Task 1: sync.py installs the root config.yaml (TDD, gitops-cluster)

**Files:**
- Modify: `infra/hermes-agent/sync/sync.py`
- Test: `infra/hermes-agent/sync/test_sync.py`

**Interfaces:**
- Consumes: the existing `log`, `HERMES_HOME`, `STATE_DIR`, `APPLIED`, `STAGING`, `main()`.
- Produces:
  - `SYNC_VERSION: int = 2`.
  - `should_skip(applied_path, ref, image) -> bool`, which now also requires `rec["sync_version"] == SYNC_VERSION`.
  - `copy_root_config(staged: Path) -> bool`.
  - The `applied` record gains `"sync_version"`.
  - Log lines Task 4 asserts on: `copied config.yaml`, `copied config.yaml (previous saved to <STATE_DIR>/config.yaml.previous)`, `config.yaml already matches the declaration`, and `WARNING declared root config.yaml <reason>; keeping the live config.yaml`.

**Why `SYNC_VERSION`:** the deploy that retires the mount would otherwise be skipped by the skip gate if the ref and image happened not to change. The config would never be copied, and the pod would boot with the root-owned stub as its config. Keying the gate on the sync's own behaviour version makes any future change to *what* a sync writes re-apply exactly once.

**Why a bad declaration votes `partial`:** the record must not claim `ok` for a config it refused. `partial` makes the next restart re-clone and retry. That boot needs the network, but the live config is kept either way. CI (Task 5) makes this path unreachable in practice.

- [ ] **Step 1: Create the branch**

```bash
cd /c/Users/Pol/projects/gitops-check
git checkout main && git pull --ff-only && git checkout -b hermes-config-single-source
```

- [ ] **Step 2: Update the existing tests and add the failing ones**

Apply these edits to `infra/hermes-agent/sync/test_sync.py`, in order.

(a) Add `import os` on the line after `import json`.

(b) Replace the `_write` helper with:

```python
def _write(tmp_path: Path, ref: str, image: str, result: str = "ok",
           sync_version=None) -> Path:
    p = tmp_path / "applied"
    p.write_text(json.dumps({
        "ref": ref, "image": image, "result": result,
        "sync_version": sync_module.SYNC_VERSION if sync_version is None else sync_version,
    }))
    return p
```

(c) Directly after `test_skips_only_when_result_ok_and_ref_image_match`, insert:

```python
def test_does_not_skip_a_record_written_by_an_older_sync(tmp_path):
    """A change to what a sync writes must re-apply once, even at the same ref."""
    p = _write(tmp_path, "abc123", "img:v1", sync_version=sync_module.SYNC_VERSION - 1)
    assert should_skip(p, "abc123", "img:v1") is False


def test_does_not_skip_a_record_with_no_sync_version(tmp_path):
    """Records from before SYNC_VERSION existed (the live pod's today) re-apply."""
    p = tmp_path / "applied"
    p.write_text(json.dumps({"ref": "abc123", "image": "img:v1", "result": "ok"}))
    assert should_skip(p, "abc123", "img:v1") is False
```

(d) In `test_main_with_successful_clone_creates_applied_record`, after `assert rec["result"] == "ok"`, add:

```python
    assert rec["sync_version"] == sync_module.SYNC_VERSION
```

(e) **Delete** the whole function `test_config_yaml_is_not_copied_onto_the_home`. It asserts the behaviour this task removes.

(f) In `test_skills_sync_failure_does_not_flip_the_result_to_partial`, inside `mock_clone`, after the `SOUL.md` line, add:

```python
        (dest / "hermes" / "root" / "config.yaml").write_bytes(b"model:\n  default: test\n")
```

(g) In `test_main_moves_a_job_from_root_to_a_profile_and_records_it`, inside `fake_clone`, after the `jobs.json` write, add:

```python
        (dest / "hermes" / "root" / "config.yaml").write_bytes(b"model:\n  default: test\n")
```

(h) In `_stage_tree`, give the `if allowlist is not None:` block an `else:` branch, so every staged tree declares a root config the way plder does:

```python
    else:
        (dest / "hermes" / "root" / "config.yaml").write_bytes(b"model:\n  default: test\n")
```

(i) Replace `test_main_does_not_warn_when_the_staged_root_config_is_missing` with:

```python
def test_main_does_not_warn_when_the_staged_root_config_is_missing(monkeypatch, capsys):
    """F: no declared config means nothing to compare against; stay silent.

    A missing root config is also a broken declaration: the live config.yaml
    is kept and the run is recorded as partial (see copy_root_config)."""
    home = sync_module.HERMES_HOME
    _live_store(home, "desktopbot", {"jobs": [{"id": "hand1", "enabled": True}]})

    def fake_clone(dest):
        _stage_tree(dest, profiles=[("monitor", [])], allowlist=None)
        (dest / "hermes" / "root" / "config.yaml").unlink()
        return "abc1234"

    applied = _run_main(monkeypatch, fake_clone)
    out = capsys.readouterr().out
    assert "multiplex_profile_allowlist" not in out, out
    assert applied["result"] == "partial"
```

(j) Append at the end of the file. All config content is written as **bytes** so Windows newline translation cannot make a byte comparison lie:

```python
# ---- root config.yaml: plder is the single source ----------------------------

GOOD_CONFIG = b"model:\n  default: deepseek/deepseek-v4-flash-0731\n_config_version: 38\n"
LIVE_CONFIG = b"model:\n  default: live\n"


def _clone_with_root(config_bytes):
    """A staged tree whose root declares config_bytes (None = no config.yaml)."""
    def fake_clone(dest):
        root = dest / "hermes" / "root"
        (root / "cron").mkdir(parents=True, exist_ok=True)
        (root / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
        (root / "SOUL.md").write_text("# SOUL")
        if config_bytes is not None:
            (root / "config.yaml").write_bytes(config_bytes)
        return "abc1234"
    return fake_clone


def test_root_config_is_installed_byte_identical(monkeypatch, capsys):
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert (sync_module.HERMES_HOME / "config.yaml").read_bytes() == GOOD_CONFIG
    assert applied["result"] == "ok"
    assert applied["sync_version"] == sync_module.SYNC_VERSION
    assert "copied config.yaml" in capsys.readouterr().out


def test_root_config_replaces_a_drifted_live_file_and_keeps_the_previous_one(monkeypatch):
    """Git wins; what it replaced (runtime edits, the kubelet stub) is kept for inspection."""
    home = sync_module.HERMES_HOME
    (home / "config.yaml").write_bytes(LIVE_CONFIG)
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert (home / "config.yaml").read_bytes() == GOOD_CONFIG
    assert (sync_module.STATE_DIR / "config.yaml.previous").read_bytes() == LIVE_CONFIG
    assert applied["result"] == "ok"


def test_identical_live_root_config_is_not_rewritten(monkeypatch, capsys):
    home = sync_module.HERMES_HOME
    live = home / "config.yaml"
    live.write_bytes(GOOD_CONFIG)
    os.utime(live, ns=(1_000_000_000, 1_000_000_000))
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert live.stat().st_mtime_ns == 1_000_000_000
    assert not (sync_module.STATE_DIR / "config.yaml.previous").exists()
    assert applied["result"] == "ok"
    assert "config.yaml already matches the declaration" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    None,
    b"model: [unclosed\n",
    b"",
    b"- just\n- a list\n",
    b"\xff\xfe not utf-8\n",
], ids=["missing", "unparseable", "empty", "not-a-mapping", "not-utf8"])
def test_a_bad_declared_root_config_keeps_the_live_file(monkeypatch, capsys, bad):
    """Config must never take the gateway down: refuse, keep what boots today."""
    home = sync_module.HERMES_HOME
    live = home / "config.yaml"
    live.write_bytes(LIVE_CONFIG)
    applied = _run_main(monkeypatch, _clone_with_root(bad))
    assert live.read_bytes() == LIVE_CONFIG
    assert applied["result"] == "partial"
    assert "keeping the live config.yaml" in capsys.readouterr().out
    assert not list(home.glob(".config.yaml*")), "temp file left behind"


def test_a_bad_declaration_on_a_fresh_home_writes_no_config(monkeypatch):
    """No live file and a broken declaration: write nothing, so stage2 seeds its example."""
    applied = _run_main(monkeypatch, _clone_with_root(b"model: [unclosed\n"))
    assert not (sync_module.HERMES_HOME / "config.yaml").exists()
    assert applied["result"] == "partial"


def test_a_failed_replace_keeps_the_live_file_and_removes_the_temp(monkeypatch):
    home = sync_module.HERMES_HOME
    live = home / "config.yaml"
    live.write_bytes(LIVE_CONFIG)
    real_replace = os.replace

    def refuse_config(src, dst, *args, **kwargs):
        if Path(dst).name == "config.yaml":
            raise PermissionError("simulated: cannot rename over config.yaml")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_config)
    applied = _run_main(monkeypatch, _clone_with_root(GOOD_CONFIG))
    assert live.read_bytes() == LIVE_CONFIG
    assert not list(home.glob(".config.yaml*"))
    assert applied["result"] == "partial"


def test_offline_boot_installs_the_last_good_root_config(monkeypatch):
    home = sync_module.HERMES_HOME
    root = sync_module.LAST_GOOD / "hermes" / "root"
    root.mkdir(parents=True)
    (root / "config.yaml").write_bytes(GOOD_CONFIG)
    sync_module.APPLIED.write_text(json.dumps({"ref": "oldsha", "image": "img:v1", "result": "ok"}))
    applied = _run_main(monkeypatch, lambda dest: None)
    assert (home / "config.yaml").read_bytes() == GOOD_CONFIG
    assert applied["ref"] == "oldsha"
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest -q 2>&1 | tail -15
```

Expected: FAIL. The `should_skip` tests error with `AttributeError: module 'sync' has no attribute 'SYNC_VERSION'`, and the new config tests fail because nothing copies `config.yaml`.

- [ ] **Step 4: Implement in `sync.py`**

(a) Below `_PROFILE_NAME_RE = …`, add:

```python
# Version of WHAT a sync writes. Recorded in the applied record and required by
# should_skip, so changing sync behaviour re-applies once even when the ref and
# image did not move. 2 = root config.yaml installed from plder.
SYNC_VERSION = 2
ROOT_CONFIG = "config.yaml"
PREVIOUS_CONFIG = "config.yaml.previous"
```

(b) Replace `should_skip` with:

```python
def should_skip(applied_path: Path, ref: str, image: str) -> bool:
    """True when the record matches ref, image AND this sync's version, with result 'ok'."""
    try:
        rec = json.loads(Path(applied_path).read_text())
    except Exception:
        return False
    return (rec.get("ref") == ref and rec.get("image") == image
            and rec.get("result") == "ok"
            and rec.get("sync_version") == SYNC_VERSION)
```

(c) Replace the whole comment block above `ROOT_FILES` (from `# DELIBERATELY EXCLUDES config.yaml.` down to the line before `ROOT_FILES`) with:

```python
# Copied verbatim. config.yaml is NOT in this tuple: it is installed by
# copy_root_config(), which validates the declaration first -- a bad SOUL.md
# costs a persona, a bad config.yaml could cost the gateway its configuration.
```

(d) After `copy_root_files`, add:

```python
def _declared_root_config(staged: Path) -> tuple[bytes | None, str]:
    """Return (bytes, "") for a usable declared root config, else (None, reason)."""
    src = staged / "hermes" / "root" / ROOT_CONFIG
    if not src.is_file():
        return None, "is missing"
    try:
        data = src.read_bytes()
    except Exception as exc:
        return None, f"is unreadable ({type(exc).__name__})"
    try:
        import yaml  # in the image's venv; lazy, like warn_unserved_profiles
    except Exception as exc:
        return None, f"cannot be checked (PyYAML unavailable: {type(exc).__name__})"
    try:
        parsed = yaml.safe_load(data.decode("utf-8"))
    except Exception as exc:
        return None, f"is not valid UTF-8 YAML ({type(exc).__name__})"
    if not isinstance(parsed, dict) or not parsed:
        return None, "is not a non-empty mapping"
    return data, ""


def copy_root_config(staged: Path) -> bool:
    """Install hermes/root/config.yaml as HERMES_HOME/config.yaml. Git wins.

    plder is the ONLY source of the root config: the main container no longer
    mounts a ConfigMap over this path. Rules, each load-bearing:

    * A declaration that is missing, unreadable, not UTF-8 YAML, or not a
      non-empty mapping is REFUSED and the live file kept (returns False, so
      the record says "partial"). Config must never take the gateway down, and
      on a fresh volume with no live file stage2 then seeds its example config
      instead of the gateway booting on garbage.
    * Byte-identical live file: nothing is written (no mtime churn, no backup).
    * Otherwise the file is written to a temp file in HERMES_HOME and renamed
      over the target. NEVER written through: on the first boot after the mount
      is retired the target is the root-owned kubelet subPath stub, which uid
      10000 cannot open for writing but can replace, because /opt/data is a
      hermes-owned directory.
    * The replaced live file is kept at STATE_DIR/config.yaml.previous. Hermes
      itself writes config.yaml at runtime (tools/approval.py command
      allowlist, /model persistence, the dashboard); an applied sync reverts
      those edits, and this is where to recover one worth committing to plder.

    stage2-hook.sh later chowns the file and runs docker_config_migrate.py,
    which leaves it alone only while _config_version equals the image's latest
    (38 on v2026.8.19) -- plder CI enforces that. Never raises.
    """
    data, reason = _declared_root_config(staged)
    if data is None:
        log(f"WARNING declared root config.yaml {reason}; keeping the live config.yaml")
        return False
    dest = HERMES_HOME / ROOT_CONFIG
    try:
        if dest.is_file() and dest.read_bytes() == data:
            log("config.yaml already matches the declaration")
            return True
    except Exception:
        pass  # an unreadable live file (e.g. a 0600 root stub) is simply replaced
    tmp = HERMES_HOME / f".{ROOT_CONFIG}.profile-sync.tmp"
    saved = ""
    try:
        if dest.is_file():
            try:
                shutil.copyfile(dest, STATE_DIR / PREVIOUS_CONFIG)
                saved = f" (previous saved to {STATE_DIR / PREVIOUS_CONFIG})"
            except Exception as exc:
                log(f"WARNING could not save the previous config.yaml: {exc}")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o640)
        os.replace(tmp, dest)
        log(f"copied config.yaml{saved}")
        return True
    except Exception as exc:
        log(f"WARNING installing config.yaml failed; keeping the live config.yaml: "
            f"{type(exc).__name__}: {exc}")
        try:
            tmp.unlink()
        except Exception:
            pass
        return False
```

(e) In `warn_unserved_profiles`'s docstring, replace `the\n    declared mirror of the live ConfigMap.` with `the\n    same file copy_root_config installs as HERMES_HOME/config.yaml.`

(f) In `main()`, replace:

```python
        steps_ok = copy_root_files(STAGING) and steps_ok
```

with:

```python
        steps_ok = copy_root_files(STAGING) and steps_ok
        # Before install_profiles on purpose: every `hermes` CLI call below then
        # already reads the declared root config, not the one it replaces.
        steps_ok = copy_root_config(STAGING) and steps_ok
```

(g) In the `APPLIED.write_text(json.dumps({...}))` payload, add `"sync_version": SYNC_VERSION,` after `"image": IMAGE,`.

- [ ] **Step 5: Run the full suite**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest -q 2>&1 | tail -3
ls -d /c/Users/Pol/AppData/Local/hermes/.agent-config 2>&1 | tail -1
```

Expected: `0 failed` (about 70 passed), and `No such file or directory` for the live Desktop home.

- [ ] **Step 6: Commit (do not push)**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent/sync/sync.py infra/hermes-agent/sync/test_sync.py
git commit -m "hermes: install the root config.yaml from plder in profile-sync"
```

---

### Task 2: Pin the root config schema version in plder

**Files:**
- Modify (plder, master): `hermes/root/config.yaml`

**Interfaces:**
- Produces: a plder commit `ROOTREF` whose root config carries `_config_version: 38`. Task 3 pins it.

The ladder must be proven a no-op before the file can claim version 38. Pinning 38 asserts the file already has the v38 schema. If a migration would change a value, that change has to be adopted deliberately, not skipped.

- [ ] **Step 1 (controller): Rehearse the migration ladder in a throwaway pod**

Paste the throwaway-pod manifest with `P=p3-migrate-rehearsal`, then in the same invocation:

```bash
git -C /c/Users/Pol/projects/plder fetch -q && git -C /c/Users/Pol/projects/plder show origin/master:hermes/root/config.yaml | md5sum
git -C /c/Users/Pol/projects/plder show origin/master:hermes/root/config.yaml \
  | kubectl exec -i -n "$NS" "$P" -- sh -c 'mkdir -p /tmp/mh /tmp/home && cat > /tmp/mh/config.yaml && md5sum /tmp/mh/config.yaml'
kubectl exec -i -n "$NS" "$P" -- env HERMES_HOME=/tmp/mh /opt/hermes/.venv/bin/python - <<'PY'
import json, yaml
from hermes_cli.config import check_config_version, migrate_config
from hermes_cli.config_defaults import DEFAULT_CONFIG
p = "/tmp/mh/config.yaml"
before = yaml.safe_load(open(p, encoding="utf-8"))
print("version before:", check_config_version())
res = migrate_config(interactive=False, quiet=True)
after = yaml.safe_load(open(p, encoding="utf-8"))
print("version after:", check_config_version(), "| results:", json.dumps(res, default=str))
MISSING = object()
def at(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return MISSING
        d = d[k]
    return d
def walk(b, a, path=()):
    if isinstance(b, dict) and isinstance(a, dict):
        for k in sorted(set(b) | set(a), key=str):
            yield from walk(b.get(k, MISSING), a.get(k, MISSING), path + (k,))
    elif b != a:
        yield path, b, a
changed, defaults = [], []
for path, b, a in walk(before, after):
    if path == ("_config_version",):
        continue
    if b is MISSING and a == at(DEFAULT_CONFIG, path):
        defaults.append(".".join(map(str, path)))
    else:
        changed.append((".".join(map(str, path)), b if b is not MISSING else "<absent>", a if a is not MISSING else "<absent>"))
print("materialized defaults (runtime-identical, load_config merges them anyway):", defaults)
print("changed values:", changed)
print("VERDICT:", "PIN_38" if not changed else "STOP")
PY
```

Expected:
- The two md5 lines match.
- `version before: (0, 38)`, then `version after: (38, 38)`.
- `changed values: []`, then `VERDICT: PIN_38`.

**If `VERDICT: STOP`, stop the plan here** and show the operator the `changed values`. Each one is a migration the running config has never had. Adopting it changes agent behaviour, so it needs a decision, not a guess.

- [ ] **Step 2: Pull plder master**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git status --short
```

Expected: a clean tree.

- [ ] **Step 3: Append the pinned version, preserving the file's line endings**

```bash
cd /c/Users/Pol/projects/plder
python - <<'PY'
p = "hermes/root/config.yaml"
raw = open(p, "rb").read()
assert b"_config_version" not in raw, "already pinned"
eol = b"\r\n" if b"\r\n" in raw else b"\n"
block = [
    b"",
    b"# Schema version of this file. MUST equal the running image's latest",
    b"# (hermes_cli/config_defaults.py, 38 on v2026.8.19): stage2's boot-time",
    b"# migrator rewrites config.yaml whenever it is older, so a lower value makes",
    b"# the live file drift from this declaration on every boot, and no value at",
    b"# all keeps the [config-migrate] warning. CI checks it against the image.",
    b"# Bump it together with the image, after re-running the migration rehearsal.",
    b"_config_version: 38",
]
if not raw.endswith(eol):
    raw += eol
open(p, "wb").write(raw + eol.join(block) + eol)
PY
python -c "import yaml; c=yaml.safe_load(open('hermes/root/config.yaml', encoding='utf-8')); assert c['_config_version']==38 and c['secrets']['command']['override_existing'] is True; print('OK', sorted(c))"
```

Expected: `OK ['_config_version', 'approvals', 'desktop', 'gateway', 'model', 'secrets']`

- [ ] **Step 4: Commit**

```bash
cd /c/Users/Pol/projects/plder
git add hermes/root/config.yaml
git commit -m "hermes: pin the root config schema version to the image's 38"
git show HEAD:hermes/root/config.yaml | grep -c $'\r'   # expect 0: the blob is LF
git show --stat HEAD | tail -2                           # expect: 1 file, 8 insertions
```

- [ ] **Step 5 (controller): Push, then record `ROOTREF`**

Pushing is inert: nothing pins the new SHA yet, and CI does not exist yet.

```bash
cd /c/Users/Pol/projects/plder && git push origin master && git rev-parse --short HEAD
```

Record the short SHA as `ROOTREF`.

---

### Task 3: Retire the config.yaml ConfigMap mount (gitops-cluster)

**Files:**
- Modify: `infra/hermes-agent/configmap.yaml`: remove the `config.yaml` key
- Modify: `infra/hermes-agent/deployment.yaml`: remove the mount and the `config` volume, update the comment, pin `ROOTREF`

**Interfaces:**
- Consumes: `copy_root_config` and `SYNC_VERSION` (Task 1), `ROOTREF` (Task 2).
- Produces: a Deployment whose only root-config source is profile-sync, and an infrastructure-only `hermes-config` ConfigMap (`ssh_config`).

The mount, the ConfigMap key and the sync change ship in **one push**. They are one atomic change of who owns the file. If the mount stays, it masks the synced file. If the key stays, it becomes a mirror that drifts.

- [ ] **Step 1: Strip config.yaml from the ConfigMap**

In `infra/hermes-agent/configmap.yaml`, delete everything from the line `  # Mounted read-only at /opt/data/config.yaml. To change the model/provider,` down to and including the `command: 'for f in …; done'` line, so that `data:` is followed directly by the `ssh_config` comment. Then replace the `data:` line with:

```yaml
# Infrastructure only. The agent's root config.yaml is declared in Forgenn/plder
# (hermes/root/config.yaml) and installed onto the PVC by the profile-sync
# initContainer. Do not re-add a config.yaml key here: a copy in this repo is a
# mirror that drifts, and mounting it over /opt/data/config.yaml masks the
# synced file.
data:
```

- [ ] **Step 2: Remove the mount and the volume, update the comment**

In `infra/hermes-agent/deployment.yaml`:

(a) In the `hermes-agent` container's `volumeMounts`, delete:

```yaml
            - name: config
              mountPath: /opt/data/config.yaml
              subPath: config.yaml
              readOnly: true
```

(b) In `volumes`, delete:

```yaml
        - name: config
          configMap:
            name: hermes-config
```

(c) In the comment block above `- name: profile-sync`, after the first sentence (ending `…"reapply on every boot" pattern below.`), insert:

```yaml
        # It is also the ONLY source of the root config.yaml: sync.py installs
        # plder's hermes/root/config.yaml onto the PVC (refusing a bad one and
        # keeping the live file). Nothing mounts a ConfigMap over that path any
        # more -- do not add one back, it would silently mask the synced file.
```

(d) Pin the ref:

```bash
cd /c/Users/Pol/projects/gitops-check
ROOTREF=REPLACE_WITH_TASK_2_SHA
python - "$ROOTREF" <<'PY'
import re, sys
p = "infra/hermes-agent/deployment.yaml"
s = open(p, encoding="utf-8").read()
new, n = re.subn(r'(- name: AGENT_CONFIG_REF\n\s*value: )"[0-9a-f]+"', rf'\1"{sys.argv[1]}"', s)
assert n == 1, f"expected exactly one AGENT_CONFIG_REF value, replaced {n}"
open(p, "w", encoding="utf-8", newline="\n").write(new)
PY
grep -A1 "name: AGENT_CONFIG_REF" infra/hermes-agent/deployment.yaml
```

- [ ] **Step 3: Verify the build**

```bash
cd /c/Users/Pol/projects/gitops-check
read -r -d '' CHECK <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if d]
cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "hermes-config")
assert sorted(cm["data"]) == ["ssh_config"], sorted(cm["data"])
spec = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
vols = {v["name"] for v in spec["volumes"]}
assert "config" not in vols, "config volume still present"
for c in spec["initContainers"] + spec["containers"]:
    for m in c.get("volumeMounts", []):
        assert m["mountPath"] != "/opt/data/config.yaml", (c["name"], m)
        assert m["name"] in vols, (c["name"], m["name"])
sync = next(c for c in spec["initContainers"] if c["name"] == "profile-sync")
print("OK: hermes-config is infra-only; no config.yaml mount; AGENT_CONFIG_REF =",
      [e["value"] for e in sync["env"] if e["name"] == "AGENT_CONFIG_REF"])
PY
kubectl kustomize infra/hermes-agent | python -c "$CHECK"
```

Expected: `OK: hermes-config is infra-only; no config.yaml mount; AGENT_CONFIG_REF = ['<ROOTREF>']`

- [ ] **Step 4: Commit (do not push)**

```bash
git add infra/hermes-agent/configmap.yaml infra/hermes-agent/deployment.yaml
git commit -m "hermes: retire the root config.yaml configmap mount, deploy plder $ROOTREF"
```

- [ ] **Step 5 (controller): Pre-flight the new sync against a stub it cannot write through**

This rehearses the first production boot with the real image, a real clone at `ROOTREF`, and the ssh config and read key. The target file cannot be opened for writing, as the kubelet stub will be. Paste the throwaway-pod manifest with `P=p3-sync-rehearsal`, then:

```bash
ROOTREF=REPLACE_WITH_TASK_2_SHA
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
tar cf - sync.py cron_upsert.py | kubectl exec -i -n "$NS" "$P" -- \
  sh -c 'mkdir -p /tmp/sync /tmp/home /tmp/stg /tmp/fh && tar xf - -C /tmp/sync && md5sum /tmp/sync/*.py'
md5sum sync.py cron_upsert.py
kubectl get configmap hermes-config -n "$NS" -o jsonpath='{.data.ssh_config}' \
  | kubectl exec -i -n "$NS" "$P" -- sh -c 'cat > /tmp/ssh_config'
kubectl get secret hermes-secrets -n "$NS" -o jsonpath='{.data.PLDER_DEPLOY_KEY_READ}' | base64 -d \
  | kubectl exec -i -n "$NS" "$P" -- sh -c 'umask 077; cat > /tmp/k'
echo "declared md5:"; git -C /c/Users/Pol/projects/plder show "$ROOTREF:hermes/root/config.yaml" | md5sum
kubectl exec -i -n "$NS" "$P" -- env ROOTREF="$ROOTREF" sh -s <<'SH'
run_sync() {
  env HERMES_HOME=/tmp/fh AGENT_CONFIG_REF="$1" AGENT_IMAGE=nousresearch/hermes-agent:v2026.8.19 PYTHONPATH=/tmp/sync \
    GIT_SSH_COMMAND="ssh -F /tmp/ssh_config -i /tmp/k -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/tmp/stg \
    /opt/hermes/.venv/bin/python -c 'import pathlib, sync; sync.STAGING = pathlib.Path("/tmp/stg"); raise SystemExit(sync.main())' </dev/null 2>&1
  echo "exit=$?"
}
echo "== 1. stub that cannot be written through"
printf 'stale: stub\n' > /tmp/fh/config.yaml && chmod 0444 /tmp/fh/config.yaml
/opt/hermes/.venv/bin/python -c 'open("/tmp/fh/config.yaml", "a")' </dev/null 2>&1 | tail -1
echo "== 2. first sync"
run_sync "$ROOTREF"
ls -l /tmp/fh/config.yaml; md5sum /tmp/fh/config.yaml
echo "previous:"; cat /tmp/fh/.agent-config/config.yaml.previous
cat /tmp/fh/.agent-config/applied; echo
echo "== 3. boot-time migrator"
env HERMES_HOME=/tmp/fh /opt/hermes/.venv/bin/python /opt/hermes/scripts/docker_config_migrate.py </dev/null 2>&1; echo "migrate exit=$?"
md5sum /tmp/fh/config.yaml; ls /tmp/fh | grep -c 'config.yaml.bak' || true
echo "== 4. same ref again"
run_sync "$ROOTREF"
echo "== 5. clone failure"
run_sync 0000000
md5sum /tmp/fh/config.yaml
cat /tmp/fh/.agent-config/applied; echo
SH
```

Expected:
1. The local and in-pod script md5s match, and the control prints `PermissionError: [Errno 13] Permission denied: '/tmp/fh/config.yaml'`.
2. The log shows these lines, in order:
   - `copied SOUL.md`
   - `copied config.yaml (previous saved to /tmp/fh/.agent-config/config.yaml.previous)`
   - `cron: 0 job(s) in /tmp/fh/cron/jobs.json`
   - `installed profile monitor`
   - `cron: 1 job(s) in /tmp/fh/profiles/monitor/cron/jobs.json`
   - `applied ref <ROOTREF> (result: ok)`
   - `exit=0`

   Skills WARNING lines are tolerated. The file shows `-rw-r-----`, its md5 equals the declared md5, `previous:` shows `stale: stub`, and the record has `"sync_version": 2` and `"result": "ok"`.
3. The migrator prints nothing, `migrate exit=0`, the md5 is unchanged, and the backup count is `0`.
4. `ref <ROOTREF> + image already applied; skipping`.
5. `WARNING clone failed: …`, `falling back to last-good tree`, `config.yaml already matches the declaration`, `applied ref <ROOTREF> (result: ok)`. The md5 is unchanged.

**If any expectation fails, STOP.** Fix it in Task 1 and re-run. Do not deploy.

---

### Task 4: Deploy the single-source root config and verify it live (controller)

**Files:** none (merge + verification).

**Gates:**
- Task 3 Step 5 passed.
- volsync within 36h.
- Not 08:45–09:15 UTC.

**Rollback (if a probe fails):** `git revert -m 1 <merge sha> && git push origin main`. The ConfigMap key and mount come back and mask the synced file. The old `sync.py` ignores `sync_version`, so the revert is clean.

- [ ] **Step 1: Snapshot what must not change**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c \
  'cd /opt/data/profiles/monitor && md5sum SOUL.md config.yaml assets/avatar.png profile.yaml && find memories -type f -exec md5sum {} \; | sort'
kubectl exec -n hermes $POD -c hermes-agent -- /opt/hermes/.venv/bin/python -c \
  "import json; print(json.load(open('/opt/data/gateway_state.json'))['served_profiles'])"
kubectl get replicationsource hermes-data -n hermes -o jsonpath='{.status.lastSyncTime}{"\n"}'; date -u +%H:%M
```

Save the output.

- [ ] **Step 2: Merge and push**

```bash
cd /c/Users/Pol/projects/gitops-check
git checkout main && git pull --ff-only
git merge --no-ff hermes-config-single-source -m "hermes: make plder the single source of the root config.yaml"
git push origin main
GITOPS_SHA=$(git rev-parse HEAD); echo "$GITOPS_SHA"
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} conditions={.status.conditions}{"\n"}'
kubectl logs -n argocd statefulset/argocd-application-controller --since=15m | grep -i hermes-agent | grep -iE "ComparisonError|level=error" | tail -5
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
```

Expected: `Synced Healthy conditions=`, an empty condition list, no controller error lines, and a completed rollout.

- [ ] **Step 3: Read profile-sync's log and the record**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl get pod -n hermes $POD -o jsonpath='{.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}{"\n"}'
kubectl logs -n hermes $POD -c profile-sync
kubectl exec -n hermes $POD -c hermes-agent -- cat /opt/data/.agent-config/applied
```

Expected:
- The ref is `<ROOTREF>`.
- The log contains `copied config.yaml (previous saved to /opt/data/.agent-config/config.yaml.previous)`, `installed profile monitor` and `applied ref <ROOTREF> (result: ok)`, with no `WARNING` other than skills.
- The record has `"sync_version": 2`.

A Ready pod is not proof. These lines are.

- [ ] **Step 4: Verify the file, its ownership, the mount, the migrator, the ConfigMap**

```bash
export MSYS_NO_PATHCONV=1
ROOTREF=REPLACE_WITH_TASK_2_SHA
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c \
  'md5sum /opt/data/config.yaml; ls -l /opt/data/config.yaml; echo "mounts: $(grep -c " /opt/data/config.yaml " /proc/self/mountinfo)"; ls /opt/data | grep -c "config.yaml.bak" ; echo "--- replaced stub (head):"; head -5 /opt/data/.agent-config/config.yaml.previous'
git -C /c/Users/Pol/projects/plder show "$ROOTREF:hermes/root/config.yaml" | md5sum
echo "config-migrate lines: $(kubectl logs -n hermes $POD -c hermes-agent | grep -c 'config-migrate')"
kubectl get configmap hermes-config -n hermes -o jsonpath='{.data}' | python -c "import json,sys; print(sorted(json.load(sys.stdin)))"
```

Expected:
- The two md5s are equal.
- `-rw-r----- 1 hermes hermes` (stage2 chowned it).
- `mounts: 0` and a backup count of `0`.
- `config-migrate lines: 0`, which is the warning that appeared on every boot until now.
- `['ssh_config']`.

- [ ] **Step 5: Verify the served profiles and the monitor are untouched**

Re-run Step 1's commands. Expected: `['default', 'monitor']`, and every hash identical to the snapshot.

- [ ] **Step 6: Agent-mode probes in every served profile**

This is a config-ownership change under multiplex, so plan 2's rule applies. Run **Create** from the agent-probe kit, wait until at least 2 minutes after the printed fire time, run **Check**, then **Remove**.

Expected: both homes `last_status ok | last_error None | response ok: True`. On failure, run the rollback above.

- [ ] **Step 7: A restart is a no-op and keeps the config**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl delete pod -n hermes $POD
kubectl rollout status deployment/hermes-agent -n hermes --timeout=600s
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl logs -n hermes $POD -c profile-sync
kubectl exec -n hermes $POD -c hermes-agent -- md5sum /opt/data/config.yaml
```

Expected: only `ref <ROOTREF> + image already applied; skipping`, and the md5 unchanged.

---

## Part B: CI in plder

### Task 5: Config validator (TDD, plder)

**Files:**
- Create: `plder/hermes/ci/validate.py`
- Create: `plder/hermes/ci/test_validate.py`
- Modify: `plder/.gitignore`

**Interfaces:**
- Produces: `validate(hermes: Path) -> list[str]` (one `"<repo-relative path>: <problem>"` per error), `deliver_error(value) -> str | None`, `main(argv) -> int` (0 valid, 1 invalid, 2 usage), and the constants `SECRETS_HELPER`, `RUNTIME_FIELDS`, `DELIVERY_PLATFORMS`. Task 7's workflow runs `python hermes/ci/validate.py hermes`.

- [ ] **Step 1: Branch**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git checkout -b hermes-ci
printf '__pycache__/\n.pytest_cache/\n' >> .gitignore
```

- [ ] **Step 2: Write the failing tests**

```python
# hermes/ci/test_validate.py
"""Hermetic tests for validate.py: every tree is built under tmp_path."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

import validate as v


def _secrets():
    return {"command": {"enabled": True, "override_existing": True, "command": v.SECRETS_HELPER}}


def _job(jid, **over):
    job = {"id": jid, "name": f"job {jid}", "managed_by": "plder", "prompt": "do the thing",
           "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
           "enabled": True, "deliver": "telegram:7850573137:5332"}
    job.update(over)
    return job


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) if path.suffix == ".json" else yaml.safe_dump(data, sort_keys=False)
    path.write_text(text, encoding="utf-8")


def _load(path: Path):
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _edit(path: Path, fn) -> None:
    data = _load(path)
    fn(data)
    _dump(path, data)


def _assert_error(tree: Path, *needles: str) -> None:
    errors = v.validate(tree)
    assert any(all(n in e for n in needles) for e in errors), f"no error with {needles!r} in {errors!r}"


def _add_profile(h: Path, name: str, jobs) -> None:
    d = h / "profiles" / name
    _dump(d / "distribution.yaml", {"name": name, "version": "0.1.0", "distribution_owned": ["SOUL.md", "config.yaml"]})
    (d / "SOUL.md").write_text(f"{name} soul\n", encoding="utf-8")
    _dump(d / "config.yaml", {"secrets": _secrets(), "cron": {"preflight": False}, "_config_version": 38})
    _dump(d / "cron" / "jobs.json", {"jobs": jobs})


@pytest.fixture
def tree(tmp_path):
    h = tmp_path / "hermes"
    _dump(h / "root" / "config.yaml", {
        "model": {"default": "deepseek/deepseek-v4-flash-0731", "provider": "openrouter"},
        "gateway": {"multiplex_profiles": True, "multiplex_profile_allowlist": ["monitor"],
                    "profile_routes": [{"name": "monitor-topic", "platform": "telegram",
                                        "chat_id": "7850573137", "thread_id": "5332", "profile": "monitor"}]},
        "secrets": _secrets(), "_config_version": 38})
    (h / "root" / "SOUL.md").write_text("root soul\n", encoding="utf-8")
    _dump(h / "root" / "cron" / "jobs.json", {"jobs": []})
    m = h / "profiles" / "monitor"
    _dump(m / "distribution.yaml", {"name": "monitor", "version": "0.1.0",
                                     "distribution_owned": ["SOUL.md", "config.yaml", "assets/"]})
    (m / "SOUL.md").write_text("monitor soul\n", encoding="utf-8")
    (m / "assets").mkdir()
    (m / "assets" / "avatar.png").write_bytes(b"\x89PNG")
    _dump(m / "config.yaml", {"model": {"default": "x"}, "secrets": _secrets(),
                               "cron": {"preflight": False}, "_config_version": 38})
    _dump(m / "cron" / "jobs.json", {"jobs": [_job("6270d3f018f2")]})
    return h


ROOT_CFG = "root/config.yaml"
MON_CFG = "profiles/monitor/config.yaml"
MON_JOBS = "profiles/monitor/cron/jobs.json"


def test_valid_tree_has_no_errors(tree):
    assert v.validate(tree) == []


def test_unparseable_yaml_is_reported(tree):
    (tree / MON_CFG).write_text("model: [unclosed\n", encoding="utf-8")
    _assert_error(tree, "hermes/profiles/monitor/config.yaml", "does not parse as YAML")


def test_unparseable_json_is_reported(tree):
    (tree / "root" / "cron" / "jobs.json").write_text("{not json", encoding="utf-8")
    _assert_error(tree, "hermes/root/cron/jobs.json", "does not parse as JSON")


@pytest.mark.parametrize("rel", ["root/config.yaml", "root/SOUL.md", "root/cron/jobs.json"])
def test_root_files_are_required(tree, rel):
    (tree / rel).unlink()
    _assert_error(tree, f"hermes/{rel}", "is missing")


def test_invalid_profile_name_is_reported(tree):
    shutil.copytree(tree / "profiles" / "monitor", tree / "profiles" / "Bad_Name")
    _assert_error(tree, "invalid profile name 'Bad_Name'")


def test_profile_without_a_manifest_is_reported(tree):
    (tree / "profiles" / "monitor" / "distribution.yaml").unlink()
    _assert_error(tree, "hermes/profiles/monitor/distribution.yaml", "is missing")


def test_manifest_name_must_match_the_directory(tree):
    _edit(tree / "profiles" / "monitor" / "distribution.yaml", lambda m: m.update(name="monitr"))
    _assert_error(tree, "name must be 'monitor'")


def test_manifest_must_declare_its_owned_paths(tree):
    _edit(tree / "profiles" / "monitor" / "distribution.yaml", lambda m: m.pop("distribution_owned"))
    _assert_error(tree, "distribution_owned must be declared")


@pytest.mark.parametrize("entry, needle", [
    ("cron/", "cron is merged"),
    ("cron/jobs.json", "cron is merged"),
    ("profile.yaml", "Bot Mode owns it"),
    ("skills/", "only skills/custom/ is git-owned"),
    ("skills/research", "only skills/custom/ is git-owned"),
    ("memories/", "user-owned"),
    ("mcp.json", "does not exist in the profile"),
])
def test_manifest_rejects_unsafe_or_absent_owned_paths(tree, entry, needle):
    _edit(tree / "profiles" / "monitor" / "distribution.yaml", lambda m: m["distribution_owned"].append(entry))
    _assert_error(tree, needle)


def test_manifest_may_own_custom_skills(tree):
    (tree / "profiles" / "monitor" / "skills" / "custom" / "x").mkdir(parents=True)
    _edit(tree / "profiles" / "monitor" / "distribution.yaml", lambda m: m["distribution_owned"].append("skills/custom/"))
    assert v.validate(tree) == []


@pytest.mark.parametrize("config_path", [ROOT_CFG, MON_CFG])
@pytest.mark.parametrize("mutate, needle", [
    (lambda c: c.pop("secrets"), "secrets.command is missing"),
    (lambda c: c["secrets"]["command"].update(enabled=False), "secrets.command.enabled must be true"),
    (lambda c: c["secrets"]["command"].update(override_existing=False), "override_existing must be true"),
    (lambda c: c["secrets"]["command"].update(command=v.SECRETS_HELPER.replace("\\n", "\n")), "not the canonical"),
    (lambda c: c.pop("_config_version"), "_config_version must be pinned"),
], ids=["no-secrets", "disabled", "no-override", "helper-typo", "no-version"])
def test_every_served_config_needs_credentials_and_a_pinned_version(tree, config_path, mutate, needle):
    _edit(tree / config_path, mutate)
    _assert_error(tree, f"hermes/{config_path}", needle)


def test_secondary_profile_needs_preflight_off(tree):
    _edit(tree / MON_CFG, lambda c: c.pop("cron"))
    _assert_error(tree, "hermes/profiles/monitor/config.yaml", "cron.preflight must be false")


def test_allowlisted_profile_must_be_declared(tree):
    _edit(tree / ROOT_CFG, lambda c: c["gateway"]["multiplex_profile_allowlist"].append("ghost"))
    _assert_error(tree, "names 'ghost', which is not a declared profile")


def test_allowlist_needs_multiplex_on(tree):
    _edit(tree / ROOT_CFG, lambda c: c["gateway"].update(multiplex_profiles=False))
    _assert_error(tree, "multiplex_profiles must be true")


def test_route_target_must_be_allowlisted(tree):
    _edit(tree / ROOT_CFG, lambda c: c["gateway"]["profile_routes"][0].update(profile="shopper"))
    _assert_error(tree, "routes to 'shopper'")


def test_route_ids_must_be_strings(tree):
    _edit(tree / ROOT_CFG, lambda c: c["gateway"]["profile_routes"][0].update(thread_id=5332))
    _assert_error(tree, "thread_id must be a quoted string")


def test_job_ids_are_unique_across_homes(tree):
    _dump(tree / "root" / "cron" / "jobs.json", {"jobs": [_job("6270d3f018f2")]})
    _assert_error(tree, "job 6270d3f018f2", "also declared")


@pytest.mark.parametrize("mutate, needle", [
    (lambda j: j.pop("managed_by"), 'managed_by must be "plder"'),
    (lambda j: j.update(managed_by="me"), 'managed_by must be "plder"'),
    (lambda j: j.pop("name"), "name is required"),
    (lambda j: j.pop("prompt"), "prompt is required"),
    (lambda j: j.update(no_agent=True), "needs a script"),
    (lambda j: j.pop("schedule"), "schedule.kind must be one of"),
    (lambda j: j["schedule"].update(expr="0 9 * *"), "5-field cron expression"),
    (lambda j: j.update(enabled="yes"), "enabled must be true or false"),
    (lambda j: j.update(state="running"), "state may only be declared"),
    (lambda j: j.update(next_run_at="2026-09-15T09:00:00+00:00"), "next_run_at is scheduler-owned"),
    (lambda j: j.pop("deliver"), "deliver is required"),
    (lambda j: j.update(deliver="carrier-pigeon"), "unknown platform"),
    (lambda j: j.update(id="../escape"), "id must be a string matching"),
])
def test_declared_job_schema(tree, mutate, needle):
    _edit(tree / MON_JOBS, lambda d: mutate(d["jobs"][0]))
    _assert_error(tree, "hermes/profiles/monitor/cron/jobs.json", needle)


@pytest.mark.parametrize("value", ["local", "origin", "all", "telegram", "telegram:7850573137",
                                   "telegram:7850573137:5332", "telegram:-1001234567:17", "origin,telegram:1:2"])
def test_valid_deliver_targets(value):
    assert v.deliver_error(value) is None


@pytest.mark.parametrize("value", ["", None, 42, "carrier-pigeon", "telegram:", "telegram::5",
                                   "telegram:1:", "telegram: 12", "local,,telegram"])
def test_invalid_deliver_targets(value):
    assert v.deliver_error(value)


def test_enabled_job_in_an_unserved_profile_is_reported(tree):
    _add_profile(tree, "shopper", [_job("aaaaaaaaaaaa")])
    _assert_error(tree, "profile 'shopper' is not in gateway.multiplex_profile_allowlist")


def test_disabled_job_in_an_unserved_profile_is_fine(tree):
    _add_profile(tree, "shopper", [_job("aaaaaaaaaaaa", enabled=False)])
    assert v.validate(tree) == []


def test_main_exit_codes(tree, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert v.main([str(tree)]) == 0
    assert "OK:" in capsys.readouterr().out
    (tree / "root" / "SOUL.md").unlink()
    assert v.main([str(tree)]) == 1
    assert "ERROR hermes/root/SOUL.md: is missing" in capsys.readouterr().out
    assert v.main([str(tmp_path / "nope")]) == 2
    assert v.main([]) == 2


def test_main_emits_github_annotations_in_actions(tree, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    (tree / "root" / "SOUL.md").unlink()
    assert v.main([str(tree)]) == 1
    assert "::error::hermes/root/SOUL.md: is missing" in capsys.readouterr().out
```

- [ ] **Step 3: Run to verify failure**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest -q hermes/ci 2>&1 | tail -3
```

Expected: collection ERROR, `ModuleNotFoundError: No module named 'validate'`.

- [ ] **Step 4: Implement `hermes/ci/validate.py`**

```python
#!/usr/bin/env python3
"""Validate the declared Hermes agent config under hermes/ before it can deploy.

    python hermes/ci/validate.py hermes

Prints one line per problem and exits 1, or prints OK and exits 0 (2 = usage).
Needs only PyYAML, so it runs the same on the workstation and in CI.

Every rule mirrors a way a deploy otherwise goes wrong, as applied by
infra/hermes-agent/sync/sync.py in Forgenn/gitops-cluster and read by Hermes
v2026.8.19. Keep them in step when either changes. Checks that need the Hermes
image itself are in image_checks.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MANAGED_BY = "plder"

# The credentials helper every served profile needs under gateway multiplex
# (gitops-cluster docs/plans/2026-09-13-hermes-monitor-bot.md). Compared exactly:
# YAML quoting of $, " and \n is where a transcription slip silently breaks it.
SECRETS_HELPER = r'for f in /etc/hermes-profile-secrets/*; do IFS= read -r v < "$f" || [ -n "$v" ]; printf "%s=%s\n" "${f##*/}" "$v"; done'

# cron_upsert.RUNTIME_FIELDS in gitops-cluster: live always wins, so declaring one is a mistake.
RUNTIME_FIELDS = ("next_run_at", "last_run_at", "last_status", "last_error", "failure_streak",
                  "monitor_state", "paused_at", "paused_reason", "created_at")
# cron/scheduler.py _KNOWN_DELIVERY_PLATFORMS in hermes v2026.8.19.
DELIVERY_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal", "matrix", "mattermost",
    "homeassistant", "dingtalk", "feishu", "wecom", "wecom_callback", "weixin", "sms",
    "email", "webhook", "bluebubbles", "qqbot", "yuanbao",
})
DELIVERY_TOKENS = frozenset({"local", "origin", "all"})
SCHEDULE_KINDS = frozenset({"cron", "interval", "once"})
DECLARABLE_STATES = frozenset({"scheduled", "paused"})
# profile_distribution.USER_OWNED_EXCLUDE: never git-owned.
USER_OWNED = frozenset({"memories", "sessions", "logs", "workspace", "home", "state.db",
                        "auth.json", ".env", "backups", "cache", "local"})
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache"})

_UNPARSED = object()
Err = Callable[[Path, str], None]


def deliver_error(value: Any) -> str | None:
    """None when `deliver` is a target Hermes can resolve, else the reason."""
    if not isinstance(value, str) or not value.strip():
        return "deliver must be a non-empty string"
    for part in (p.strip() for p in value.split(",")):
        if not part:
            return f"deliver {value!r} has an empty target"
        if part in DELIVERY_TOKENS:
            continue
        platform, sep, rest = part.partition(":")
        if platform not in DELIVERY_PLATFORMS:
            return f"deliver target {part!r}: unknown platform {platform!r}"
        if sep and (not rest or rest.startswith(":") or rest.endswith(":")
                    or any(c.isspace() for c in rest)):
            return (f"deliver target {part!r} is malformed "
                    "(expected platform, platform:chat_id or platform:chat_id:thread_id)")
    return None


def _parse_all(hermes: Path, err: Err) -> dict[Path, Any]:
    parsed: dict[Path, Any] = {}
    for path in sorted(hermes.rglob("*")):
        if not path.is_file() or SKIP_DIRS.intersection(path.parts):
            continue
        suffix = path.suffix.lower()
        if suffix not in (".yaml", ".yml", ".json"):
            continue
        kind = "JSON" if suffix == ".json" else "YAML"
        try:
            text = path.read_text(encoding="utf-8")
            parsed[path] = json.loads(text) if kind == "JSON" else yaml.safe_load(text)
        except Exception as exc:
            detail = (str(exc).strip().splitlines() or [""])[0]
            err(path, f"does not parse as {kind}: {type(exc).__name__}: {detail}")
    return parsed


def _mapping(path: Path, parsed: dict, err: Err) -> dict | None:
    if not path.is_file():
        err(path, "is missing")
        return None
    value = parsed.get(path, _UNPARSED)
    if value is _UNPARSED:
        return None  # the parse error is already reported
    if not isinstance(value, dict) or not value:
        err(path, "must be a non-empty mapping")
        return None
    return value


def _check_manifest(d: Path, manifest: dict, err: Err) -> None:
    path = d / "distribution.yaml"
    if manifest.get("name") != d.name:
        err(path, f"name must be {d.name!r} (the directory name), got {manifest.get('name')!r}")
    owned = manifest.get("distribution_owned")
    if owned is None:
        err(path, "distribution_owned must be declared: the default list owns cron/ and skills/, "
                  "which would be overwritten on every sync")
        return
    if not isinstance(owned, list) or not all(isinstance(e, str) and e.strip("/ ") for e in owned):
        err(path, "distribution_owned must be a list of relative paths")
        return
    for raw in owned:
        entry = raw.strip().strip("/")
        top = entry.split("/", 1)[0]
        if top == "cron":
            err(path, f"distribution_owned must not list {raw!r}: cron is merged by the sync's upsert, never copied")
        elif entry == "profile.yaml":
            err(path, "distribution_owned must not list profile.yaml: Bot Mode owns it")
        elif top == "skills" and entry != "skills/custom" and not entry.startswith("skills/custom/"):
            err(path, f"distribution_owned must not list {raw!r}: only skills/custom/ is git-owned; "
                      "bundled skills belong to skills_sync")
        elif top in USER_OWNED:
            err(path, f"distribution_owned must not list {raw!r}: {top} is user-owned data")
        elif not (d / entry).exists():
            err(path, f"distribution_owned lists {raw!r}, which does not exist in the profile")


def _check_served_config(path: Path, cfg: dict, *, secondary: bool, err: Err) -> None:
    secrets = cfg.get("secrets")
    command = secrets.get("command") if isinstance(secrets, dict) else None
    if not isinstance(command, dict):
        err(path, "secrets.command is missing: under gateway multiplex this profile would have no LLM credentials")
    else:
        if command.get("enabled") is not True:
            err(path, "secrets.command.enabled must be true")
        if command.get("override_existing") is not True:
            err(path, "secrets.command.override_existing must be true")
        if command.get("command") != SECRETS_HELPER:
            err(path, "secrets.command.command is not the canonical file-reading helper "
                      "(check the YAML quoting of $, \" and \\n)")
    version = cfg.get("_config_version")
    if not isinstance(version, int) or isinstance(version, bool):
        err(path, "_config_version must be pinned to an integer (image_checks.py checks it equals the image's latest)")
    if secondary:
        cron = cfg.get("cron")
        if not isinstance(cron, dict) or cron.get("preflight") is not False:
            err(path, "cron.preflight must be false: a secondary profile's scope holds no Telegram token, "
                      "so preflight blocks its deliveries")


def _check_gateway(path: Path, cfg: dict, profiles: set[str], err: Err) -> set[str]:
    gateway = cfg.get("gateway", {})
    if not isinstance(gateway, dict):
        err(path, "gateway must be a mapping")
        return set()
    allow = gateway.get("multiplex_profile_allowlist", [])
    if not isinstance(allow, list) or not all(isinstance(n, str) for n in allow):
        err(path, "gateway.multiplex_profile_allowlist must be a list of profile names")
        allow = []
    for name in allow:
        if name not in profiles:
            err(path, f"gateway.multiplex_profile_allowlist names {name!r}, which is not a declared profile under hermes/profiles/")
    if allow and gateway.get("multiplex_profiles") is not True:
        err(path, "gateway.multiplex_profiles must be true when an allowlist is declared")
    routes = gateway.get("profile_routes", [])
    if not isinstance(routes, list):
        err(path, "gateway.profile_routes must be a list")
        routes = []
    for i, route in enumerate(routes):
        where = f"gateway.profile_routes[{i}]"
        if not isinstance(route, dict):
            err(path, f"{where} must be a mapping")
            continue
        if route.get("profile") not in allow:
            err(path, f"{where} routes to {route.get('profile')!r}, which is not in gateway.multiplex_profile_allowlist")
        if not isinstance(route.get("platform"), str) or not route.get("platform"):
            err(path, f"{where}.platform is required")
        for key in ("chat_id", "thread_id"):
            if key in route and not isinstance(route[key], str):
                err(path, f"{where}.{key} must be a quoted string: routing compares strings")
    return set(allow)


def _check_jobs(path: Path, parsed: dict, home: str, served: bool, seen: dict, err: Err) -> None:
    if not path.is_file():
        return
    doc = parsed.get(path, _UNPARSED)
    if doc is _UNPARSED:
        return
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), list):
        err(path, 'must be a mapping with a "jobs" list')
        return
    for i, job in enumerate(doc["jobs"]):
        where = f"jobs[{i}]"
        if not isinstance(job, dict):
            err(path, f"{where} must be a mapping")
            continue
        jid = job.get("id")
        if isinstance(jid, str) and JOB_ID_RE.match(jid):
            where = f"job {jid}"
            if jid in seen:
                err(path, f"{where}: id is also declared in {seen[jid]}; ids must be unique across every home")
            else:
                seen[jid] = home
        else:
            err(path, f"{where}: id must be a string matching {JOB_ID_RE.pattern}")
        if job.get("managed_by") != MANAGED_BY:
            err(path, f'{where}: managed_by must be "{MANAGED_BY}"')
        if not isinstance(job.get("name"), str) or not job["name"].strip():
            err(path, f"{where}: name is required")
        schedule = job.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") not in SCHEDULE_KINDS:
            err(path, f"{where}: schedule.kind must be one of {sorted(SCHEDULE_KINDS)}")
        elif schedule["kind"] == "cron" and (not isinstance(schedule.get("expr"), str)
                                             or len(schedule["expr"].split()) != 5):
            err(path, f"{where}: schedule.expr must be a 5-field cron expression")
        if job.get("no_agent") is True:
            if not isinstance(job.get("script"), str) or not job["script"].strip():
                err(path, f"{where}: a no_agent job needs a script")
        elif not isinstance(job.get("prompt"), str) or not job["prompt"].strip():
            err(path, f"{where}: prompt is required (or no_agent: true with a script)")
        if "enabled" in job and not isinstance(job["enabled"], bool):
            err(path, f"{where}: enabled must be true or false")
        if "state" in job and job["state"] not in DECLARABLE_STATES:
            err(path, f"{where}: state may only be declared as {sorted(DECLARABLE_STATES)}")
        for field in RUNTIME_FIELDS:
            if field in job:
                err(path, f"{where}: {field} is scheduler-owned and must not be declared")
        if "deliver" not in job:
            err(path, f"{where}: deliver is required")
        else:
            problem = deliver_error(job["deliver"])
            if problem:
                err(path, f"{where}: {problem}")
        if not served and job.get("enabled", True) is not False:
            err(path, f"{where}: enabled, but profile {home!r} is not in gateway.multiplex_profile_allowlist, "
                      "so it would never run")


def validate(hermes: Path) -> list[str]:
    hermes = Path(hermes)
    errors: list[str] = []

    def err(path: Path, msg: str) -> None:
        try:
            rel = path.relative_to(hermes.parent).as_posix()
        except ValueError:
            rel = path.as_posix()
        errors.append(f"{rel}: {msg}")

    parsed = _parse_all(hermes, err)

    root = hermes / "root"
    root_cfg = _mapping(root / "config.yaml", parsed, err)
    if not (root / "SOUL.md").is_file():
        err(root / "SOUL.md", "is missing")
    if not (root / "cron" / "jobs.json").is_file():
        err(root / "cron" / "jobs.json", 'is missing: keep {"jobs": []} so the sync can still retire root jobs')

    profiles: dict[str, Path] = {}
    profiles_dir = hermes / "profiles"
    if profiles_dir.is_dir():
        for d in sorted(p for p in profiles_dir.iterdir() if p.is_dir()):
            if not PROFILE_NAME_RE.match(d.name):
                err(d, f"invalid profile name {d.name!r} (must match {PROFILE_NAME_RE.pattern})")
                continue
            profiles[d.name] = d
            manifest = _mapping(d / "distribution.yaml", parsed, err)
            if manifest is not None:
                _check_manifest(d, manifest, err)

    if root_cfg is not None:
        _check_served_config(root / "config.yaml", root_cfg, secondary=False, err=err)
    for d in profiles.values():
        cfg = _mapping(d / "config.yaml", parsed, err)
        if cfg is not None:
            _check_served_config(d / "config.yaml", cfg, secondary=True, err=err)

    allowlist = _check_gateway(root / "config.yaml", root_cfg or {}, set(profiles), err)

    seen: dict[str, str] = {}
    _check_jobs(root / "cron" / "jobs.json", parsed, "root", True, seen, err)
    for name, d in profiles.items():
        _check_jobs(d / "cron" / "jobs.json", parsed, name, name in allowlist, seen, err)
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: validate.py <path to the hermes/ directory>", file=sys.stderr)
        return 2
    hermes = Path(args[0])
    if not hermes.is_dir():
        print(f"error: {hermes} is not a directory", file=sys.stderr)
        return 2
    errors = validate(hermes)
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "ERROR "
    for line in errors:
        print(f"{prefix}{line}")
    if errors:
        print(f"{len(errors)} problem(s) in {hermes}")
        return 1
    print(f"OK: {hermes} is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests, then validate the real tree**

```bash
cd /c/Users/Pol/projects/plder
python -m pytest -q hermes/ci 2>&1 | tail -3
python hermes/ci/validate.py hermes; echo "exit=$?"
```

Expected: `0 failed`, then `OK: hermes is valid` and `exit=0`. A root `_config_version` error means Task 2 is not on this branch: rebase on master.

- [ ] **Step 6: Commit**

```bash
git add .gitignore hermes/ci/validate.py hermes/ci/test_validate.py
git commit -m "ci: add hermes config validator"
```

---

### Task 6: AGENT_CONFIG_REF bump helper (TDD, plder)

**Files:**
- Create: `plder/hermes/ci/bump_ref.py`
- Create: `plder/hermes/ci/test_bump_ref.py`

**Interfaces:**
- Produces:
  - `read_ref(text) -> str`
  - `set_ref(text, ref) -> tuple[str, bool]`
  - `RefError(ValueError)`
  - CLI `bump_ref.py get <file>` prints the ref.
  - CLI `bump_ref.py set <file> <sha>` prints `changed`/`unchanged` and exits 0, or exits 1 on RefError.

  Task 7's `update-gitops` job calls the CLI, and Task 11 reuses it by hand.

- [ ] **Step 1: Write the failing tests**

```python
# hermes/ci/test_bump_ref.py
import pytest

import bump_ref as b

SNIPPET = '''\
        # commit (AGENT_CONFIG_REF) over the shared PVC, same idea as claude-install's
        - name: profile-sync
          env:
            - name: GIT_CONFIG_VALUE_0
              value: /staging
            - name: AGENT_CONFIG_REF
              value: "cb6f405"
            - name: AGENT_IMAGE
              value: "nousresearch/hermes-agent:v2026.8.19"
'''


def test_read_ref():
    assert b.read_ref(SNIPPET) == "cb6f405"


def test_set_ref_changes_exactly_one_line():
    new, changed = b.set_ref(SNIPPET, "0123456789ab")
    assert changed is True
    old_lines, new_lines = SNIPPET.splitlines(), new.splitlines()
    diff = [(o, n) for o, n in zip(old_lines, new_lines) if o != n]
    assert len(old_lines) == len(new_lines)
    assert diff == [('              value: "cb6f405"', '              value: "0123456789ab"')]


def test_set_ref_to_the_same_value_is_a_noop():
    new, changed = b.set_ref(SNIPPET, "cb6f405")
    assert changed is False and new == SNIPPET


def test_unquoted_value_is_read_and_rewritten_quoted():
    text = SNIPPET.replace('value: "cb6f405"', "value: cb6f405")
    assert b.read_ref(text) == "cb6f405"
    new, changed = b.set_ref(text, "abcdef1")
    assert changed and 'value: "abcdef1"' in new


def test_crlf_line_endings_are_preserved():
    text = SNIPPET.replace("\n", "\r\n")
    new, changed = b.set_ref(text, "abcdef1")
    assert changed and new == text.replace('"cb6f405"', '"abcdef1"')


@pytest.mark.parametrize("text", [
    SNIPPET.replace("AGENT_CONFIG_REF\n", "OTHER_REF\n"),
    SNIPPET + '            - name: AGENT_CONFIG_REF\n              value: "1111111"\n',
    SNIPPET.replace('              value: "cb6f405"', '              valueFrom: {}'),
], ids=["zero", "two", "no-value-line"])
def test_ambiguous_or_missing_entry_is_refused(text):
    with pytest.raises(b.RefError):
        b.set_ref(text, "abcdef1")


@pytest.mark.parametrize("ref", ["HEAD", "abc", "CB6F405", "cb6f405 ", "g123456"])
def test_non_sha_is_refused(ref):
    with pytest.raises(b.RefError):
        b.set_ref(SNIPPET, ref)


def test_cli_get_and_set(tmp_path, capsys):
    f = tmp_path / "deployment.yaml"
    f.write_bytes(SNIPPET.encode())
    assert b.main(["get", str(f)]) == 0
    assert capsys.readouterr().out.strip() == "cb6f405"
    assert b.main(["set", str(f), "abcdef123456"]) == 0
    assert capsys.readouterr().out.strip() == "changed"
    assert f.read_bytes() == SNIPPET.replace('"cb6f405"', '"abcdef123456"').encode()
    assert b.main(["set", str(f), "abcdef123456"]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"
    assert b.main(["set", str(f), "nope"]) == 1
    assert b.main(["bogus"]) == 2
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest -q hermes/ci/test_bump_ref.py 2>&1 | tail -3
```

Expected: `ModuleNotFoundError: No module named 'bump_ref'`.

- [ ] **Step 3: Implement `hermes/ci/bump_ref.py`**

```python
#!/usr/bin/env python3
"""Read or set AGENT_CONFIG_REF in gitops-cluster infra/hermes-agent/deployment.yaml.

    python bump_ref.py get <deployment.yaml>
    python bump_ref.py set <deployment.yaml> <sha>    # prints "changed" or "unchanged"

Edits by pattern, never by line number, and refuses unless the file holds
exactly one `- name: AGENT_CONFIG_REF` env entry with `value:` on the next line.
Every other byte of the file is preserved (comments mentioning the name too).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_NAME_RE = re.compile(r'^[ \t]*-[ \t]+name:[ \t]*["\']?AGENT_CONFIG_REF["\']?[ \t]*\r?$', re.M)
_ENTRY_RE = re.compile(
    r'(?P<head>^[ \t]*-[ \t]+name:[ \t]*["\']?AGENT_CONFIG_REF["\']?[ \t]*\r?\n[ \t]*value:[ \t]*)'
    r'(?P<quote>["\']?)(?P<ref>[^"\'\s]*)(?P=quote)(?P<tail>[ \t]*\r?)$',
    re.M,
)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class RefError(ValueError):
    pass


def _entry(text: str) -> re.Match:
    names = len(_NAME_RE.findall(text))
    if names != 1:
        raise RefError(f"expected exactly one AGENT_CONFIG_REF env entry, found {names}")
    matches = list(_ENTRY_RE.finditer(text))
    if len(matches) != 1:
        raise RefError("the AGENT_CONFIG_REF entry has no `value:` on the line after its name")
    return matches[0]


def read_ref(text: str) -> str:
    return _entry(text).group("ref")


def set_ref(text: str, ref: str) -> tuple[str, bool]:
    if not _SHA_RE.match(ref):
        raise RefError(f"not a lowercase commit sha: {ref!r}")
    m = _entry(text)
    if m.group("ref") == ref:
        return text, False
    new = text[:m.start()] + m.group("head") + f'"{ref}"' + m.group("tail") + text[m.end():]
    return new, True


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 2 and args[0] == "get":
            print(read_ref(Path(args[1]).read_bytes().decode("utf-8")))
            return 0
        if len(args) == 3 and args[0] == "set":
            path = Path(args[1])
            new, changed = set_ref(path.read_bytes().decode("utf-8"), args[2])
            if changed:
                path.write_bytes(new.encode("utf-8"))
            print("changed" if changed else "unchanged")
            return 0
    except RefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests, then against the real deployment (read-only)**

```bash
cd /c/Users/Pol/projects/plder
python -m pytest -q hermes/ci 2>&1 | tail -3
python hermes/ci/bump_ref.py get /c/Users/Pol/projects/gitops-check/infra/hermes-agent/deployment.yaml
```

Expected: `0 failed`, then the currently pinned ref (`<ROOTREF>` after Task 4). The real file contains a comment that mentions `AGENT_CONFIG_REF`, so this proves the pattern ignores it.

- [ ] **Step 5: Commit**

```bash
git add hermes/ci/bump_ref.py hermes/ci/test_bump_ref.py
git commit -m "ci: add AGENT_CONFIG_REF bump helper"
```

---

### Task 7: Image checks and the workflow (plder)

**Files:**
- Create: `plder/hermes/ci/image_checks.sh`
- Create: `plder/hermes/ci/image_checks.py`
- Create: `plder/.github/workflows/hermes-config.yml`

**Interfaces:**
- Consumes: `validate.py` (Task 5), `bump_ref.py` (Task 6), secret `GITOPS_DEPLOY_KEY` (Task 8).
- Produces: workflow `hermes-config` with jobs `validate` and `update-gitops`. A gitops commit `hermes: pin plder <12-char sha>` that changes one line.

Design decisions:
- **Triggers.** Push triggers on `hermes/**` minus `hermes/ci/**`, so neither Pi commits nor CI-tooling commits restart the agent. A pull_request validates `hermes/**` including `hermes/ci/**` and the workflow file.
- **Ancestry guard.** The pinned ref only moves forward. Validate jobs of two quick pushes can finish out of order even though `update-gitops` is serialised, so the guard stops an older run from overwriting a newer pin. It skips when the pushed commit is already contained in the pinned one, which also makes a re-run a no-op.
- **12-character short SHA.** A 7-character ref that later becomes ambiguous in plder would make `git checkout` fail. `sync.py` would then silently fall back to last-good.
- **The deploy key** is loaded into `ssh-agent` from the environment. It is never written to disk, even on the runner, and the host key is pinned to GitHub's published ed25519 key.
- **The image step** overrides the entrypoint and runs as uid 10000. That way neither s6 `/init` nor stage2 runs (stage2 would reject a non-root start anyway), which matches the profile-sync initContainer.

- [ ] **Step 1: Write `hermes/ci/image_checks.sh`**

```sh
#!/bin/sh
# Runs INSIDE nousresearch/hermes-agent with the entrypoint overridden (no s6
# /init, no stage2 hook) as uid 10000 -- the same way the profile-sync
# initContainer runs. Installs every declared profile into a SCRATCH
# HERMES_HOME exactly as sync.py does, then runs image_checks.py.
#   usage: HERMES_HOME=/tmp/ci-home HOME=/tmp/ci-user sh image_checks.sh <hermes dir>
set -u
SRC="${1:-/src/hermes}"
HERMES=/opt/hermes/.venv/bin/hermes
PY=/opt/hermes/.venv/bin/python
: "${HERMES_HOME:?HERMES_HOME must point at a scratch directory}"
case "$HERMES_HOME" in
  /opt/data|/opt/data/*) echo "refusing to install into a live-looking HERMES_HOME: $HERMES_HOME" >&2; exit 2 ;;
esac
mkdir -p "$HERMES_HOME" "${HOME:-/tmp/ci-user}"
fail=0
for manifest in "$SRC"/profiles/*/distribution.yaml; do
  [ -f "$manifest" ] || continue
  dir="${manifest%/distribution.yaml}"
  name="${dir##*/}"
  echo "== hermes profile install $name"
  if "$HERMES" profile install "$dir" --name "$name" --force -y </dev/null; then
    echo "OK: installed $name"
  else
    echo "::error file=hermes/profiles/$name/distribution.yaml::hermes profile install failed for $name"
    fail=1
  fi
done
"$PY" "$SRC/ci/image_checks.py" "$SRC" "$HERMES_HOME" || fail=1
exit "$fail"
```

- [ ] **Step 2: Write `hermes/ci/image_checks.py`**

```python
#!/usr/bin/env python3
"""Checks that need the pinned Hermes image. Runs INSIDE it, via image_checks.sh.

    image_checks.py <hermes dir> <scratch HERMES_HOME the profiles were installed into>

Not unit-tested on the workstation (it imports hermes_cli); rehearsed in a
throwaway pod instead (gitops-cluster docs/plans/2026-09-14-hermes-ci-single-source.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import validate_config_structure  # noqa: E402
from hermes_cli.config_defaults import DEFAULT_CONFIG  # noqa: E402


def main(src: Path, home: Path) -> int:
    latest = DEFAULT_CONFIG["_config_version"]
    failures = 0

    def rel(path: Path) -> str:
        return path.relative_to(src.parent).as_posix()

    def fail(path: Path, msg: str) -> None:
        nonlocal failures
        failures += 1
        print(f"::error file={rel(path)}::{msg}")

    for cfg_path in [src / "root" / "config.yaml", *sorted(src.glob("profiles/*/config.yaml"))]:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if cfg.get("_config_version") != latest:
            fail(cfg_path, f"_config_version is {cfg.get('_config_version')!r} but this image's latest is {latest}: "
                           "the boot-time migrator would rewrite the synced file on every start")
        for issue in validate_config_structure(cfg):
            if issue.severity == "error":
                fail(cfg_path, f"{issue.message} ({issue.hint})")
            else:
                print(f"::warning file={rel(cfg_path)}::{issue.message}")

    for manifest_path in sorted(src.glob("profiles/*/distribution.yaml")):
        installed = home / "profiles" / manifest_path.parent.name
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        for entry in manifest.get("distribution_owned") or []:
            owned = str(entry).strip().strip("/")
            if not (installed / owned).exists():
                fail(manifest_path, f"install did not produce {owned!r} in {installed}")
        declared, landed = manifest_path.parent / "config.yaml", installed / "config.yaml"
        if declared.is_file() and (not landed.is_file() or landed.read_bytes() != declared.read_bytes()):
            fail(manifest_path, "installed config.yaml differs from the declared one (install --force must copy it verbatim)")

    print(f"image checks: {failures} failure(s); image config version {latest}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
```

- [ ] **Step 3: Write `.github/workflows/hermes-config.yml`**

```yaml
# Validates the declared Hermes config and, on master, pins the pushed commit
# into gitops-cluster (infra/hermes-agent/deployment.yaml AGENT_CONFIG_REF).
# ArgoCD rolls the agent from there. Design and live verification:
# gitops-cluster docs/plans/2026-09-14-hermes-ci-single-source.md
name: hermes-config

on:
  push:
    branches: [master]
    # pi/** never restarts the agent; hermes/ci/** is tooling, not config.
    paths:
      - 'hermes/**'
      - '!hermes/ci/**'
  pull_request:
    paths:
      - 'hermes/**'
      - '.github/workflows/hermes-config.yml'
  workflow_dispatch: {}

permissions:
  contents: read

env:
  HERMES_IMAGE: nousresearch/hermes-agent:v2026.8.19

jobs:
  validate:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install validator dependencies
        run: python -m pip install --disable-pip-version-check pyyaml==6.0.3 pytest==9.1.1

      - name: Validator unit tests
        run: python -m pytest -q hermes/ci

      - name: Schema and cross-reference checks
        run: python hermes/ci/validate.py hermes

      - name: Pull the pinned Hermes image
        run: docker pull "$HERMES_IMAGE"

      - name: Dry-run profile installs in the pinned image
        # Entrypoint overridden: no s6 /init, no stage2 hook (which rejects a
        # non-root start), uid 10000 -- exactly how profile-sync runs.
        run: |
          docker run --rm --user 10000:10000 --entrypoint /bin/sh \
            -e HERMES_HOME=/tmp/ci-home -e HOME=/tmp/ci-user \
            -v "$GITHUB_WORKSPACE/hermes:/src/hermes:ro" \
            "$HERMES_IMAGE" /src/hermes/ci/image_checks.sh /src/hermes

  update-gitops:
    needs: validate
    if: github.event_name == 'push' && github.ref == 'refs/heads/master'
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    concurrency:
      group: plder-update-gitops
      cancel-in-progress: false
    steps:
      - name: Check out plder with full history (ancestry guard)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
          path: plder

      - name: Load the gitops deploy key into ssh-agent (never written to disk)
        env:
          GITOPS_DEPLOY_KEY: ${{ secrets.GITOPS_DEPLOY_KEY }}
        run: |
          eval "$(ssh-agent -s)"
          python3 -c 'import os, sys; sys.stdout.write(os.environ["GITOPS_DEPLOY_KEY"].replace("\r", "").strip() + "\n")' | ssh-add -
          ssh-add -l
          echo "SSH_AUTH_SOCK=$SSH_AUTH_SOCK" >> "$GITHUB_ENV"
          echo "SSH_AGENT_PID=$SSH_AGENT_PID" >> "$GITHUB_ENV"
          mkdir -p ~/.ssh && chmod 700 ~/.ssh
          echo 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' >> ~/.ssh/known_hosts

      - name: Clone gitops-cluster
        env:
          GIT_SSH_COMMAND: ssh -o StrictHostKeyChecking=yes
        run: git clone --depth 50 git@github.com:Forgenn/gitops-cluster.git gitops

      - name: Pin AGENT_CONFIG_REF and push
        env:
          FULL_SHA: ${{ github.sha }}
          GIT_SSH_COMMAND: ssh -o StrictHostKeyChecking=yes
        run: |
          set -euo pipefail
          SHORT="${FULL_SHA:0:12}"
          DEP=infra/hermes-agent/deployment.yaml
          cd gitops
          CURRENT="$(python3 ../plder/hermes/ci/bump_ref.py get "$DEP")"
          echo "gitops pins $CURRENT; pushed commit is $SHORT"
          if git -C ../plder cat-file -e "${CURRENT}^{commit}" 2>/dev/null \
             && git -C ../plder merge-base --is-ancestor "$FULL_SHA" "$CURRENT"; then
            echo "gitops already pins $CURRENT, which contains $SHORT; nothing to do"
            exit 0
          fi
          python3 ../plder/hermes/ci/bump_ref.py set "$DEP" "$SHORT"
          git add "$DEP"
          if git diff --cached --quiet; then
            echo "no change"; exit 0
          fi
          SUBJECT="$(git -C ../plder log -1 --format=%s "$FULL_SHA")"
          git config user.name "plder-ci"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git commit -q -F - <<EOF
          hermes: pin plder $SHORT

          $SUBJECT

          Source: https://github.com/Forgenn/plder/commit/$FULL_SHA
          Pinned by the plder hermes-config workflow: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID
          EOF
          for attempt in 1 2 3 4 5; do
            if git push origin HEAD:main; then
              echo "pushed $(git rev-parse --short HEAD) on attempt $attempt"; exit 0
            fi
            sleep $((attempt * 5))
            git pull --rebase origin main
          done
          echo "::error::could not push to gitops-cluster main after 5 attempts"
          exit 1
```

YAML strips the block's common indentation, so the heredoc terminator `EOF` lands at column 0 in the script.

- [ ] **Step 4: Lint locally and commit**

```bash
cd /c/Users/Pol/projects/plder
python -c "import yaml; w=yaml.safe_load(open('.github/workflows/hermes-config.yml', encoding='utf-8')); print(sorted(w['jobs']), w[True]['push']['paths'])"
python -m pytest -q hermes/ci 2>&1 | tail -2
git add hermes/ci/image_checks.sh hermes/ci/image_checks.py .github/workflows/hermes-config.yml
git commit -m "ci: validate hermes config and pin it into gitops-cluster"
```

Expected: `['update-gitops', 'validate'] ['hermes/**', '!hermes/ci/**']` (PyYAML parses the `on:` key as `True`), then `0 failed`.

- [ ] **Step 5 (controller): Rehearse the image step in a throwaway pod**

Paste the throwaway-pod manifest with `P=p3-ci-rehearsal`, then:

```bash
git -C /c/Users/Pol/projects/plder archive --format=tar hermes-ci hermes \
  | kubectl exec -i -n "$NS" "$P" -- sh -c 'mkdir -p /tmp/src && tar xf - -C /tmp/src && ls /tmp/src/hermes'
kubectl exec -i -n "$NS" "$P" -- sh -s <<'SH'
echo "== positive"
env HERMES_HOME=/tmp/ci-home HOME=/tmp/ci-user sh /tmp/src/hermes/ci/image_checks.sh /tmp/src/hermes </dev/null; echo "exit=$?"
echo "== negative: stale schema version"
rm -rf /tmp/neg && cp -r /tmp/src /tmp/neg
sed -i 's/^_config_version: 38/_config_version: 37/' /tmp/neg/hermes/profiles/monitor/config.yaml
env HERMES_HOME=/tmp/ci-home2 HOME=/tmp/ci-user sh /tmp/neg/hermes/ci/image_checks.sh /tmp/neg/hermes </dev/null; echo "exit=$?"
echo "== guard"
env HERMES_HOME=/opt/data sh /tmp/src/hermes/ci/image_checks.sh /tmp/src/hermes </dev/null; echo "exit=$?"
SH
```

Expected:
- **positive:** `OK: installed monitor`, `image checks: 0 failure(s); image config version 38`, `exit=0`.
- **negative:** `::error file=hermes/profiles/monitor/config.yaml::_config_version is 37 but this image's latest is 38…`, `exit=1`.
- **guard:** `refusing to install into a live-looking HERMES_HOME: /opt/data`, `exit=2`.

**If positive fails, STOP.** Fix it and re-run before any push.

---

### Task 8: Create the gitops deploy key (controller only)

**Files:** none in git. Creates plder Actions secret `GITOPS_DEPLOY_KEY` and gitops-cluster deploy key `plder-ci-update-gitops` (read-write).

The private key exists only in process memory, then in GitHub's secret store. The generator script holds no secret, so it may live on disk. Re-running rotates both halves, because it deletes any earlier key with the same title first.

- [ ] **Step 1: Check `cryptography`**

```bash
python -c "import cryptography; print('cryptography', cryptography.__version__)"
```

Expected: `cryptography 44.0.2`. On ImportError, use Step 3's fallback in place of Step 2's run command.

- [ ] **Step 2: Write the generator script and run it**

```bash
KEYGEN_PY=/c/Users/Pol/projects/gitops-check/.superpowers/planning/plder_ci_keygen.py
cat > "$KEYGEN_PY" <<'PY'
"""Create GITOPS_DEPLOY_KEY (plder secret) + its RW deploy key on gitops-cluster.

The private half lives only in this process and in the gh subprocess's stdin.
KEYGEN=pod: read `<base64 private>\n<public line>` from stdin (Step 3 fallback).
"""
import base64, hashlib, json, os, subprocess, sys

TITLE = "plder-ci-update-gitops"


def gh(*args, data=None):
    return subprocess.run(["gh", *args], input=data, capture_output=True, check=True).stdout


if os.environ.get("KEYGEN") == "pod":
    lines = [l.strip() for l in sys.stdin.read().replace("\r", "").splitlines() if l.strip()]
    private, public = base64.b64decode(lines[0]), lines[1]
else:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                           serialization.PublicFormat.OpenSSH).decode() + " " + TITLE
    del key

assert private.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----") and private.endswith(b"\n"), "bad private key shape"
assert public.startswith("ssh-ed25519 "), "bad public key shape"

for old in json.loads(gh("api", "repos/Forgenn/gitops-cluster/keys")):
    if old["title"] == TITLE:
        gh("api", "-X", "DELETE", f"repos/Forgenn/gitops-cluster/keys/{old['id']}")
        print("deleted earlier deploy key", old["id"])

gh("secret", "set", "GITOPS_DEPLOY_KEY", "--repo", "Forgenn/plder", data=private)
del private
added = json.loads(gh("api", "repos/Forgenn/gitops-cluster/keys", "-f", f"title={TITLE}",
                      "-f", f"key={public}", "-F", "read_only=false"))
fp = base64.b64encode(hashlib.sha256(base64.b64decode(public.split()[1])).digest()).decode().rstrip("=")
print(f"secret GITOPS_DEPLOY_KEY set on Forgenn/plder; deploy key id={added['id']} "
      f"read_only={added['read_only']} SHA256:{fp}")
PY
python C:/Users/Pol/projects/gitops-check/.superpowers/planning/plder_ci_keygen.py
```

Expected: `secret GITOPS_DEPLOY_KEY set on Forgenn/plder; deploy key id=<n> read_only=False SHA256:<fp>`. Nothing else is printed. If a `CalledProcessError` shows, its argv holds only the public key. Fix the cause and re-run the whole step.

- [ ] **Step 3: Fallback without `cryptography` (only if Step 1 failed)**

Write the script from Step 2 first (the `cat > "$KEYGEN_PY"` part), then generate the key in a pod's memory-backed `/dev/shm`. Paste the throwaway-pod manifest with `P=p3-keygen`, then:

```bash
kubectl exec -n "$NS" "$P" -- sh -c '
  grep -q " /dev/shm tmpfs " /proc/mounts || { echo "no tmpfs /dev/shm" >&2; exit 3; }
  d=$(mktemp -d /dev/shm/k.XXXXXX) || exit 4
  ssh-keygen -q -t ed25519 -N "" -C plder-ci-update-gitops -f "$d/id" </dev/null \
    && base64 -w0 "$d/id" && printf "\n" && cat "$d/id.pub"
  rc=$?; rm -rf "$d"; exit $rc' \
  | KEYGEN=pod python C:/Users/Pol/projects/gitops-check/.superpowers/planning/plder_ci_keygen.py
```

Base64 carries the private key through `kubectl exec`'s LF→CRLF translation intact. Expected output is the same as Step 2.

- [ ] **Step 4: Verify and tidy**

```bash
gh secret list --repo Forgenn/plder
gh api repos/Forgenn/gitops-cluster/keys --jq '.[] | "\(.id) \(.title) read_only=\(.read_only)"'
rm -f /c/Users/Pol/projects/gitops-check/.superpowers/planning/plder_ci_keygen.py
```

Expected: `GITOPS_DEPLOY_KEY` listed, and exactly one `plder-ci-update-gitops read_only=false` next to the two existing keys.

Risk to accept knowingly: anyone who can push workflow files to plder can use this key to write gitops-cluster. That includes the future meta bot's RW plder key. The spec's "Agent holds write keys to gitops-cluster" row covers it. Revoke with `gh api -X DELETE repos/Forgenn/gitops-cluster/keys/<id>`.

---

### Task 9: Land CI and prove the gate (controller)

**Files:** none (PRs and verification).

- [ ] **Step 1: Push the branch, open the PR, watch validation**

```bash
cd /c/Users/Pol/projects/plder
git fetch -q && git rebase origin/master && python -m pytest -q hermes/ci 2>&1 | tail -1
git push -u origin hermes-ci
gh pr create --repo Forgenn/plder --base master --head hermes-ci \
  --title "ci: validate hermes config and pin it into gitops-cluster" \
  --body "Adds hermes/ci (validator, bump helper, image checks) and the hermes-config workflow. Plan: gitops-cluster docs/plans/2026-09-14-hermes-ci-single-source.md"
gh pr checks hermes-ci --repo Forgenn/plder --watch
```

Expected: `validate` pass and `update-gitops` skipping. The "Dry-run profile installs" step log shows `OK: installed monitor` and `image checks: 0 failure(s)`. Check it with `gh run view <run id> --repo Forgenn/plder --log | grep -E "OK: installed|image checks:"`.

- [ ] **Step 2: Merge. The merge itself must not trigger the workflow.**

```bash
GITOPS_BEFORE=$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)
gh pr merge hermes-ci --repo Forgenn/plder --merge --delete-branch
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only
MERGE_SHA=$(git rev-parse HEAD); echo "$MERGE_SHA"
```

After 2 minutes:

```bash
gh run list --repo Forgenn/plder --workflow hermes-config.yml --json headSha,event --jq ".[] | select(.headSha==\"$MERGE_SHA\")"
```

Expected: empty output. The merge touches only `.github/` and `hermes/ci/**`.

- [ ] **Step 3: A deliberately invalid config must fail validation**

```bash
cd /c/Users/Pol/projects/plder
git checkout -b ci-invalid-probe
python - <<'PY'
p = "hermes/profiles/monitor/config.yaml"
raw = open(p, "rb").read()
assert raw.count(b"override_existing: true") == 1
open(p, "wb").write(raw.replace(b"override_existing: true", b"override_existing: false"))
p = "hermes/root/config.yaml"
raw = open(p, "rb").read()
eol = b"\r\n" if b"\r\n" in raw else b"\n"
needle = b"    - monitor" + eol
assert raw.count(needle) == 1
open(p, "wb").write(raw.replace(needle, needle + b"    - ghost" + eol))
PY
python hermes/ci/validate.py hermes; echo "local exit=$?"
git commit -am "ci: deliberately invalid config (validation probe, do not merge)"
git push -u origin ci-invalid-probe
gh pr create --repo Forgenn/plder --base master --head ci-invalid-probe --title "DO NOT MERGE: validation probe" --body "Must fail validation; closed by the plan."
gh pr checks ci-invalid-probe --repo Forgenn/plder --watch; echo "checks exit=$?"
RUN=$(gh run list --repo Forgenn/plder --workflow hermes-config.yml --branch ci-invalid-probe --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$RUN" --repo Forgenn/plder --log-failed | grep -E "override_existing must be true|'ghost', which is not a declared profile"
```

Expected:
- The local run prints both errors and `local exit=1`.
- `checks exit` is non-zero, with `validate` failing at "Schema and cross-reference checks" and `update-gitops` skipped.
- The grep shows both messages.

- [ ] **Step 4: Close the probe and confirm nothing deployed**

```bash
gh pr close ci-invalid-probe --repo Forgenn/plder --delete-branch
cd /c/Users/Pol/projects/plder && git checkout master && git branch -D ci-invalid-probe
test "$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)" = "$GITOPS_BEFORE" && echo "OK: gitops main untouched"
```

- [ ] **Step 5: A Pi-only commit must not trigger the workflow**

```bash
cd /c/Users/Pol/projects/plder
printf 'CI path-filter probe; removed by the next commit.\n' > pi/.ci-path-probe
git add pi/.ci-path-probe && git commit -m "pi: path-filter probe for the hermes-config workflow" && git push origin master
PI1=$(git rev-parse HEAD)
git rm -q pi/.ci-path-probe && git commit -m "pi: remove the path-filter probe" && git push origin master
PI2=$(git rev-parse HEAD)
```

After 2 minutes:

```bash
gh run list --repo Forgenn/plder --workflow hermes-config.yml --json headSha --jq ".[] | select(.headSha==\"$PI1\" or .headSha==\"$PI2\")"
test "$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)" = "$GITOPS_BEFORE" && echo "OK: no bump for pi/**"
```

Expected: empty output, then `OK: no bump for pi/**`.

---

## Part C: End to end, and the verifications plan 1 never ran

### Task 10: A trivial plder change deploys itself (controller)

**Files:**
- Modify (plder master): `hermes/root/config.yaml`, which gets a comment line

**Gates:** volsync within 36h. Not 08:45–09:15 UTC.

**Rollback:** `python /c/Users/Pol/projects/plder/hermes/ci/bump_ref.py set infra/hermes-agent/deployment.yaml <previous ref>` in the gitops clone, then commit and push. Or `git revert` the plder commit, which lets CI pin the revert.

- [ ] **Step 1: Snapshot**

Run Task 4 Step 1's commands, and record `GITOPS_BEFORE=$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)`.

- [ ] **Step 2: Make and push the change**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only
python - <<'PY'
p = "hermes/root/config.yaml"
raw = open(p, "rb").read()
eol = b"\r\n" if b"\r\n" in raw else b"\n"
line = (b"# Installed onto the agent's PVC by profile-sync on every applied sync;"
        b" runtime edits are reverted by the next one (the replaced file is kept at"
        b" /opt/data/.agent-config/config.yaml.previous).")
assert line not in raw
open(p, "wb").write(line + eol + raw)
PY
python hermes/ci/validate.py hermes
git commit -am "hermes: note that the root config is installed from plder"
git push origin master
PLDER_SHA=$(git rev-parse HEAD); SHORT=${PLDER_SHA:0:12}; echo "$PLDER_SHA $SHORT"
```

- [ ] **Step 3: CI goes green and commits to gitops**

Poll until the run exists (use Monitor/background if `sleep` is blocked):

```bash
until RUN=$(gh run list --repo Forgenn/plder --workflow hermes-config.yml --json databaseId,headSha --jq ".[] | select(.headSha==\"$PLDER_SHA\") | .databaseId" | head -1) && [ -n "$RUN" ]; do sleep 5; done; echo "run $RUN"
gh run watch "$RUN" --repo Forgenn/plder --exit-status
gh run view "$RUN" --repo Forgenn/plder --log | grep -E "gitops pins|pushed [0-9a-f]+ on attempt"
GITOPS_SHA=$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)
gh api "repos/Forgenn/gitops-cluster/commits/$GITOPS_SHA" --jq '.commit.message, (.files[] | "\(.filename) +\(.additions) -\(.deletions)")'
```

Expected:
- The run succeeds, and its log shows `gitops pins <ROOTREF>; pushed commit is <SHORT>` and `pushed <x> on attempt 1`.
- `GITOPS_SHA` differs from `GITOPS_BEFORE`.
- The commit message starts `hermes: pin plder <SHORT>`, followed by the plder subject and a `Source:` link.
- Files: exactly `infra/hermes-agent/deployment.yaml +1 -1`.

- [ ] **Step 4: ArgoCD syncs and the pod rolls**

```bash
export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} conditions={.status.conditions}{"\n"}'
kubectl logs -n argocd statefulset/argocd-application-controller --since=15m | grep -i hermes-agent | grep -iE "ComparisonError|level=error" | tail -5
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl get pod -n hermes $POD -o jsonpath='{.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}{"\n"}'
kubectl logs -n hermes $POD -c profile-sync
```

Expected:
- `Synced Healthy conditions=`, with no controller errors.
- The env value is `<SHORT>`.
- The log includes `copied config.yaml (previous saved to …)`, `installed profile monitor` and `applied ref <SHORT> (result: ok)`.

- [ ] **Step 5: Root config on disk equals plder; served profiles unchanged**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- md5sum /opt/data/config.yaml
git -C /c/Users/Pol/projects/plder show "$PLDER_SHA:hermes/root/config.yaml" | md5sum
kubectl exec -n hermes $POD -c hermes-agent -- head -1 /opt/data/config.yaml
```

Expected: the md5s are equal, and line 1 is the new comment. Then re-run Task 4 Step 1's commands and compare with this task's Step 1 snapshot: `['default', 'monitor']` and identical monitor hashes.

- [ ] **Step 6: Agent-mode probes in root and monitor**

Run the kit's **Create**, wait, **Check**, **Remove**. Expected: both homes `last_status ok | last_error None | response ok: True`.

- [ ] **Step 7: Re-running the pin job is a no-op**

```bash
JOB=$(gh run view "$RUN" --repo Forgenn/plder --json jobs --jq '.jobs[] | select(.name=="update-gitops") | .databaseId')
gh run rerun "$RUN" --repo Forgenn/plder --job "$JOB"
until [ "$(gh run view "$RUN" --repo Forgenn/plder --json status --jq .status)" = "completed" ]; do sleep 10; done
gh run view "$RUN" --repo Forgenn/plder --log | grep "already pins" | tail -1
test "$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)" = "$GITOPS_SHA" && echo "OK: no second commit"
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only
```

Expected: `gitops already pins <SHORT>, which contains <SHORT>; nothing to do`, then `OK: no second commit`. The local gitops clone is fast-forwarded onto the bot's commit.

---

### Task 11: The outstanding live cron and offline verifications (controller)

**Files:**
- Modify (plder master): `hermes/profiles/monitor/cron/jobs.json`. A throwaway job is added, renamed, then removed.
- Modify (gitops main, twice): `infra/hermes-agent/deployment.yaml` (`AGENT_CONFIG_REF` → `0000000` → back)

**Gates:** Task 10 passed. volsync within 36h. At least 90 minutes clear of 09:00 UTC, because this task rolls the pod five times.

The pod's jobs live in the **monitor** store. The declared probe `plan3-decl-probe` is scheduled `0 4 1 1 *` (January 1st), delivers `local`, and cannot fire during the test. Throughout, the real report `6270d3f018f2` must keep its id, schedule and `next_run_at`.

Each plder push in this task deploys itself through CI. Wait for it the way Task 10 Steps 3–4 do: find the run for the pushed SHA, `gh run watch --exit-status`, wait for the ArgoCD revision, `kubectl rollout status`, then read the profile-sync log.

- [ ] **Step 1: Create the hand-made job and record it**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -i -n hermes "$POD" -c hermes-agent -- sh -s <<'SH'
/command/s6-setuidgid hermes env HERMES_HOME=/opt/data/profiles/monitor /opt/hermes/.venv/bin/hermes \
  cron create "0 4 1 1 *" "Reply with exactly PLAN3_HANDMADE." --name "plan3 handmade probe" --deliver local </dev/null 2>&1 | tail -2
SH
```

Then run the **job-table printer**. Expected: monitor shows `6270d3f018f2 … managed_by=plder` and `<HANDMADE_ID> 'plan3 handmade probe' managed_by=None state=scheduled enabled=True next=<T>`. Save both lines verbatim. This is the stand-in for a job created from Telegram: same store, and no `managed_by`.

- [ ] **Step 2: Push A (declare the throwaway job). The hand-made job survives an applied sync.**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only
python - <<'PY'
import json
p = "hermes/profiles/monitor/cron/jobs.json"
doc = json.load(open(p, encoding="utf-8"))
assert all(j["id"] != "plan3-decl-probe" for j in doc["jobs"])
doc["jobs"].append({
    "id": "plan3-decl-probe", "name": "plan3 declared probe", "managed_by": "plder",
    "prompt": "Reply with exactly PLAN3_DECL_PROBE and nothing else.",
    "skills": [], "skill": None, "model": None, "provider": None, "script": None,
    "no_agent": False, "monitor_script": None, "monitor_url": None, "context_from": None,
    "schedule": {"kind": "cron", "expr": "0 4 1 1 *", "display": "0 4 1 1 *"},
    "schedule_display": "0 4 1 1 *", "repeat": {"times": None, "completed": 0},
    "enabled": True, "deliver": "local", "workdir": None,
})
with open(p, "w", encoding="utf-8", newline="\n") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
python hermes/ci/validate.py hermes
git add hermes/profiles/monitor/cron/jobs.json
git diff --cached --stat
git commit -m "hermes: declare a throwaway monitor job (plan 3 live verification)"
git push origin master && git rev-parse HEAD
```

Expected before the push: `OK: hermes is valid`, and a diff stat of about 30 insertions and 1 deletion (the old closing bracket line).

After the deploy, the profile-sync log shows `cron: 3 job(s) in /opt/data/profiles/monitor/cron/jobs.json` and `result: ok`. The job-table printer shows:
- `6270d3f018f2`, with the same `next` as Step 1.
- `plan3-decl-probe 'plan3 declared probe' managed_by=plder state=scheduled enabled=True`.
- The hand-made line **identical** to Step 1: same id, `managed_by=None`, same `next`.

- [ ] **Step 3: The hand-made job survives a plain restart (skip path)**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl delete pod -n hermes $POD && kubectl rollout status deployment/hermes-agent -n hermes --timeout=600s
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl logs -n hermes $POD -c profile-sync
```

Expected: only `ref <push A short> + image already applied; skipping`. The job-table printer output matches Step 2's.

- [ ] **Step 4: Pause the declared job, then push B (rename it). It stays paused.**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -i -n hermes "$POD" -c hermes-agent -- sh -s <<'SH'
/command/s6-setuidgid hermes env HERMES_HOME=/opt/data/profiles/monitor /opt/hermes/.venv/bin/hermes cron pause plan3-decl-probe </dev/null 2>&1 | tail -1
SH
```

Job-table printer. Expected: `plan3-decl-probe … state=paused enabled=False paused_at=<P>`. Save `<P>`.

```bash
cd /c/Users/Pol/projects/plder && git pull --ff-only
python - <<'PY'
import json
p = "hermes/profiles/monitor/cron/jobs.json"
doc = json.load(open(p, encoding="utf-8"))
[job] = [j for j in doc["jobs"] if j["id"] == "plan3-decl-probe"]
assert "state" not in job, "the declaration must stay silent about state for this test"
job["name"] = "plan3 declared probe v2"
with open(p, "w", encoding="utf-8", newline="\n") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
git commit -am "hermes: rename the throwaway monitor job while it is paused live"
git push origin master && git rev-parse HEAD
```

After the deploy, the log shows `cron: 3 job(s)` and `result: ok`. Job table:
- `plan3-decl-probe 'plan3 declared probe v2' … state=paused enabled=False paused_at=<P>`. The declared rename applied, and the live pause survived, including `paused_at`.
- `6270d3f018f2` and the hand-made job unchanged.

- [ ] **Step 5: Push C (remove the declared job). It disappears; the hand-made job survives.**

```bash
cd /c/Users/Pol/projects/plder && git pull --ff-only
python - <<'PY'
import json
p = "hermes/profiles/monitor/cron/jobs.json"
doc = json.load(open(p, encoding="utf-8"))
before = len(doc["jobs"])
doc["jobs"] = [j for j in doc["jobs"] if j["id"] != "plan3-decl-probe"]
assert len(doc["jobs"]) == before - 1 and [j["id"] for j in doc["jobs"]] == ["6270d3f018f2"]
with open(p, "w", encoding="utf-8", newline="\n") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
test "$(git show HEAD~2:hermes/profiles/monitor/cron/jobs.json | md5sum)" = "$(python -c "import sys; sys.stdout.buffer.write(open('hermes/profiles/monitor/cron/jobs.json','rb').read().replace(b'\r\n', b'\n'))" | md5sum)" && echo "OK: declaration restored byte-identical to before push A"
git commit -am "hermes: remove the throwaway monitor job"
git push origin master && git rev-parse --short=12 HEAD
```

Record the printed short SHA as `CREF`. After the deploy, the log shows `cron: 2 job(s) in /opt/data/profiles/monitor/cron/jobs.json` and `result: ok`. Job table: monitor holds exactly `6270d3f018f2` (same `next`) and the hand-made job (identical to Step 1). `plan3-decl-probe` is **gone** even though it was paused: it carried `managed_by: plder`, so its removal from git retires it.

- [ ] **Step 6: Offline restart comes up on last-good**

Pinning a commit that does not exist makes the clone step fail exactly as an unreachable GitHub does: `clone()` returns `None` and the same fallback runs. Breaking the key or the network would also break the agent's own git.

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only
python /c/Users/Pol/projects/plder/hermes/ci/bump_ref.py set infra/hermes-agent/deployment.yaml 0000000
git commit -am "hermes: pin plder 0000000 to exercise the offline fallback"
git push origin main || { git pull --rebase origin main && git push origin main; }
GITOPS_SHA=$(git rev-parse HEAD)
export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl get pod -n hermes $POD -o jsonpath='{.status.phase} ready={.status.containerStatuses[0].ready} restarts={.status.containerStatuses[0].restartCount}{"\n"}'
kubectl logs -n hermes $POD -c profile-sync
kubectl exec -n hermes $POD -c hermes-agent -- sh -c 'cat /opt/data/.agent-config/applied; echo; md5sum /opt/data/config.yaml'
git -C /c/Users/Pol/projects/plder show "$CREF:hermes/root/config.yaml" | md5sum
```

Expected:
- `Running ready=true restarts=0`. **A CrashLoopBackOff or `Init:Error` here is a release blocker.**
- The log contains `WARNING clone failed: …`, `falling back to last-good tree`, `config.yaml already matches the declaration` and `applied ref <CREF> (result: ok)`. The recorded ref is **not** `0000000`.
- The config md5 equals the plder blob md5.
- The job-table printer matches Step 5. The served profiles are still `['default', 'monitor']`: re-run the `served_profiles` line from Task 4 Step 1.

- [ ] **Step 7: Restore the real pin**

```bash
cd /c/Users/Pol/projects/gitops-check && git pull --ff-only
python /c/Users/Pol/projects/plder/hermes/ci/bump_ref.py set infra/hermes-agent/deployment.yaml "$CREF"
git commit -am "hermes: restore plder $CREF after the offline fallback test"
git push origin main || { git pull --rebase origin main && git push origin main; }
GITOPS_SHA=$(git rev-parse HEAD)
export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl logs -n hermes $POD -c profile-sync
```

Expected: only `ref <CREF> + image already applied; skipping`. The fallback recorded `CREF` from a tree identical to `CREF`, so nothing needs re-applying.

- [ ] **Step 8: Clean up, final probe, record**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -i -n hermes "$POD" -c hermes-agent -- sh -s <<'SH'
ID=$(/opt/hermes/.venv/bin/python -c "import json; print(' '.join(j['id'] for j in json.load(open('/opt/data/profiles/monitor/cron/jobs.json'))['jobs'] if j.get('name')=='plan3 handmade probe'))" </dev/null)
for i in $ID; do /command/s6-setuidgid hermes env HERMES_HOME=/opt/data/profiles/monitor /opt/hermes/.venv/bin/hermes cron remove "$i" </dev/null 2>&1 | tail -1; done
SH
```

Job-table printer. Expected: root shows nothing, and monitor shows only `6270d3f018f2` with `next=<tomorrow>T09:00:00+00:00`.

Run the agent-probe kit (**Create**, wait, **Check**, **Remove**) once more. Expected: both homes pass.

Then append a `## Deploy record` section to this file. It should hold:
- The dates, `ROOTREF`, the Task 10 `SHORT`, `CREF`, and the gitops SHAs.
- One line per verification with its observed result.

Commit it (`docs: record the plan 3 deploy and live verifications`) and push `main` after `git pull --ff-only`. The push touches only `docs/`, so the pod does not roll.

---

## Out of scope

Tracked in the spec for later plans:
- The meta bot's plder write credential (`163152504`, not mapped).
- The remaining six bots.
- `skills/custom/` authoring.
- A daily drift report comparing `/opt/data/config.yaml` against the applied ref (now possible, and useful because runtime writers exist).
- A monitor check for ArgoCD `ComparisonError`.
- Protecting the `GITOPS_DEPLOY_KEY` workflow path from other plder writers.
