# Hermes agent configuration as code

Date: 2026-09-12
Status: design agreed, not yet implemented
Scope: `infra/hermes-agent/` (this repo) + `Forgenn/plder` (agent config monorepo)

## Summary

Bring Hermes agent *behaviour* — system prompts, bot/profile definitions, cron
routines, skill/tool/MCP curation — under GitOps, delivered from `plder` into the
Hermes PVC by an initContainer pinned to a commit SHA, with CI bumping that SHA the
way `loomie` already does.

Three things this delivers beyond "config in git":

1. The cluster monitor's daily report, lost to the 2026-09-08 volume corruption,
   comes back **declared** rather than hand-made.
2. Named profiles start receiving upstream skill updates — they never have.
3. A volume loss stops costing agent behaviour.

## Problem

Only `config.yaml` and `ssh_config` are declared today, via a ConfigMap. Everything
that defines behaviour lives hand-edited on the Longhorn PVC at `/opt/data`: root
`SOUL.md`, `profiles/monitor/` (profile.yaml, config.yaml, SOUL.md, memories, avatar,
skills), cron jobs, hooks, plugins.

That is the volume that suffered corruption on 2026-09-08 (engine restarted over
frozen CRs with a stale replica RW → torn reads + fsck damage). Already lost: the
monitor's daily cron job. `hermes cron list` returns "No scheduled jobs"; its outputs
ran Aug 28 – Sep 7 and `executions.db` stops Sep 8 09:01. Nothing declared it, so
nothing noticed for four days.

## Verified findings

All read from the running pod (`nousresearch/hermes-agent:v2026.8.19`, hermes_cli
0.20.5) or proven by a live spike. Items marked ⚠ corrected an earlier wrong belief.

### Distributions

1. `hermes_cli/profile_distribution.py` — profiles publish as git repos with a
   `distribution.yaml`. CLI: `hermes profile install <source> [--name] [--force] [-y]`,
   `hermes profile update <name> [--force-config] [-y]`.
2. `DEFAULT_DIST_OWNED = (SOUL.md, config.yaml, mcp.json, skills, cron,
   distribution.yaml)`. `USER_OWNED_EXCLUDE` (never touched) includes `memories/`,
   `sessions/`, `logs/`, `workspace/`, `home/`, `state.db`, `auth.json`, `.env`,
   `backups/`, `cache/`, `local/`. Authors override the owned list via
   `distribution_owned:`.
3. `distribution.yaml` must sit at the **repo root** for git-URL installs; there is no
   subdirectory support.
4. `#<ref>` pinning is documented but **not implemented** — `_stage_source` passes the
   raw URL to `git clone --depth 1`.
5. `_stage_source` accepts a **local directory**, which dissolves both (3) and (4):
   clone ourselves at a pinned SHA, install from the staged path.
6. ⚠ `install --force` calls `_copy_dist_payload(preserve_config=False)`
   unconditionally — `--force-config` exists only on `update`. Git-wins on config.yaml
   is achieved by `install --force`.
7. ⚠ There is no `hermes_cli/__main__.py`. The entrypoint is the console script
   `/opt/hermes/.venv/bin/hermes`.

### Profiles, bots and the gateway

8. `get_hermes_home()` resolves context-override → `HERMES_HOME` env → platform
   default. The pod sets `HERMES_HOME=/opt/data`, and when set, the `active_profile`
   file is ignored. **The default agent cannot be a profile**; there is one
   irreducible root home.
9. **Bot Mode** (shipped v0.20.3; this pod runs 0.20.5). A Bot *is* a profile —
   identical on disk, no separate registry. Bot Mode adds: `message_agent(target,
   message)` for bot-to-bot messaging (gated by `agent.bot_mode_protocol: true`),
   `@mention` handoff, group chats (2–6 bots, ≤3 rounds), and **routines — "plain
   Hermes cron jobs namespaced `[bot:<name>] <routine>`"**.
10. Bot Mode is **GUI-driven**. Bot creation, group membership and routines are
    Desktop panes; roster/group/hidden state lives in `ui_meta` inside `profile.yaml`
    and is mirrored across gateways. New bots **share the main profile's OAuth/token
    pool** by default — so N bots do NOT need N Telegram tokens.
