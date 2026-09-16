#!/usr/bin/env bash
# Usage: create-topic.sh "Topic Name"   -> prints the new message_thread_id
# Calls Bot API createForumTopic INSIDE the pod with the pod's own token. The token
# never leaves the container and is never printed (the URL embeds it, so errors
# print only the HTTP code and Telegram's description).
. "$(dirname "$0")/lib.sh"
kubectl exec -i -n "$NS" "$(pod)" -c hermes-agent -- "$VENV_PY" - "$1" "$CHAT_ID" <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request
token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not token:
    sys.exit("TELEGRAM_BOT_TOKEN is not set in the container env")
body = urllib.parse.urlencode({"chat_id": sys.argv[2], "name": sys.argv[1]}).encode()
try:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/createForumTopic", data=body, timeout=30) as r:
        res = json.load(r)
except urllib.error.HTTPError as e:
    try:
        desc = json.load(e).get("description", "")
    except Exception:
        desc = ""
    sys.exit(f"createForumTopic failed: HTTP {e.code} {desc}")
except Exception as e:
    sys.exit(f"createForumTopic failed: {type(e).__name__}")
if not res.get("ok"):
    sys.exit(f"createForumTopic failed: {res.get('description')}")
print(res["result"]["message_thread_id"])
PY
