# Hermes Monitor Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Cluster Monitor a real Hermes bot — its daily report runs inside the `monitor` profile, under that profile's own persona, model and skill curation — instead of a root-agent job that merely carries the `[bot:monitor]` name.

**Architecture:** Turn on gateway multiplex so the single gateway ticks the `monitor` profile's cron store. Declare the monitor profile as a distribution in plder and extend `sync.py` to install declared profiles with `hermes profile install --force`. Move the daily job from the root cron declaration to the monitor's, so the root store retires it (it is `managed_by: plder`) and the profile store receives it with the same id, in one pod start.

**Tech Stack:** Kubernetes (k3s) + Kustomize + ArgoCD v3.1.8; Python 3.13 (`/opt/hermes/.venv`); pytest; Hermes Agent v2026.8.19 (hermes_cli 0.20.5).

**Spec:** `docs/plans/2026-09-12-hermes-config-as-code.md` (phase 4, "Monitor as a real bot"). Builds on the shipped plan `docs/plans/2026-09-13-hermes-config-sync-root.md`.

## Global Constraints

- Hermes image `nousresearch/hermes-agent:v2026.8.19`. Do not bump it.
- `HERMES_HOME=/opt/data`. The monitor profile home is `/opt/data/profiles/monitor`.
- CLI entrypoint: `/opt/hermes/.venv/bin/hermes`. `python -m hermes_cli` does not work.
- `kubectl exec` lands as uid 0. Wrap any in-pod write in `/command/s6-setuidgid hermes`.
- Git Bash mangles container paths passed to `kubectl exec`. Export `MSYS_NO_PATHCONV=1` or wrap commands in `sh -c '...'`.
- **`sync.py` must never fail the pod.** Every new failure path logs and continues; `main()` returns 0.
- **Tests stay hermetic.** `test_sync.py` has an autouse `isolated_main` fixture that rebinds `HERMES_HOME`/`STAGING`/etc. to tmp paths. Every test that reaches `subprocess.run` must monkeypatch it. No test may touch the network or a real Hermes home.
- Never declared in git, by design: `profile.yaml` (Bot Mode's Desktop UI owns roster/group metadata there), `skills/` (upstream-bundled; `skills_sync` owns them), `.env`, `memories/`, `sessions/`.
- Verify files with **in-pod `md5sum` vs local `md5sum`**, not `kubectl exec -- cat | diff` (kubectl translates line endings on Windows).
- plder default branch is `master`. Commit style: `component: lowercase description`.

## Verified facts this plan relies on

Read from the running image's source on 2026-09-13; re-verify if the image changes.

- Multiplex: `gateway.multiplex_profiles: true` and `gateway.multiplex_profile_allowlist: [...]` are both read from the nested `gateway:` section (`gateway/config.py` ~1187-1213, ~1418-1432). Env `GATEWAY_MULTIPLEX_PROFILES` overrides config. The default profile is always served; with an allowlist, only listed named profiles are added (`hermes_cli/profiles.py::profiles_to_serve`).
- With multiplex on, the ticker ticks each served profile's cron store (`cron/scheduler_provider.py` ~526-531). With it off, only the root store ticks — which is why the monitor's routine has so far run as the root agent.
- A cron job in a profile store executes under that profile's `HERMES_HOME` and `SOUL.md` (proven by a live spike, 2026-09-12).
- `hermes profile install <dir> --name N --force -y` on an existing profile bootstraps missing user dirs and copies only `distribution_owned` paths with `preserve_config=False`; user-owned paths are excluded (`hermes_cli/profile_distribution.py`).
- `deliver: telegram` resolves `TELEGRAM_HOME_CHANNEL` from env (`cron/scheduler.py` ~511). **Unverified:** whether the monitor profile's own 24 KB `.env` (a vendor-example copy) shadows the shared value in profile scope. Task 6 tests this live.
- `hermes-config` is a plain ConfigMap mounted by `subPath`, so editing it neither rolls the pod nor live-updates the mounted file. Config changes land only when the pod restarts — which is why Task 6 ships them in the same commit as the `AGENT_CONFIG_REF` bump.

---

### Task 1: Declare the monitor profile in plder

**Files:**
- Create: `plder/hermes/profiles/monitor/distribution.yaml`
- Create: `plder/hermes/profiles/monitor/SOUL.md` (captured)
- Create: `plder/hermes/profiles/monitor/config.yaml` (captured)
- Create: `plder/hermes/profiles/monitor/assets/avatar.png` (captured)

**Interfaces:**
- Produces: `hermes/profiles/monitor/` containing a `distribution.yaml`. Task 3's `install_profiles()` treats any directory under `hermes/profiles/` that contains a `distribution.yaml` as an installable profile.

- [ ] **Step 1: Capture the live profile files as base64**

Base64 transfer avoids line-ending translation, so the files land byte-identical.

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
cd /c/Users/Pol/projects/plder && git pull --ff-only
mkdir -p hermes/profiles/monitor/assets
for f in SOUL.md config.yaml assets/avatar.png; do
  kubectl exec -n hermes $POD -c hermes-agent -- base64 -w0 /opt/data/profiles/monitor/$f \
    | base64 -d > hermes/profiles/monitor/$f
done
ls -la hermes/profiles/monitor hermes/profiles/monitor/assets
```

- [ ] **Step 2: Verify byte-identity with in-pod hashes**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c \
  'cd /opt/data/profiles/monitor && md5sum SOUL.md config.yaml assets/avatar.png'
( cd /c/Users/Pol/projects/plder/hermes/profiles/monitor && md5sum SOUL.md config.yaml assets/avatar.png )
```

Expected: the three hashes match pairwise.

- [ ] **Step 3: Write the distribution manifest**

`skills/`, `cron/` and `profile.yaml` are deliberately absent. Skills belong to upstream `skills_sync`. Cron is merged by `sync.py`'s id-keyed upsert, never copied. `profile.yaml` belongs to Bot Mode.

```yaml
# hermes/profiles/monitor/distribution.yaml
name: monitor
version: 0.1.0
description: "Cluster Monitor — alerts, storage, backups, ArgoCD sync; daily exception report"
distribution_owned:
  - SOUL.md
  - config.yaml
  - assets/
```

- [ ] **Step 4: Commit (do not push yet)**

```bash
cd /c/Users/Pol/projects/plder
git add hermes/profiles/monitor
git commit -m "hermes: declare the monitor profile as a distribution"
```

---

### Task 2: Move the daily report job into the monitor profile

**Files:**
- Modify: `plder/hermes/root/cron/jobs.json` — job removed (file keeps an empty `jobs` list)
- Create: `plder/hermes/profiles/monitor/cron/jobs.json` — job added, same id

**Interfaces:**
- Consumes: the existing declared job `6270d3f018f2` in `hermes/root/cron/jobs.json`.
- Produces: `hermes/profiles/monitor/cron/jobs.json` holding that job, read by Task 3's per-profile `apply_cron`.

The id stays `6270d3f018f2`. On the next sync, the root upsert retires it (it is `managed_by: plder` and no longer declared at root) and the monitor upsert creates it. The root file must remain, with `{"jobs": []}` — deleting the file would make `apply_cron` treat it as "nothing to do" and never retire the job.

- [ ] **Step 1: Move the job programmatically**

Never retype the 4,084-character prompt.

```bash
cd /c/Users/Pol/projects/plder
python - <<'PY'
import json
root_p = "hermes/root/cron/jobs.json"
mon_p = "hermes/profiles/monitor/cron/jobs.json"
root = json.load(open(root_p, encoding="utf-8"))
moved = [j for j in root["jobs"] if j["id"] == "6270d3f018f2"]
assert len(moved) == 1, moved
root["jobs"] = [j for j in root["jobs"] if j["id"] != "6270d3f018f2"]
import os
os.makedirs("hermes/profiles/monitor/cron", exist_ok=True)
for path, doc in ((root_p, root), (mon_p, {"jobs": moved})):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
print("root jobs:", len(root["jobs"]), "| monitor jobs:", len(moved))
PY
```

Expected: `root jobs: 0 | monitor jobs: 1`

- [ ] **Step 2: Validate the moved job is unchanged**

```bash
cd /c/Users/Pol/projects/plder
python - <<'PY'
import json, subprocess
before = json.loads(subprocess.check_output(["git", "show", "HEAD:hermes/root/cron/jobs.json"]))["jobs"][0]
after = json.load(open("hermes/profiles/monitor/cron/jobs.json", encoding="utf-8"))["jobs"][0]
assert before == after, "job content changed during the move"
assert json.load(open("hermes/root/cron/jobs.json", encoding="utf-8")) == {"jobs": []}
assert "BACKUP FRESHNESS" in after["prompt"] and after["managed_by"] == "plder"
print("OK: job moved verbatim; root declaration is an empty list")
PY
```

- [ ] **Step 3: Commit (do not push yet)**

```bash
git add hermes/root/cron/jobs.json hermes/profiles/monitor/cron/jobs.json
git commit -m "hermes: move the daily monitor report into the monitor profile"
```

---

### Task 3: Teach sync.py to install declared profiles (TDD)

**Files:**
- Modify: `infra/hermes-agent/sync/sync.py`
- Test: `infra/hermes-agent/sync/test_sync.py`

**Interfaces:**
- Consumes: the existing `apply_cron(home: Path, declared_file: Path) -> bool`, `sync_skills(home: Path) -> bool`, `log(msg)`, and module globals `HERMES_HOME`, `STAGING`.
- Produces: `HERMES_BIN: str`, `install_profiles(staged: Path) -> tuple[list[str], bool]`, and the `applied` record's `"profiles"` field populated with installed profile names.

- [ ] **Step 1: Write the failing tests**

Append to `infra/hermes-agent/sync/test_sync.py`. The autouse `isolated_main` fixture already rebinds `HERMES_HOME` and `STAGING`; each test patches `subprocess.run` itself.

```python
# ---- install_profiles -------------------------------------------------------

def _profile(staged, name, jobs=None, manifest=True):
    d = staged / "hermes" / "profiles" / name
    d.mkdir(parents=True)
    if manifest:
        (d / "distribution.yaml").write_text(f"name: {name}\nversion: 0.1.0\n")
    if jobs is not None:
        (d / "cron").mkdir()
        (d / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    return d


def _fake_run(calls, fail_for=(), exc=None):
    def run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        if cmd[1:3] == ["profile", "install"] and cmd[5] in fail_for:
            raise exc or sync_module.subprocess.CalledProcessError(1, cmd, stderr=b"boom")
        return MagicMock(returncode=0)
    return run


def _installs(calls):
    return [c for c in calls if c["cmd"][1:3] == ["profile", "install"]]


def test_install_profiles_installs_each_declared_profile(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    _profile(staged, "shopper")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    names, ok = sync_module.install_profiles(staged)
    assert names == ["monitor", "shopper"]
    assert ok is True
    cmds = [c["cmd"] for c in _installs(calls)]
    assert [c[5] for c in cmds] == ["monitor", "shopper"]
    assert all(c[0] == sync_module.HERMES_BIN and "--force" in c and "-y" in c for c in cmds)
    assert all(c["env"]["HERMES_HOME"] == str(sync_module.HERMES_HOME) for c in _installs(calls))


def test_install_profiles_ignores_directories_without_a_manifest(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    _profile(staged, "notes", manifest=False)
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    names, ok = sync_module.install_profiles(staged)
    assert names == ["monitor"] and ok is True
    assert [c["cmd"][5] for c in _installs(calls)] == ["monitor"]


def test_install_profiles_continues_after_a_failed_install(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    _profile(staged, "shopper")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls, fail_for=("monitor",)))
    names, ok = sync_module.install_profiles(staged)
    assert names == ["shopper"]
    assert ok is False


def test_install_profiles_survives_a_missing_binary(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run",
                        _fake_run(calls, fail_for=("monitor",), exc=FileNotFoundError("hermes")))
    names, ok = sync_module.install_profiles(staged)
    assert names == [] and ok is False


def test_install_profiles_survives_a_failure_with_no_stderr(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor")
    calls = []
    err = sync_module.subprocess.CalledProcessError(1, ["hermes"], stderr=None)
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls, fail_for=("monitor",), exc=err))
    names, ok = sync_module.install_profiles(staged)
    assert names == [] and ok is False


def test_install_profiles_rejects_an_invalid_profile_name(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "Bad_Name")
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    names, ok = sync_module.install_profiles(staged)
    assert names == [] and ok is False
    assert _installs(calls) == []


def test_install_profiles_applies_the_profiles_declared_cron(monkeypatch):
    staged = sync_module.STAGING
    _profile(staged, "monitor", jobs=[{"id": "abc123def456", "name": "[bot:monitor] daily"}])
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run([]))
    names, ok = sync_module.install_profiles(staged)
    live = json.loads((sync_module.HERMES_HOME / "profiles" / "monitor" / "cron" / "jobs.json").read_text())
    assert ok is True
    assert [j["id"] for j in live["jobs"]] == ["abc123def456"]
    assert live["jobs"][0]["managed_by"] == "plder"


def test_install_profiles_with_no_profiles_directory_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run(calls))
    assert sync_module.install_profiles(sync_module.STAGING) == ([], True)
    assert calls == []


def test_main_moves_a_job_from_root_to_a_profile_and_records_it(monkeypatch):
    """The root declaration drops the job and the profile declares it: after one
    run the root store has retired it and the profile store holds it."""
    job = {"id": "6270d3f018f2", "name": "[bot:monitor] daily exception report"}
    home = sync_module.HERMES_HOME
    (home / "cron").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{**job, "managed_by": "plder"}]}))

    def fake_clone(dest):
        (dest / "hermes" / "root" / "cron").mkdir(parents=True)
        (dest / "hermes" / "root" / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
        _profile(dest, "monitor", jobs=[job])
        return "abc1234"

    monkeypatch.setattr(sync_module, "REF", "abc1234")
    monkeypatch.setattr(sync_module, "clone", fake_clone)
    monkeypatch.setattr(sync_module.subprocess, "run", _fake_run([]))
    assert main() == 0

    root_live = json.loads((home / "cron" / "jobs.json").read_text())
    prof_live = json.loads((home / "profiles" / "monitor" / "cron" / "jobs.json").read_text())
    applied = json.loads(sync_module.APPLIED.read_text())
    assert root_live["jobs"] == []
    assert [j["id"] for j in prof_live["jobs"]] == ["6270d3f018f2"]
    assert applied["profiles"] == ["monitor"]
    assert applied["result"] == "ok"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest test_sync.py -v -k "install_profiles or moves_a_job"
```

Expected: FAIL — `AttributeError: module 'sync' has no attribute 'install_profiles'` (and `HERMES_BIN`).

- [ ] **Step 3: Implement `install_profiles`**

In `sync.py`, add `import re` alongside the existing imports, add the constants below `VENV_PY`, and add the function after `copy_root_files`:

```python
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
# Same shape Hermes itself enforces for profile names: lowercase, digits, dashes.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
```

```python
def install_profiles(staged: Path) -> tuple[list[str], bool]:
    """Install every declared profile distribution, then apply its cron.

    A declared profile is a directory under staged/hermes/profiles/ containing a
    distribution.yaml. `hermes profile install --force` copies only the paths the
    manifest lists and never touches user-owned data (memories, sessions, .env).
    Returns (installed profile names, all_ok). Never raises.
    """
    root = staged / "hermes" / "profiles"
    installed: list[str] = []
    ok = True
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    except Exception as exc:
        log(f"WARNING could not list declared profiles in {root}: {exc}")
        return installed, False

    for src in candidates:
        name = src.name
        if not (src / "distribution.yaml").is_file():
            continue
        if not _PROFILE_NAME_RE.match(name):
            log(f"WARNING skipping declared profile with an invalid name: {name!r}")
            ok = False
            continue
        try:
            subprocess.run(
                [HERMES_BIN, "profile", "install", str(src), "--name", name, "--force", "-y"],
                env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
                check=True, capture_output=True, timeout=180,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip()[:300]
            log(f"WARNING profile install failed for {name}: {detail}")
            ok = False
            continue
        except Exception as exc:
            log(f"WARNING profile install failed for {name}: {type(exc).__name__}: {str(exc)[:300]}")
            ok = False
            continue

        installed.append(name)
        log(f"installed profile {name}")
        home = HERMES_HOME / "profiles" / name
        ok = apply_cron(home, src / "cron" / "jobs.json") and ok
        # Log-only, for the same reason as the root call in main(): a transient
        # skills failure must not flip result to "partial" and force a re-clone
        # on every restart. Unlike root, profiles get NO sync from stage2-hook,
        # so this call is what finally keeps their bundled skills current.
        sync_skills(home)
    return installed, ok
```

- [ ] **Step 4: Wire it into `main()`**

In `main()`, replace:

```python
        sync_skills(HERMES_HOME)

        result = "ok" if steps_ok else "partial"
```

with:

```python
        sync_skills(HERMES_HOME)

        # Profiles run AFTER the root cron apply on purpose: moving a job from the
        # root declaration to a profile's must retire it from the root store and
        # create it in the profile store within the same run.
        profiles, profiles_ok = install_profiles(STAGING)
        steps_ok = profiles_ok and steps_ok

        result = "ok" if steps_ok else "partial"
```

and in the `APPLIED.write_text(...)` payload replace `"profiles": [],` with `"profiles": profiles,`.

- [ ] **Step 5: Run the full suite to verify everything passes**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest -v
```

Expected: PASS — all pre-existing tests plus the 9 new ones.

- [ ] **Step 6: Confirm no test touched a real Hermes home**

```bash
ls -d /c/Users/Pol/AppData/Local/hermes/.agent-config /c/Users/Pol/AppData/Local/hermes/profiles/monitor 2>&1
```

Expected: both `No such file or directory`.

- [ ] **Step 7: Commit**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent/sync/sync.py infra/hermes-agent/sync/test_sync.py
git commit -m "hermes: install declared profiles in config sync"
```

---

### Task 4: Declare gateway multiplex

**Files:**
- Modify: `infra/hermes-agent/configmap.yaml` — the `config.yaml` key of `hermes-config`
- Modify: `plder/hermes/root/config.yaml` — kept in step by hand (it is not synced while the ConfigMap owns root config)

**Interfaces:**
- Produces: a gateway that serves `default` plus `monitor`, and therefore ticks the monitor's cron store.

- [ ] **Step 1: Add the gateway section to the ConfigMap**

In `infra/hermes-agent/configmap.yaml`, inside the `config.yaml: |` block, append at the same indentation as `model:`:

```yaml
    # Serve named profiles from the one gateway. Required for a bot's own cron:
    # without multiplex only the root cron store ticks, so a profile routine runs
    # as the ROOT agent with the root persona (cron/scheduler_provider.py).
    # Allowlisted, so each new bot is an explicit, reviewed addition.
    gateway:
      multiplex_profiles: true
      multiplex_profile_allowlist:
        - monitor
```

- [ ] **Step 2: Mirror it in plder's root config**

Append to `/c/Users/Pol/projects/plder/hermes/root/config.yaml` (no indentation — it is a plain file):

```yaml
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist:
    - monitor
```

- [ ] **Step 3: Verify both parse and agree**

```bash
cd /c/Users/Pol/projects/gitops-check
python - <<'PY'
import yaml
cm = yaml.safe_load(open("infra/hermes-agent/configmap.yaml"))
live = yaml.safe_load(cm["data"]["config.yaml"])["gateway"]
declared = yaml.safe_load(open("/c/Users/Pol/projects/plder/hermes/root/config.yaml"))["gateway"]
assert live == declared == {"multiplex_profiles": True, "multiplex_profile_allowlist": ["monitor"]}, (live, declared)
print("OK: ConfigMap and plder root config agree:", live)
PY
kubectl kustomize infra/hermes-agent > /dev/null && echo "BUILD OK"
```

- [ ] **Step 4: Commit both (do not push gitops yet)**

```bash
git add infra/hermes-agent/configmap.yaml
git commit -m "hermes: serve the monitor profile through gateway multiplex"
cd /c/Users/Pol/projects/plder
git add hermes/root/config.yaml
git commit -m "hermes: mirror gateway multiplex in the declared root config"
```

---

### Task 5: Pre-flight — run the real sync against a scratch home

**Gate:** Task 6 does not start unless this passes. Plan 1 needed three production deploys because its fixes were validated only partially; this rehearses the exact code path against the real image and a real clone, without touching `/opt/data`.

**Files:** none.

- [ ] **Step 1: Push plder**

Inert until gitops pins the new SHA.

```bash
cd /c/Users/Pol/projects/plder && git push origin master && git rev-parse --short HEAD
```

Record that SHA as `NEWREF`.

- [ ] **Step 2: Rehearse inside the pod**

Replace `NEWREF` with the SHA from Step 1.

```bash
export MSYS_NO_PATHCONV=1
NEWREF=REPLACE_WITH_SHA_FROM_STEP_1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
tar cf - sync.py cron_upsert.py | kubectl exec -i -n hermes $POD -c hermes-agent -- \
  sh -c 'rm -rf /dev/shm/sync && mkdir -p /dev/shm/sync && tar xf - -C /dev/shm/sync && chown -R 10000:10000 /dev/shm/sync'
kubectl get secret hermes-secrets -n hermes -o jsonpath='{.data.PLDER_DEPLOY_KEY_READ}' | base64 -d \
| kubectl exec -i -n hermes $POD -c hermes-agent -- sh -c "
umask 077; cat > /dev/shm/k; chown 10000:10000 /dev/shm/k
rm -rf /dev/shm/stg /dev/shm/fh; mkdir /dev/shm/stg; chmod 777 /dev/shm/stg
mkdir -p /dev/shm/fh; chown 10000:10000 /dev/shm/fh
cat > /dev/shm/run.py <<PY
import sys; sys.path.insert(0, '/dev/shm/sync')
from pathlib import Path
import sync as s
s.HERMES_HOME = Path('/dev/shm/fh'); s.STATE_DIR = s.HERMES_HOME / '.agent-config'
s.APPLIED = s.STATE_DIR / 'applied'; s.LAST_GOOD = s.STATE_DIR / 'last-good'
s.STAGING = Path('/dev/shm/stg'); s.REF = '$NEWREF'; s.IMAGE = 'nousresearch/hermes-agent:v2026.8.19'
print('main() returned', s.main())
PY
chown 10000:10000 /dev/shm/run.py
cd /tmp && /command/s6-setuidgid hermes env HOME=/opt/data/home HERMES_HOME=/dev/shm/fh \
  GIT_SSH_COMMAND='ssh -F /opt/data/home/.ssh/config -i /dev/shm/k -o IdentitiesOnly=yes' \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/dev/shm/stg \
  /opt/hermes/.venv/bin/python /dev/shm/run.py 2>&1 | tail -15
echo '--- scratch results ---'
cat /dev/shm/fh/.agent-config/applied 2>&1 | tr -d '\n'; echo
ls /dev/shm/fh/profiles/monitor 2>&1 | tr '\n' ' '; echo
/opt/hermes/.venv/bin/python -c \"import json;print('root jobs', len(json.load(open('/dev/shm/fh/cron/jobs.json'))['jobs']))\" 2>&1
/opt/hermes/.venv/bin/python -c \"import json;j=json.load(open('/dev/shm/fh/profiles/monitor/cron/jobs.json'))['jobs'];print('monitor jobs', [x['id'] for x in j])\" 2>&1
rm -rf /dev/shm/stg /dev/shm/fh /dev/shm/sync /dev/shm/k /dev/shm/run.py; echo 'scratch cleaned'
"
```

Expected, all of:
- the log contains `installed profile monitor` and ends `applied ref <NEWREF> (result: ok)`, followed by `main() returned 0`
- the applied record shows `"profiles": ["monitor"]` and `"result": "ok"`
- `/dev/shm/fh/profiles/monitor` lists `SOUL.md`, `config.yaml`, `assets`, `cron`
- `root jobs 0` and `monitor jobs ['6270d3f018f2']`

The scratch root store starts empty, so `root jobs 0` checks only the empty declaration here. Retirement from a populated root store is covered by the unit test `test_main_moves_a_job_from_root_to_a_profile_and_records_it` and verified live in Task 6.

**If any expectation fails, STOP.** Fix it in Task 3 and re-run this step. Do not deploy.

---

### Task 6: Deploy and verify the monitor runs as itself

**Files:**
- Modify: `infra/hermes-agent/deployment.yaml` — `AGENT_CONFIG_REF`

**Gate:** Task 5 passed. `kubectl get replicationsource hermes-data -n hermes -o jsonpath='{.status.lastSyncTime}'` is within 36h.

- [ ] **Step 1: Record the monitor's user data before deploying**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c \
  'cd /opt/data/profiles/monitor && find memories -type f -exec md5sum {} \; | sort; md5sum profile.yaml'
```

Save the output. Step 4 compares against it.

- [ ] **Step 2: Pin the new plder SHA and push, in ONE commit with Task 4**

The ConfigMap is subPath-mounted and does not live-update, so multiplex takes effect only on the pod restart this ref bump causes. Enabling multiplex and moving the job must land in the same restart, or the report has no ticking store in between.

```bash
cd /c/Users/Pol/projects/gitops-check
NEWREF=REPLACE_WITH_SHA_FROM_TASK_5
python - "$NEWREF" <<'PY'
import re, sys
p = "infra/hermes-agent/deployment.yaml"
s = open(p, encoding="utf-8").read()
new, n = re.subn(r'(- name: AGENT_CONFIG_REF\n\s*value: )"[0-9a-f]+"', rf'\1"{sys.argv[1]}"', s)
assert n == 1, f"expected exactly one AGENT_CONFIG_REF value, replaced {n}"
open(p, "w", encoding="utf-8", newline="\n").write(new)
PY
grep -A1 "name: AGENT_CONFIG_REF" infra/hermes-agent/deployment.yaml
kubectl kustomize infra/hermes-agent > /dev/null && echo "BUILD OK"
git add infra/hermes-agent/deployment.yaml
git commit -m "hermes: pin plder $NEWREF (monitor profile + multiplex)"
git log --oneline -3   # Task 3, Task 4 and this commit all present
git push origin main
```

Expected: `grep` shows `value: "<NEWREF>"`, and the push includes the Task 3 and Task 4 commits.

- [ ] **Step 3: Watch the rollout and read profile-sync's own log**

```bash
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n hermes --timeout=600s
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl logs -n hermes $POD -c profile-sync
```

Expected: `installed profile monitor`, a root line `cron: 0 job(s) in /opt/data/cron/jobs.json`, a monitor line `cron: 1 job(s) in /opt/data/profiles/monitor/cron/jobs.json`, and `applied ref <NEWREF> (result: ok)`.

Pod Ready is not proof — plan 1's first deploy came up healthy while the sync had failed. The log lines are the proof.

- [ ] **Step 4: Verify the stores, the persona, and untouched user data**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c '
H=/opt/hermes/.venv/bin/hermes
echo "--- root store ---";    /command/s6-setuidgid hermes env HERMES_HOME=/opt/data $H cron list 2>&1 | head -4
echo "--- monitor store ---"; /command/s6-setuidgid hermes env HERMES_HOME=/opt/data/profiles/monitor $H cron list 2>&1 | head -8
echo "--- user data ---"; cd /opt/data/profiles/monitor && find memories -type f -exec md5sum {} \; | sort; md5sum profile.yaml'
```

Expected: the root store shows no jobs. The monitor store shows `6270d3f018f2 [active]`, `0 9 * * *`, next run 09:00. The memories and `profile.yaml` hashes match Step 1 exactly.

- [ ] **Step 5: Prove the GATEWAY ticks the monitor store**

Plan 1's spike ran `hermes cron tick` by hand, which bypasses the gateway entirely. This step uses `cron run`, which only marks a job due; the gateway's own ticker must execute it.

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c '
M=/opt/data/profiles/monitor; H=/opt/hermes/.venv/bin/hermes
/command/s6-setuidgid hermes mkdir -p $M/scripts
printf "%s\n" "#!/bin/bash" "echo PROBE_HOME=\$HERMES_HOME" "echo PROBE_SOUL=\$(head -c 40 \$HERMES_HOME/SOUL.md)" > $M/scripts/probe.sh
chown 10000:10000 $M/scripts/probe.sh; chmod 755 $M/scripts/probe.sh
/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron create "0 4 1 1 *" --name "gateway tick probe" --script probe.sh --no-agent --deliver local 2>&1 | grep -E "Created job"
ID=$(/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron list 2>&1 | grep -B1 "gateway tick probe" | grep -oE "^ *[0-9a-f]{12}" | tr -d " ")
/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron run $ID 2>&1 | head -1
echo "probe id: $ID"'
```

Wait 2 minutes (the ticker runs every 60 seconds), then:

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c \
  'f=$(ls -t /opt/data/profiles/monitor/cron/output/*/*.md 2>/dev/null | head -1); echo "$f"; grep -E "PROBE_" "$f"'
```

Expected: `PROBE_HOME=/opt/data/profiles/monitor` and `PROBE_SOUL=` followed by the first 40 characters of the monitor's own `SOUL.md` ("You monitor this homelab k3s cluster dai"), **not** the vendor-default root persona.

- [ ] **Step 6: Prove a monitor job can deliver to Telegram**

This resolves the one unverified fact: whether the monitor profile's own `.env` shadows `TELEGRAM_HOME_CHANNEL` in profile scope. **It sends one test message to Telegram.**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c '
M=/opt/data/profiles/monitor; H=/opt/hermes/.venv/bin/hermes
printf "%s\n" "#!/bin/bash" "echo \"monitor bot delivery test ($(date -u +%H:%M)Z) - safe to ignore\"" > $M/scripts/deliver-probe.sh
chown 10000:10000 $M/scripts/deliver-probe.sh; chmod 755 $M/scripts/deliver-probe.sh
/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron create "0 4 1 1 *" --name "telegram delivery probe" --script deliver-probe.sh --no-agent --deliver telegram 2>&1 | grep -E "Created job"
ID=$(/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron list 2>&1 | grep -B1 "telegram delivery probe" | grep -oE "^ *[0-9a-f]{12}" | tr -d " ")
/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron run $ID 2>&1 | head -1; echo "probe id: $ID"'
```

Wait 2 minutes, then check the job's delivery outcome:

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- /opt/hermes/.venv/bin/python -c "
import json
for j in json.load(open('/opt/data/profiles/monitor/cron/jobs.json'))['jobs']:
    if 'probe' in j['name']: print(j['name'], '| last_status', j.get('last_status'), '| delivery_error', j.get('last_delivery_error'))"
```

Expected: `telegram delivery probe | last_status ok | delivery_error None`, and the message arrives in Telegram.

**If delivery fails, STOP — do not guess a fix.** The likely cause is that the monitor profile's own `.env` (a vendor-example copy) shadows `TELEGRAM_HOME_CHANNEL` in profile scope, but this plan has not verified how profile scope resolves secrets. First read how `agent/secret_scope.py` and `cron/scheduler.py` (~511) resolve the home channel for a profile-scoped job, confirm the actual cause from the job's `last_delivery_error`, and only then choose a fix. Two constraints on whatever fix follows: do not hand-edit the profile `.env` (it is user-owned and no distribution installs it, so a hand edit is lost on a fresh volume), and do not invent a `config.yaml` key without finding it in the source. Leave the probe in place until the fix is verified, and note that the daily report will fail delivery the same way.

- [ ] **Step 7: Remove both probes**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c '
M=/opt/data/profiles/monitor; H=/opt/hermes/.venv/bin/hermes
for n in "gateway tick probe" "telegram delivery probe"; do
  ID=$(/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron list 2>&1 | grep -B1 "$n" | grep -oE "^ *[0-9a-f]{12}" | tr -d " ")
  [ -n "$ID" ] && /command/s6-setuidgid hermes env HERMES_HOME=$M $H cron remove $ID -y 2>&1 | head -1
done
rm -f $M/scripts/probe.sh $M/scripts/deliver-probe.sh
/command/s6-setuidgid hermes env HERMES_HOME=$M $H cron list 2>&1 | grep -cE "^ *[0-9a-f]{12}"'
```

Expected: the final count is `1` — only the daily report remains.

- [ ] **Step 8: Confirm tomorrow's 09:00 report is the real proof**

The next day, the report in Telegram must come from the monitor profile. Its output lands in `/opt/data/profiles/monitor/cron/output/6270d3f018f2/`, not the root `/opt/data/cron/output/`. It should include the BACKUP FRESHNESS check.

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c \
  'ls -t /opt/data/profiles/monitor/cron/output/6270d3f018f2/ 2>&1 | head -2; ls -t /opt/data/cron/output/6270d3f018f2/ 2>&1 | head -1'
```

Expected: a new file under the monitor profile's output dated today, and nothing newer than the move under the root output.

---

### Task 7: Route a Telegram topic to the monitor (needs the operator)

**Files:**
- Modify: `infra/hermes-agent/configmap.yaml` — `gateway.profile_routes`
- Modify: `plder/hermes/root/config.yaml` — mirrored

**Gate:** the operator chooses which Telegram topic belongs to the monitor. This task cannot pick one: the topics in use are identified only by number.

**Interfaces:**
- Produces: messages sent in the chosen topic are answered by the monitor profile with its own persona and memory. Routing matches platform + `chat_id` + `thread_id` (`gateway/profile_routing.py`); `platform` is compared as a plain string, so `telegram` works.

- [ ] **Step 1: List the known topics and have the operator choose**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- /opt/hermes/.venv/bin/python -c "
import json
for e in json.load(open('/opt/data/channel_directory.json'))['platforms'].get('telegram', []):
    print(e['id'], '| thread_id', e['thread_id'], '|', e['name'])"
```

The operator names the topic (or creates a new "Cluster Monitor" topic and sends one message there so it appears in this list). Record its `chat_id` (the number before the colon) and `thread_id`.

- [ ] **Step 2: Add the route**

Inside the `gateway:` section added in Task 4, in `infra/hermes-agent/configmap.yaml`:

```yaml
      profile_routes:
        - name: monitor-topic
          platform: telegram
          chat_id: "CHAT_ID_FROM_STEP_1"
          thread_id: "THREAD_ID_FROM_STEP_1"
          profile: monitor
```

Mirror the same block under `gateway:` in `plder/hermes/root/config.yaml`. Quote both ids as strings: routing compares strings.

- [ ] **Step 3: Verify, commit, deploy**

```bash
cd /c/Users/Pol/projects/gitops-check
python -c "import yaml; r=yaml.safe_load(yaml.safe_load(open('infra/hermes-agent/configmap.yaml'))['data']['config.yaml'])['gateway']['profile_routes'][0]; assert r['platform']=='telegram' and r['profile']=='monitor' and isinstance(r['thread_id'], str); print('OK', r)"
kubectl kustomize infra/hermes-agent > /dev/null && echo "BUILD OK"
git add infra/hermes-agent/configmap.yaml && git commit -m "hermes: route the monitor telegram topic to the monitor profile"
cd /c/Users/Pol/projects/plder && git add hermes/root/config.yaml && git commit -m "hermes: mirror the monitor topic route" && git push origin master
```

The ConfigMap does not roll the pod. Restart it deliberately: `kubectl rollout restart deployment/hermes-agent -n hermes`, then push gitops.

- [ ] **Step 4: Verify the persona answers in the topic**

The operator sends "who are you and what do you check?" in the monitor topic. Expected: the reply describes cluster monitoring (the monitor's `SOUL.md`), not the generic Hermes persona. Confirm the session landed in the profile:

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes --no-headers | grep hermes-agent | awk '{print $1}' | head -1)
kubectl exec -n hermes $POD -c hermes-agent -- sh -c 'ls -t /opt/data/profiles/monitor/sessions/ 2>&1 | head -2'
```

Expected: a session file dated now.

---

## Out of scope

Tracked in the spec, delivered by later plans: the `update-gitops` CI job; the remaining six bots (homelab-ops, shopper, research, developer, projects, meta); `skills/custom/` and meta authoring into the repo; retiring the root `config.yaml` ConfigMap mount; a monitor check for ArgoCD `ComparisonError` in controller logs.