11. ⚠ `cron/scheduler_provider.py:526-531`: *"When `profile_homes` is set
    (multiplex_profiles on), tick EACH profile's cron store on every tick cycle so
    secondary-profile jobs actually fire… [otherwise] only the process-global
    HERMES_HOME (the default profile) is ticked."*
    **Multiplex is mandatory for a bot to be a bot.** Without it, a Desktop-created
    routine lands in the *root* store (the only one that ticks) and executes with the
    root persona.
12. ⚠ This explains the monitor entirely. Root `SOUL.md` is byte-identical to
    `/opt/hermes/docker/SOUL.md` (vendor default, `cmp` verified), the profile's
    `sessions/` is empty, and boot logs show `profile=monitor prior_state=None action=
    registered` — never started. **The monitor's SOUL, model pin and 90-entry
    `skills.disabled` have never affected anything.** An earlier belief that
    `[bot:monitor]` was a dispatch mechanism was wrong: nothing parses that prefix,
    it is a Bot Mode naming convention.

### Skills

13. `get_skills_dir()` → `HERMES_HOME/skills`. Runtime never reads the image's
    `optional-skills`; skills are PVC copies.
14. `docker/stage2-hook.sh` runs `tools/skills_sync.py` on **every container start**,
    with no args — so it syncs the **root home only**. The manifest
    (`skills/.bundled_manifest`, `name:origin_hash`) merges: new → copied; upstream
    changed + local untouched → updated; upstream changed + local modified → skipped;
    user-deleted → stays deleted.
15. Named profiles never get that sync — `seed_profile_skills()` is called only at
    profile create and from `hermes update`, which never runs in a container. Proof:
    root manifest Sep 8 22:45 (last pod start) vs monitor manifest Aug 26 22:24
    (creation). **Subagent skills are frozen; no image bump moves them.**

### Cron (proven by spike, 2026-09-12)

16. `jobs.json` schema captured by creating a throwaway job. It **interleaves
    declarative and runtime fields** in one file: `id`, `name`, `prompt`, `script`,
    `schedule`, `deliver` alongside `state`, `next_run_at`, `last_run_at`,
    `last_status`, `last_error`, `failure_streak`, `repeat.completed`,
    `monitor_state`. `cron/jobs.py` rewrites it every tick.
    **A declarative overwrite would reset the scheduler and delete jobs created
    conversationally from Telegram. Upsert-by-id is mandatory.**
17. A profile-scoped job executes under the profile's own home and persona — verified:
    a probe job in a throwaway profile reported `HERMES_HOME=/opt/data/profiles/
    spiketest` and read that profile's `SOUL.md`.
18. `hermes cron create` supports `--no-agent --script` (script stdout delivered
    verbatim, no LLM call) and `--monitor-url` / `--monitor-script` (hash-suppressed:
    unchanged output suppresses the agent run entirely). Both are cheap watchdog
    primitives.

### Operational

19. `kubectl exec` lands as **uid 0**, not `hermes`. Any write must be wrapped in
    `/command/s6-setuidgid hermes` or it leaves root-owned files on the PVC.
20. `hermes profile delete` needs root — it stats a wrapper under `/root/.local/bin`.
21. ⚠ **The Hermes volsync backup is dead.** `ReplicationSource hermes-data`:
    `lastSyncTime 2026-09-06T02:07`, synchronizing since 09-11, no mover pod. The last
    good snapshot **predates the corruption**. Everything deliberately kept out of git
    (`memories/`, `sessions/`, `state.db`) is currently unprotected.
22. `plder`'s default branch is **`master`**, not `main`.
23. An `update-gitops` CI pattern already exists (`infra/loomie/CLAUDE.md`): push →
    Actions → commit new tags into this repo with `GITOPS_TOKEN` → ArgoCD syncs.

## Design

### Repo layout

`plder` keeps its name, **becomes private**, and becomes a monorepo:

