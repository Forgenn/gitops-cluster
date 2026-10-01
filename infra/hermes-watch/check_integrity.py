"""Out-of-pod integrity check for Hermes (gitops docs/plans/2026-09-28-hermes-hardening.md Task 13).

Every bot runs as one uid in one pod, so a hijacked bot could rewrite another bot's
SOUL, skills, config (including the permanent `command_allowlist` approvals) or
scheduled jobs, or point the Telegram bot's webhook at itself. Nothing inside the pod
can be trusted to notice, so this runs from its own namespace with its own
service account:

1. every plder-managed file in the pod must match plder at the pinned AGENT_CONFIG_REF,
   and a managed directory may hold nothing plder does not ship;
2. every scheduled job must be declared in plder, and a declared job must still carry
   its declared prompt, schedule and the rest;
3. the Telegram bot must have no webhook set (Hermes polls; a webhook diverts it).

In the pod it runs only root-owned binaries (/bin/tar, /bin/cat) under a clean PATH:
/opt/tools/bin comes first on the pod's PATH and every bot can write to it. Hashing and
parsing happen here.

Findings go to Pol's Telegram once, again only when they change or after 24h, and a
"resolved" note follows the fix (state in ConfigMap hermes-watch/hermes-watch-state).
If the check itself cannot run, that is sent too: it fails closed. DRY=1 prints instead.
Remediation for a file mismatch (a plain restart does not re-sync an unchanged ref):
  kubectl -n hermes exec <pod> -c hermes-agent -- rm -f /opt/data/.agent-config/applied
  kubectl -n hermes rollout restart deploy/hermes-agent
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

import yaml

NS = "hermes"
HOME = "/opt/data"
STATE_NS, STATE_CM = "hermes-watch", "hermes-watch-state"
RESEND_AFTER = 86400
REMEDIATION = ("Fix a mismatch: kubectl -n hermes exec <pod> -c hermes-agent -- rm -f "
               "/opt/data/.agent-config/applied && kubectl -n hermes rollout restart deploy/hermes-agent")
# Scheduler state Hermes owns; plder's cron_upsert never lets a declaration set these.
CRON_RUNTIME = {"next_run_at", "last_run_at", "last_status", "last_error", "failure_streak",
                "monitor_state", "paused_at", "paused_reason", "created_at", "repeat"}


def run(args: list[str], check: bool = False) -> bytes:
    return subprocess.run(args, capture_output=True, check=check).stdout


def kubectl(*args: str) -> str:
    return run(["kubectl", *args], check=True).decode()


def kexec(pod: str, *cmd: str) -> bytes:
    return run(["kubectl", "-n", NS, "exec", pod, "-c", "hermes-agent", "--",
                "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", *cmd])


def parse_tar(raw: bytes) -> dict[str, bytes | None]:
    """Regular files -> bytes; anything else (symlink, device) -> None."""
    out: dict[str, bytes | None] = {}
    if not raw:
        return out
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for m in tf.getmembers():
            name = m.name.removeprefix("./")
            if m.isdir():
                continue
            out[name] = tf.extractfile(m).read() if m.isfile() else None
    return out


def managed_entries(dist: Path) -> list[str]:
    """distribution_owned entries (no trailing slash), refusing paths that leave `dist`."""
    default = ["SOUL.md", "config.yaml"]
    entries = default
    manifest = dist / "distribution.yaml"
    if manifest.is_file():
        entries = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("distribution_owned") or default
    safe = []
    for e in entries:
        p = PurePosixPath(str(e).rstrip("/"))
        if p.is_absolute() or ".." in p.parts or not p.parts:
            continue
        safe.append(str(p))
    return safe


def config_differs(want_text: str, have_text: str) -> bool:
    try:
        want = yaml.safe_load(want_text) or {}
        have = yaml.safe_load(have_text) or {}
    except yaml.YAMLError:
        return True
    for cfg in (want, have):
        skills = cfg.get("skills")
        if isinstance(skills, dict):
            skills.pop("disabled", None)
            if not skills:
                cfg.pop("skills")
    return want != have


def compare_files(name: str, dist: Path, pod: dict[str, bytes | None], ref: str) -> list[str]:
    want: dict[str, bytes] = {}
    dirs: list[str] = []
    for entry in managed_entries(dist):
        p = dist / entry
        if p.is_file():
            want[entry] = p.read_bytes()
        elif p.is_dir():
            dirs.append(entry)
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    want[f.relative_to(dist).as_posix()] = f.read_bytes()
    problems = []
    for rel, data in want.items():
        if rel not in pod:
            problems.append(f"file missing in pod: {name}/{rel}")
        elif pod[rel] is None:
            problems.append(f"not a regular file in pod: {name}/{rel}")
        elif name != "root" and rel == "config.yaml":
            # sync.py adds skills.disabled (computed from skills.allow.yaml) to each
            # profile's installed config; compare everything else as parsed YAML.
            # An added command_allowlist ("always" approval) still shows up here.
            if config_differs(data.decode("utf-8", "replace"), pod[rel].decode("utf-8", "replace")):
                problems.append(f"config differs from plder {ref} (beyond skills.disabled): {name}/config.yaml")
        elif hashlib.sha256(pod[rel]).digest() != hashlib.sha256(data).digest():
            problems.append(f"file differs from plder {ref}: {name}/{rel}")
    for rel in sorted(pod):
        if rel in want or "__pycache__" in rel.split("/"):
            continue
        if any(rel.startswith(d + "/") for d in dirs):
            problems.append(f"unexpected file in pod: {name}/{rel}")
    return problems


def compare_cron(name: str, declared: dict, live: dict) -> list[str]:
    decl = {j["id"]: j for j in (declared or {}).get("jobs", [])}
    have = {j.get("id"): j for j in (live or {}).get("jobs", [])}
    problems = []
    for jid, d in decl.items():
        job = have.get(jid)
        if job is None:
            problems.append(f"cron job missing in pod: {name}/{jid}")
            continue
        paused_live = job.get("state") == "paused" and "state" not in d
        changed = [k for k, v in d.items()
                   if k not in CRON_RUNTIME and not (k == "enabled" and paused_live) and job.get(k) != v]
        if changed:
            problems.append(f"cron job differs from plder: {name}/{jid} ({', '.join(changed)})")
    for jid, job in have.items():
        if jid in decl:
            continue
        if job.get("managed_by") != "plder":
            problems.append(f"scheduled job not declared in plder: {name}/{jid}")
        else:
            problems.append(f"cron job no longer in plder: {name}/{jid}")
    return problems


def pick_pod(pods: dict) -> str | None:
    for p in pods.get("items", []):
        st = p.get("status", {})
        if st.get("phase") != "Running" or p.get("metadata", {}).get("deletionTimestamp"):
            continue
        if any(c.get("name") == "hermes-agent" and c.get("ready") for c in st.get("containerStatuses", [])):
            return p["metadata"]["name"]
    return None


def digest(problems: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()


def should_send(problems: list[str], state: dict, now: float) -> str | None:
    if not problems:
        return "resolved" if state.get("digest") else None
    if state.get("digest") == digest(problems) and now - float(state.get("sent_at", 0)) < RESEND_AFTER:
        return None
    return "alert"


def load_state() -> dict:
    out = run(["kubectl", "-n", STATE_NS, "get", "configmap", STATE_CM, "-o", "jsonpath={.data}"])
    try:
        return json.loads(out or b"{}")
    except ValueError:
        return {}


def save_state(state: dict) -> None:
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": STATE_CM, "namespace": STATE_NS},
          "data": {k: str(v) for k, v in state.items()}}
    subprocess.run(["kubectl", "apply", "--server-side", "--force-conflicts", "--field-manager=hermes-watch",
                    "-f", "-"], input=json.dumps(cm).encode(), capture_output=True, check=True)


def telegram(text: str) -> None:
    if os.environ.get("DRY") == "1":
        print("WOULD SEND:\n" + text)
        return
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    thread = os.environ.get("TELEGRAM_THREAD_ID")
    if thread:
        body["message_thread_id"] = int(thread)
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def webhook_set() -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=20) as r:
        return json.load(r).get("result", {}).get("url", "")


def check() -> list[str] | None:
    """Findings, or None when no ready Hermes pod exists (a rollout; nothing to check)."""
    ref = kubectl("-n", NS, "get", "deploy", "hermes-agent", "-o",
                  'jsonpath={.spec.template.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}').strip()
    if not ref:
        raise RuntimeError("AGENT_CONFIG_REF not found on deploy/hermes-agent")
    pod = pick_pod(json.loads(kubectl("-n", NS, "get", "pod", "-l", "app=hermes-agent", "-o", "json")))
    if pod is None:
        print("no ready hermes-agent pod; skipping this run")
        return None
    problems: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_SSH_COMMAND=(
            "ssh -i /keys/plder_read -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={tmp}/known_hosts"))
        repo = Path(tmp) / "plder"
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "git@github.com:Forgenn/plder.git", str(repo)],
                       check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", ref], check=True, env=env)

        targets = [("root", repo / "hermes" / "root", HOME)]
        targets += [(d.name, d, f"{HOME}/profiles/{d.name}")
                    for d in sorted((repo / "hermes" / "profiles").iterdir()) if d.is_dir()]
        for name, dist, home in targets:
            entries = managed_entries(dist)
            pod_files = parse_tar(kexec(pod, "/bin/tar", "-C", home, "--ignore-failed-read", "-cf", "-", "--", *entries))
            problems += compare_files(name, dist, pod_files, ref)
            decl_file = dist / "cron" / "jobs.json"
            declared = json.loads(decl_file.read_text(encoding="utf-8")) if decl_file.is_file() else {"jobs": []}
            raw = kexec(pod, "/bin/cat", f"{home}/cron/jobs.json")
            try:
                live = json.loads(raw) if raw.strip() else {"jobs": []}
            except ValueError:
                problems.append(f"cron jobs.json unreadable in pod: {name}")
                continue
            problems += compare_cron(name, declared, live)

    if webhook_set():
        problems.append("Telegram bot has a webhook set (Hermes polls; a webhook diverts every message)")
    print(f"plder {ref}, pod {pod}: {len(problems)} problem(s)")
    for p in problems:
        print(" - " + p)
    return problems


def main() -> int:
    try:
        problems = check()
    except Exception as exc:
        msg = f"⚠️ Hermes integrity check could not run: {type(exc).__name__}: {str(exc)[:200]}"
        print(msg)
        try:
            telegram(msg)
        except Exception as send_exc:
            print(f"and the alert failed too: {type(send_exc).__name__}")
        return 2
    if problems is None:
        return 0
    state, now = load_state(), time.time()
    action = should_send(problems, state, now)
    if action == "alert":
        telegram("⚠️ Hermes integrity check\n" + "\n".join("• " + p for p in problems[:30])
                 + ("\n…" if len(problems) > 30 else "") + "\n\n" + REMEDIATION)
        save_state({"digest": digest(problems), "sent_at": now})
    elif action == "resolved":
        telegram("✅ Hermes integrity check: the earlier findings are resolved.")
        save_state({})
    return 0


if __name__ == "__main__":
    sys.exit(main())
