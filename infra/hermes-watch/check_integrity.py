"""Out-of-pod integrity check for Hermes (gitops docs/plans/2026-09-28-hermes-hardening.md Task 13).

Every bot runs as one uid in one pod, so a hijacked bot could rewrite another bot's
SOUL, skills, config (including the permanent `command_allowlist` approvals) or
scheduled jobs, or point the Telegram bot's webhook at itself. Nothing inside the pod
can be trusted to notice, so this runs from its own namespace with its own
service account:

1. every plder-managed file in the pod must match plder at the pinned AGENT_CONFIG_REF;
2. every scheduled job must be declared in plder (`managed_by: plder`);
3. the Telegram bot must have no webhook set (Hermes polls; a webhook diverts it).

Findings go to Pol's Telegram. DRY=1 prints instead of sending.
Remediation for a file mismatch (a plain restart does not re-sync an unchanged ref):
  kubectl -n hermes exec <pod> -c hermes-agent -- rm -f /opt/data/.agent-config/applied
  kubectl -n hermes rollout restart deploy/hermes-agent
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

NS = "hermes"
HOME = "/opt/data"
REMEDIATION = ("Fix a mismatch: kubectl -n hermes exec <pod> -c hermes-agent -- rm -f "
               "/opt/data/.agent-config/applied && kubectl -n hermes rollout restart deploy/hermes-agent")


def sh(*args: str, check: bool = True) -> str:
    return subprocess.run(list(args), capture_output=True, text=True, check=check).stdout


def kexec(pod: str, *cmd: str) -> str:
    return sh("kubectl", "-n", NS, "exec", pod, "-c", "hermes-agent", "--", *cmd, check=False)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def managed_files(dist: Path) -> list[str]:
    """Relative paths plder installs for one profile (files, and files under dirs)."""
    manifest = dist / "distribution.yaml"
    owned = ["SOUL.md", "config.yaml"]
    if manifest.is_file():
        owned = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("distribution_owned", owned)
    out = []
    for entry in owned:
        p = dist / entry.rstrip("/")
        if p.is_file():
            out.append(entry.rstrip("/"))
        elif p.is_dir():
            out += [str(f.relative_to(dist)).replace(os.sep, "/") for f in sorted(p.rglob("*")) if f.is_file()]
    return out


def config_differs(plder_file: Path, pod_text: str) -> bool:
    try:
        want = yaml.safe_load(plder_file.read_text(encoding="utf-8")) or {}
        have = yaml.safe_load(pod_text) or {}
    except yaml.YAMLError:
        return True
    for cfg in (want, have):
        skills = cfg.get("skills")
        if isinstance(skills, dict):
            skills.pop("disabled", None)
            if not skills:
                cfg.pop("skills")
    return want != have


def pod_hashes(pod: str, home: str, rels: list[str]) -> dict[str, str]:
    if not rels:
        return {}
    out = kexec(pod, "sh", "-c", 'cd "$0" && sha256sum -- "$@" 2>/dev/null; true', home, *rels)
    got = {}
    for line in out.splitlines():
        h, _, rel = line.partition("  ")
        got[rel.strip()] = h
    return got


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


def main() -> int:
    ref = sh("kubectl", "-n", NS, "get", "deploy", "hermes-agent", "-o",
             'jsonpath={.spec.template.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}').strip()
    pod = sh("kubectl", "-n", NS, "get", "pod", "-l", "app=hermes-agent",
             "-o", "jsonpath={.items[0].metadata.name}").strip()
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
            rels = managed_files(dist)
            have = pod_hashes(pod, home, rels)
            for rel in rels:
                if name != "root" and rel == "config.yaml":
                    # sync.py adds skills.disabled (computed from skills.allow.yaml) to each
                    # profile's installed config; compare everything else as parsed YAML.
                    # An added command_allowlist ("always" approval) still shows up here.
                    if config_differs(dist / rel, kexec(pod, "cat", f"{home}/config.yaml")):
                        problems.append(f"config differs from plder {ref} (beyond skills.disabled): {name}/config.yaml")
                    continue
                if have.get(rel) != sha(dist / rel):
                    problems.append(f"file differs from plder {ref}: {name}/{rel}" if rel in have
                                    else f"file missing in pod: {name}/{rel}")

    counter = ('import json,glob\n'
               'n=0\n'
               'for f in glob.glob("/opt/data/cron/jobs.json")+glob.glob("/opt/data/profiles/*/cron/jobs.json"):\n'
               '    try: jobs=json.load(open(f)).get("jobs",[])\n'
               '    except Exception: continue\n'
               '    n+=sum(1 for j in jobs if j.get("managed_by")!="plder")\n'
               'print(n)')
    unmanaged = kexec(pod, "/opt/hermes/.venv/bin/python", "-c", counter).strip() or "?"
    if unmanaged != "0":
        problems.append(f"scheduled jobs not declared in plder: {unmanaged}")

    try:
        url = webhook_set()
        if url:
            problems.append("Telegram bot has a webhook set (Hermes polls; a webhook diverts every message)")
    except Exception as exc:
        problems.append(f"could not check the Telegram webhook: {type(exc).__name__}")

    print(f"plder {ref}, pod {pod}: {len(problems)} problem(s)")
    for p in problems:
        print(" - " + p)
    if problems:
        telegram("⚠️ Hermes integrity check\n" + "\n".join("• " + p for p in problems[:30])
                 + ("\n…" if len(problems) > 30 else "") + "\n\n" + REMEDIATION)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