```
plder/
  package.json                 # Pi manifest — paths updated, file stays at root
  pi/                          # extensions/, skills/, themes/, settings.json, …
  hermes/
    profiles/
      monitor/  homelab-ops/  shopper/  research/  meta/
        distribution.yaml
        SOUL.md
        config.yaml
        mcp.json
        cron/jobs.json
        assets/avatar.png
    root/
      SOUL.md
      config.yaml
      cron/jobs.json
```

### Manifest

```yaml
name: monitor
version: 0.1.0
description: "Cluster Monitor — alerts, storage, ArgoCD sync; daily report"
distribution_owned:
  - SOUL.md
  - config.yaml
  - mcp.json
  - assets/
  - skills/custom/     # hand-authored skills — git owns these
```

Three deliberate exclusions, and one deliberate inclusion:

- **`skills/`** (a default) is omitted *as a whole*, but `skills/custom/` is declared.
  The 82 bundled skills are upstream content: declaring them would freeze every bot's
  skills, bloat the repo, and break `skills_sync`. What loads is curated by
  `skills.disabled` in `config.yaml`, which *is* declared. Skills **you** author are
  the opposite case — they exist nowhere but the PVC unless git holds them, which is
  the failure this whole document exists to prevent. `_copy_dist_payload` is
  path-aware ("nested entries such as `skills/research` … are honoured"), so a single
  nested entry covers them.
  The `custom/` namespace is load-bearing, not cosmetic: `_copy_entry` does
  `rmtree`-then-`copytree` on a declared directory, so declaring `skills/research/`
  to ship one hand-written skill would destroy the bundled category of that name.
  Custom skills therefore live at `skills/custom/<name>/SKILL.md` — the same two-level
  shape the loader already scans, in a category upstream will never use.
- **`profile.yaml`** is omitted. Bot Mode stores roster identity, group membership and
  `_ui_meta_revisions` there from the Desktop app; git ownership would revert your
  group chats on every restart. The avatar *image* stays git-declared via `assets/`;
  what becomes backup-protected rather than declared is the description and the
  `ui_meta` that references it.

- **`cron/`** (a default) is omitted, and so is `cron/jobs.json`. Declaring `cron/`
  wholesale would `rmtree` the directory, destroying `cron/output/` and
  `executions.db` — the artefacts that made the lost job recoverable. But declaring
  even the single nested file is wrong too: `profile install` would copy it over
  wholesale, resetting `next_run_at`/`last_status` and deleting jobs you created
  conversationally, *before* any merge step could preserve them. Cron is therefore
  owned by nothing in the manifest and applied solely by the upsert in step 4, which
  reads the declared jobs from the staged tree at
  `hermes/profiles/<n>/cron/jobs.json`.

### Delivery — `profile-sync` initContainer

Runs the hermes image (has git, the venv and `hermes_cli`), as `runAsUser: 10000`,
with `HERMES_HOME=/opt/data` set explicitly (env is not inherited from the main
container).

**Ordering and mounts.** `profile-sync` must be listed **after** `ssh-key-perms`, and
needs mounts that today exist only on the main container: the `ssh-keys` emptyDir and
the `ssh-config` ConfigMap, plus `HOME=/opt/data/home` so git resolves the
`github.com-plder` alias from `$HOME/.ssh/config` (or an explicit `GIT_SSH_COMMAND`).
Without these the clone cannot authenticate at all.

1. **Skip if unchanged.** If `/opt/data/.agent-config/applied` records both the current
   `AGENT_CONFIG_REF` **and** the current image tag / hermes version, exit 0 without
   touching the network. Keying on the ref alone would mean an image bump never
   re-syncs profile skills, leaving finding 15 half-fixed. Step 5 needs no network and
   may alternatively run unconditionally.
2. **Clone** `plder` at `$AGENT_CONFIG_REF` into an emptyDir via the plder deploy key,
   then copy the staged tree to `/opt/data/.agent-config/last-good/`. On clone failure,
   fall back to that tree, log loudly, and continue. **Never fail the pod for a
   config-fetch failure.**
3. **Install each profile**:
   `/opt/hermes/.venv/bin/hermes profile install /staging/hermes/profiles/<n> --name <n> --force -y`
