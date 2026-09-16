# hermes-agent ops kit

Workstation helpers for the live Hermes pod (Git Bash). Not deployed: kustomization.yaml
does not reference this directory. Written by docs/plans/2026-09-14-hermes-bot-roster.md.

Rules the scripts encode: probes are scheduled, never `cron run`; rehearsals run in a
throwaway pod, never the live container; the Telegram token and deploy keys never leave
the cluster. `PROFILE` is `default` for the root home.