4. **Upsert cron** — for every home (root and each profile), merge the declared jobs
   from `<staged>/hermes/.../cron/jobs.json` into the live `<home>/cron/jobs.json` by
   `id`: preserve runtime fields (`state`, `next_run_at`, `last_run_at`, `last_status`,
   `failure_streak`, `repeat.completed`, `monitor_state`) on existing ids, add new ids,
   and **never delete an undeclared job** — those are the ones you created from
   Telegram. Applies without a restart, since `get_due_jobs` re-reads the file every
   tick.
   Two exceptions keep the merge from being write-only: a declared `state` (e.g.
   `paused`) is **authoritative** and overrides the live value, and each declared job
   is stamped `managed_by: plder`, which lets the upsert delete ids it previously
   declared and that have since disappeared from the repo. Without that, a job can
   never be retired or disabled from git — and phase 4 in particular would leave the
   root copy of the daily report firing under the root persona alongside the monitor's.
5. **Sync profile skills** (fixes finding 15), tolerating failure exactly as
   `stage2-hook.sh` does:
   `HERMES_HOME=<profile dir> /opt/hermes/.venv/bin/python -c "from tools.skills_sync import sync_skills; sync_skills()" || warn`
6. **Copy root** `hermes/root/{SOUL.md,config.yaml}` into `/opt/data` (copied, not
   mounted — Hermes writes to those paths; the current RO ConfigMap mount is why
   `[config-migrate]` warns on every boot). Root's `cron/jobs.json` is **not** copied —
   it goes through the same upsert as every other home, for the same reason.
7. **Record** `{ref, image, applied_at, profiles, result}` to
   `/opt/data/.agent-config/applied` — recording the ref **actually applied**, not
   `$AGENT_CONFIG_REF`. If step 2 fell back to the last-good tree, recording the
   intended ref would make step 1 skip on the next restart and the new ref would never
   be fetched.

The root `config.yaml` ConfigMap mount is retired; `ssh_config` stays in this repo as
infrastructure.

### Pinning and CI

The ref lives on the initContainer as `AGENT_CONFIG_REF`, **not** in the ConfigMap —
`configmap.yaml` is a plain resource with no hash suffix, so editing it would not roll
the pod. It does not collide with Renovate's `custom.regex` manager, which only
matches `image:`.

`update-gitops` job in plder, `GITOPS_TOKEN`, `on: push: branches: [master],
paths: ['hermes/**']` — `pi/**` commits never restart the agent.

**A validation job gates the bump**: parse every YAML/JSON, and dry-run
`hermes profile install` into a temp `HERMES_HOME` using the same image. This is the
only gate between a bad commit and a deployed agent, and it is load-bearing because
the meta bot pushes directly to master (below).

### Multiplex

`GATEWAY_MULTIPLEX_PROFILES=1` plus `gateway.multiplex_profile_allowlist: [...]`.
Without this the whole roster is inert (finding 11). `agent.bot_mode_protocol` already
defaults to `True` (`config_defaults.py:212`, silent unless a profile is Desktop-managed)
— declare it for explicitness, not because it needs enabling. One Telegram bot serves all profiles; the default profile owns the
shared listener and bots are routed by chat/thread via `gateway.profile_routes` onto
the Telegram topics already in use.

## The roster

| Bot | Job | Acts | Model tier | Key mechanism |
|---|---|---|---|---|
| monitor | Cluster health, daily exception report | no | cheap | scheduled run + `--no-agent` watchdogs |
| homelab-ops | Remediation, gitops PRs, nixos changes | **yes** | strong | on-demand, escalated approvals |
| shopper | Stock/price watching, purchase research | no | mid | **`--monitor-url`** |
| research | Topic digests, papers, source following | no | strongest | `blogwatcher` + weekly digest |
| developer | Application repos — loomie, career-ops, side projects | **yes** | strong | direct push to app repos |
| projects | Physical builds: 3D printing, electronics, sim rig | no | mid | datasheets + build log |
| meta | Improves Hermes and the other bots | **yes** | strongest | direct push to plder |

Boundaries, so seven bots do not blur into one:

```
developer    → app repos            ✗ never the cluster, ✗ never plder
homelab-ops  → cluster + gitops/nixos
meta         → plder only
projects     → the build (BOM, log, design)  ──buy this──▶ shopper
research     → literature            ──findings──▶ projects, shopper
monitor      → read-only             ──incident──▶ homelab-ops
```

`developer` and `meta` are the pair most tempting to merge — meta is arguably
"developer scoped to plder". They stay separate because meta pushes to the repo that
defines every bot including itself: a different blast radius, not a different topic.

- **monitor** — toolsets `terminal, web, memory, skills`. Restores job
  `[bot:monitor] daily exception report`, `0 9 * * *`, prompt recovered verbatim from
  `cron/output/6270d3f018f2/`. Add a `--no-agent` watchdog for volsync staleness
  (finding 21). No `coding`, no `delegation`, no write; k8s RBAC already read-only.
- **homelab-ops** — `terminal, file, coding, code_execution, debugging, web, memory,
  skills, delegation`; skills `github-pr-workflow`, `github-issue-to-pr`,
  `github-repo-management`, `codebase-inspection`, `docker-management`,
  `hermes-s6-container-supervision`, `systematic-debugging`, `claude-code`. Inherits
  the existing escalation policy for cluster mutations.
- **shopper** — `web, browser, search, vision, memory, skills`; skills
  `product-price-monitor`, `shop`. Uses Hermes's own web search. One `--monitor-url`
  job per tracked item; weekly digest.
- **research** — `web, search, browser, file, memory, skills, context_engine,
  session_search`; skills `arxiv`, `blogwatcher`, `competitor-news-monitor`,
  `grounded-citations`, `research-paper-writing`, `obsidian`.
- **developer** — `file, coding, code_execution, debugging, terminal, web, search,
  memory, skills, delegation, project, todo`; skills `github-pr-workflow`,
  `github-issues`, `github-issue-to-pr`, `codebase-inspection`, `code-wiki`,
  `ast-grep`, `systematic-debugging`, `test-driven-development`,
  `requesting-code-review`, `simplify-code`, `plan`, `spike`, `claude-code`,
  `python-debugpy`, `node-inspect-debugger`, `rest-graphql-debug`,
  `subagent-driven-development`.
- **projects** — `web, search, browser, vision, file, memory, skills, kanban, todo,
  project`; skills `concept-diagrams`, `architecture-diagram`, `excalidraw`,
  `tldraw-offline`, `domain-intel`, `ocr-and-documents`, `pdf`, `nano-pdf`,
  `document-to-action-items`, `canvas`, `jupyter-notebook`. No bundled CAD / KiCad /
  3D-printing skill exists, so this is the first bot expected to need
  `skills/custom/` — the datasheet stack (`ocr-and-documents` + `pdf` + `vision`)
  covers component PDFs and photos of what was actually built.
- **meta** — `file, coding, code_execution, terminal, web, memory, skills, delegation`;
  skills `hermes-agent-skill-authoring`, `hermes-agent`, `dogfood`,
  `requesting-code-review`, `simplify-code`, `github-*`.
  **Its skill authoring writes into the repo, not the PVC** — new skills land at
  `hermes/profiles/<bot>/skills/custom/<name>/` in a plder working copy and reach the
  fleet through the normal sync. A skill authored onto `/opt/data` directly is
  invisible to git, lost with the volume, and unavailable to any other bot; authoring
  into the repo makes every skill it writes deployable to any bot by construction.

Hand-offs via `message_agent`: monitor → homelab-ops (incidents); research → shopper
and projects (findings); projects → shopper (BOM acquisition); meta → all (config).

## Credentials

- plder becomes private → a **fourth read-only deploy key**, stored in Infisical,
  surfaced by ESO into `hermes-secrets`, copied by the existing `ssh-key-perms`
  initContainer, with a `Host github.com-plder` alias in `ssh_config` (deploy keys are
  per-repo — the same reason `github.com-nixos` already exists).
- Pi installs will need auth for `pi install git:github.com/Forgenn/plder`.
- That deploy key is **read-only and belongs to the initContainer**. The meta bot
  pushes to plder, so it needs a *separate write* credential — never the sync key.
  Keeping them distinct means a compromised sync path cannot rewrite the fleet, and
  the init keeps working if the write credential is revoked.
- The developer bot pushes to several application repos. GitHub deploy keys are
  per-repo, so N repos would mean N keys mounted into one pod; use a single
  **fine-grained PAT** scoped to exactly those repos instead, stored in Infisical and
  surfaced through `envFrom` as a git credential helper. Scope it to the app repos
  only — it must not carry gitops-cluster, nixos-config or plder.
- Bots share the root profile's token pool (finding 10) — no per-bot Telegram tokens.
- Per-profile `.env` is user-owned and installed by nothing; a fresh volume yields
  profiles with no credentials. Convention: distinct env names (`SHOPPER_*`) delivered
  process-wide by `envFrom`, referenced as `${SHOPPER_*}` in profile config.

## Risks

| Risk | Mitigation |
|---|---|
| Bad commit deploys to the agent unreviewed | CI validation gate; last-good fallback; revert-the-SHA rollback (~3 min, no CI round trip) |
| **meta bot rewrites its own and others' config, live in ~4 min** | Accepted deliberately. CI validation is the only gate; `applied` record + daily drift report make changes visible after the fact |
| Self-lockout: a bad self-edit breaks the gateway used to fix it | Init never fails the pod on config errors; rollback is a git revert in this repo, not a chat message |
| Agent holds write keys to gitops-cluster | Add pushes touching `infra/hermes-agent/` to the approvals ESCALATE list |
| developer bot pushes to app repos unreviewed | Accepted deliberately. Blast radius is one project rather than the fleet, and those repos have their own CI; PAT scoped to app repos only |
| Seven bots on a 2Gi limit | Warm backends ~60MB each (default 3, 10-min idle). Raise the memory limit before phase 5, and treat the warm count as a tunable |
| First sync resets live config | **Capture before declare** — seed the repo from the live PVC verbatim as step 1 |
| Memories/sessions lost with the volume | Out of git by design; depends entirely on volsync — **currently broken, fix first** |
| `skills.disabled` is a denylist against a moving upstream | Image bumps silently enable newly-bundled skills; review on bump |

## Implementation phases

1. **Fix volsync** (`hermes-data` ReplicationSource) — prerequisite, not part of the design.
2. **Capture** live PVC state into `plder` verbatim; make the repo private; restructure
   into `pi/` + `hermes/`; add the deploy key.
3. **Root only** — init steps 1–2, **4 (root scope)**, 6–7 plus resilience. Step 4 is
   the only thing that writes cron, so the daily job cannot be restored without it.
   Verify a restart is a no-op.
4. **Monitor as a real bot** — multiplex on, allowlist, routing, job moved to the
   profile store, `skills_sync` pass. Verify the report runs with the monitor's persona.
5. **Remaining six bots**, one at a time — raise the memory limit first.
6. **CI** — `update-gitops` + validation job.

## Verification

- Restart the pod: profile files match the repo; `memories/MEMORY.md` survived;
  `.bundled_manifest` timestamps finally move for profiles.
- `hermes cron list` shows the restored job with next fire 09:00.
- A conversationally-created reminder survives a restart (proves upsert, not overwrite).
- A declared job removed from the repo disappears on the next sync, while that reminder
  still survives (proves the `managed_by` tombstone is scoped, not a blanket delete).
- A probe job in a served profile reports that profile's `HERMES_HOME` and SOUL.
- Unplug the network and restart: the pod still comes up (proves the skip/fallback).

## Open items

- Whether env expansion applies inside `jobs.json` as it does in `config.yaml` — moot
  while the repo is private and IDs can be literal; revisit if it ever goes public.
- Telegram `profile_routes` matching is exercised by Discord examples upstream; verify
  on the first routed bot.
- Whether the loader picks up `skills/custom/<name>/SKILL.md` identically to a bundled
  category — the shape matches what it already scans, but confirm on the first custom
  skill rather than assuming.
