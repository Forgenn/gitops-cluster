# Hermes Bot Roster (homelab-ops, shopper, research, projects) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up four more Hermes bots — homelab-ops, shopper, research, projects — each with its own persona, model tier, toolsets, curated skills, Telegram topic and routines, one at a time, on mechanisms that stay correct across image bumps.

**Architecture:** Every bot is a plder profile distribution (`hermes/profiles/<n>/`) installed by `sync.py` and served by gateway multiplex. Plan 3 made plder the single source of the root `config.yaml` and made a plder master push deploy itself (CI validates, pins `AGENT_CONFIG_REF`, ArgoCD rolls the pod), so a bot rollout is a plder PR; gitops-cluster changes only for infrastructure. Four shared mechanisms land first: a larger memory limit; a declared **skill allowlist** (`skills.allow.yaml`) that `sync.py` turns into `skills.disabled` from the live inventory at every sync; **per-platform toolset allowlists** in each profile's config; and a git **pre-push guard** that escalates pushes touching `infra/hermes-agent/`. The monitor is migrated onto them first, as the proving ground, together with OpenRouter-pinned auxiliary models (ending the Nous-portal errors).

**Tech Stack:** Kubernetes (k3s) + Kustomize + ArgoCD v3.1.8; Python 3.12 (workstation) / 3.13 (image venv); pytest; PyYAML 6.0.3; GitHub Actions (plder `hermes-config` workflow from plan 3); `gh` CLI; Hermes Agent `nousresearch/hermes-agent:v2026.8.19` (hermes_cli 0.20.5); OpenRouter.

**Spec:** `docs/plans/2026-09-12-hermes-config-as-code.md` ("The roster", phase 5). Builds on plan 1 (`2026-09-13-hermes-config-sync-root.md`), plan 2 (`2026-09-13-hermes-monitor-bot.md`, whose "Verified facts", "Incident 2026-09-13" and "Follow-up 2026-09-14" are binding) and plan 3 (`2026-09-14-hermes-ci-single-source.md`, **assumed fully landed**). developer and meta are plan 5 (they need new push credentials).

---

## Verified facts this plan relies on

Read from the image source (`/opt/hermes`, v2026.8.19) and the live pod on 2026-09-14. Re-verify if the image changes.

### Skills
- **There is no native allowlist.** `agent/skill_utils.py::get_disabled_skill_names` reads only `skills.disabled` (global) unioned with `skills.platform_disabled.<platform>`. `tools/skills_tool.py::_find_all_skills` skips a skill whose **frontmatter `name`** (fallback: directory name) is in that set.
- `skills/.bundled_manifest` lines are `<frontmatter name>:<hash>`; `tools/skills_sync.py::sync_skills` copies every bundled skill into a home regardless of `skills.disabled`, so a disabled skill still receives upstream updates.
- **Official optional skills** (`/opt/hermes/optional-skills`) install without network through `tools/skills_sync.py::restore_official_optional_skill(name, restore=True)`: a no-op when the installed copy's hash matches the image source; otherwise it backs up the old copy under `skills/.restore-backups/` and copies the image's version in. So one idempotent call both installs and refreshes on an image bump. (`hermes skills install official/<cat>/<name>` also works but goes through the hub router.)
- Skill frontmatter may carry `metadata.hermes.requires_toolsets`; `agent/prompt_builder.py` hides such a skill when a required toolset is missing (e.g. `sdlc-review` requires `kanban`).
- A skill with `platforms:` not matching `linux` is invisible (`skill_matches_platform`), e.g. `apple-notes`, `findmy`.
- The monitor's 76-entry denylist leaves exactly one effective bundled skill visible on linux: `hermes-agent` (the others left enabled are macOS-only or need `kanban`). Its store also holds one non-bundled skill, `kubernetes-monitoring-troubleshooting`.
- Root holds 12 hand-made (non-manifest) skills that exist only on the PVC: `argocd-diff-and-ownership`, `argocd-operator-drift`, `homelab-cluster-monitor`, `homelab-cluster-monitor-report`, `homelab-gitops-drive`, `homelab-nixos-flake`, `homelab-pvc-storage-migration`, `homelab-tailscale`, `homelab-tailscale-remote-access`, `homelab-telegram-gateway`, `kubernetes-gitops-storage-ops`, `kubernetes-monitoring-troubleshooting`.
- `profile_distribution._copy_dist_payload` honours nested `distribution_owned` entries (`skills/custom/`, `scripts/plder/`): `rmtree` + `copytree` of exactly that path, parents created. A declared path missing from the staged tree is skipped, **not** deleted from the PVC.

### Toolsets
- Config key is top-level **`platform_toolsets.<platform>`** (`hermes_cli/tools_config.py::_get_platform_tools`). A list naming any configurable key is an **allowlist** of configurable toolsets, plus non-configurable platform toolsets recovered automatically.
- Configurable keys in this image (`CONFIGURABLE_TOOLSETS`): `web, browser, terminal, file, code_execution, vision, video, image_gen, video_gen, bfl, x_search, tts, stt, skills, todo, memory, context_engine, session_search, clarify, delegation, cronjob, homeassistant, spotify, discord, discord_admin, yuanbao, computer_use` (plugin `a2a` is default-off). The spec's `search`, `coding`, `debugging`, `kanban`, `project` are not configurable toolsets: `search` is part of `web`; `coding` is a session posture; `kanban`/`project` are gated to kanban workers and the GUI.
- **One drift hole:** `_enable_recently_shipped_toolsets` adds toolsets in `_RECENTLY_SHIPPED_TOOLSETS` (`{"bfl"}` today) to an explicit list **unless** the platform's `known_builtin_toolsets.<platform>` list already names them. Declaring `known_builtin_toolsets.<platform>` = every configurable key closes it; `agent.disabled_toolsets` is subtracted after every rule.
- Platforms that matter: `telegram` (gateway turns, `gateway/run.py` ~28096 resolves `_get_platform_tools(user_config, platform_key)` under the routed profile's config), **`cron`** (`cron/scheduler.py::_resolve_cron_enabled_toolsets`: per-job `enabled_toolsets`, else `platform_toolsets.cron`; `cronjob`, `messaging`, `clarify` are always stripped in cron), and **`cli`** (a `hermes -p <bot> chat` hand-off session; unset means the full `hermes-cli` composite).
- **`browser` is unusable in this pod**: `tools/browser_tool.py::check_browser_requirements()` returns `False` (no `agent-browser` CLI, no cloud provider). No bot lists it.

### MCP
- Runtime MCP servers come only from config.yaml **`mcp_servers`** (`tools/mcp_tool.py` ~5494) plus plugin-portable servers (`plugins.enabled: []` everywhere). A profile `mcp.json` is distribution metadata only; nothing reads it at runtime. No profile or root config declares `mcp_servers` today.

### Routing, topics, served profiles
- `gateway/profile_routing.py`: routes match `platform` + `chat_id` + `thread_id` conjunctively with **plain string equality**, most specific first. Proven live for `thread_id "5332"` → monitor (plan 2).
- `gateway_state.json` carries `served_profiles`.
- Telegram private-chat topics: `plugins/platforms/telegram/adapter.py::_create_dm_topic` calls Bot API `createForumTopic(chat_id, name)` (Bot API 9.4 private topics; chat 7850573137 already has Topics enabled). The adapter's own named-topic path persists thread ids back into `config.yaml`, which fights a git-owned file, so topics are created by calling `createForumTopic` directly **inside the pod with the pod's token** and the numeric id is declared in plder.

### Secondary-profile `.env` and the api_server warning
- The monitor's `.env` is a byte copy of root's `.env` (the vendor `.env.example` plus a usable `API_SERVER_KEY`), made by `hermes_cli/profiles.py::backfill_profile_envs`/`--clone` in August. `hermes profile install` (what `sync.py` runs) creates **no** `.env` (`install_distribution` → `_bootstrap_user_dirs` only).
- `gateway/config.py` ~2212 enables `api_server` for any scope holding a usable `API_SERVER_KEY`, **unless** config.yaml sets `platforms.api_server.enabled: false` explicitly (`_enabled_explicit`, merged at ~1514). With it enabled, `gateway/run.py::_start_one_profile_adapters` raises `SecondaryPortBindingConfigError`, logged "Skipping secondary profile 'monitor' due to port-binding config error".
- The skip is **harmless for serving**: it only skips that profile's *secondary adapters* (which a bot must not have anyway); `served_profiles` ("eligible for shared routing, HTTP prefixes, cron, and profile runtime scope", run.py ~15201) still includes it. The declarative fix is `platforms: {api_server: {enabled: false}}` in every secondary profile config — no `.env` edit.

### Bot-to-bot messaging
- **There is no `message_agent` tool in this version.** Bot Mode messaging is `tools/bot_mode_probe.py`: on a Bot-Mode-managed install (any `profile.yaml` with `ui_meta['hermes-bots']` — the monitor has one), a session titled `Bot Chat` gets a protocol section telling the agent to message a teammate by writing the message to a file and running `hermes -p <agent> chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <file>` in the **terminal**; the reply prints on stdout. `agent.bot_mode_protocol` (default `true`, `hermes_cli/config_defaults.py:212`) gates only that prompt section.
- **`hermes` is not on the pod's `PATH`** (the Deployment sets `PATH=/opt/tools/bin:/usr/local/bin:…`); the CLI is `/opt/hermes/.venv/bin/hermes`.
- The target runs as a non-multiplexed CLI process: credentials come from its `secrets.command` helper; `approvals.single_query_mode` (default `deny`) denies flagged commands; its toolsets are `platform_toolsets.cli`.
- Consequence: only a bot **with the terminal toolset** can initiate a hand-off.

### Models
- Present in the pod's `/opt/data/provider_models_cache.json` (OpenRouter picker, fetched 2026-09-09) with prices from `models_dev_cache.json` (USD per M tokens, input/output):
  | Tier | Model id | In / Out | Input modalities |
  |---|---|---|---|
  | cheap | `deepseek/deepseek-v4-flash-0731` | 0.06 / 0.12 | text |
  | mid | `google/gemini-3.8-flash` | 0.75 / 3.75 | text, image, video, audio, pdf |
  | strong | `anthropic/claude-sonnet-5` | 2 / 10 | text, image, pdf |
  | strongest | `anthropic/claude-opus-5` | 5 / 25 | text, image, pdf |
  Considered and not chosen: `deepseek/deepseek-v4-pro-0813` (0.66/1.98, text-only), `openai/gpt-5.6-sol` (2/10), `anthropic/claude-fable-5.1` (10/50).
- `fallback_model: {provider, model}` is a valid top-level key (`hermes_cli/config.py` ~2117). A cron job's own `model`/`provider` fields pin it (`cron.model_drift_guard` only engages for unpinned jobs with a creation-time snapshot, which declared jobs do not carry).

### Auxiliary models and the Nous errors
- Auxiliary tasks are configured at `auxiliary.<task>.{provider,model}` (`hermes_cli/config_defaults.py` ~1013); `load_config` deep-merges over defaults (`hermes_cli/config.py::_deep_merge`), so declaring `provider`/`model` keeps the default timeouts.
- The recurring `Auxiliary Nous client unavailable … marking nous unhealthy` warnings (every boot, last seen 2026-09-14 16:29) come from the **auto** chains: vision's order is main provider (skipped: DeepSeek is text-only) → `openrouter` → `nous` (`agent/auxiliary_client.py` ~7110). `auth.json` has no Nous credentials. Pinning each task to `provider: openrouter` with an explicit model removes Nous from every path.

### Approvals
- Keys (`config_defaults.py` ~2333): `mode`, `timeout`, `cron_mode` (default `deny`), `single_query_mode` (default `deny`), `smart_policy`, `denial_breaker_threshold`, `deny`, … Read per profile via `load_config_readonly()` (`tools/approval.py::_get_approval_config`).
- **`smart_policy` only ever sees commands the detector flags.** `tools/approval.py::check_all_command_guards` returns approved immediately when neither tirith nor `DANGEROUS_PATTERNS` flags a command, and **no pattern matches `kubectl` or a plain `git push`** (only force-push, reset --hard, etc.). The root policy text about kubectl is therefore inert; the pod's RBAC is what keeps kubectl read-only. There is no config key for a user-defined "always escalate" pattern list.
- `approvals.deny` globs (fnmatch, case-insensitive) block unconditionally, before yolo/off.
- Terminal subprocesses inherit the pod env minus a provider-secret blocklist (`tools/environments/local.py::_sanitize_subprocess_env`); `CLAUDE_CODE_OAUTH_TOKEN` is deliberately kept; in a container `HOME` becomes `<profile home>/home` (`hermes_constants.get_subprocess_home`). The agent's gitops clones set `core.sshCommand` per repo, so git auth does not depend on `HOME`.
- The pod has `git 2.47.3` and **no `/etc/gitconfig`**; a system `core.hooksPath` applies to every repository the agent touches. `git push --no-verify`, `-c core.hooksPath=…`, `GIT_CONFIG_NOSYSTEM` and `GIT_CONFIG_SYSTEM` bypass it — hence the matching `approvals.deny` rules.

### Cron
- `hermes cron create` supports `--no-agent --script`, `--monitor-url` (bounded GET, output hashed, unchanged = agent run suppressed), `--model/--provider`, `--deliver platform:chat:thread`. `--script` paths resolve under `<home>/scripts/` and may contain subdirectories (`cron/scheduler.py` ~3780); non-`.sh` scripts run with the venv interpreter.
- A response of exactly `[SILENT]` suppresses delivery (`cron/scheduler.py` ~554).
- The `cronjob` agent tool accepts `monitor_url` (`tools/cronjob_tools.py` ~1215), so shopper can create watch jobs from chat.
- Memory is loaded for cron agents; `load_soul_identity=True`.

### Capacity (2026-09-14, pod `hermes-agent-55dc8648fd-pvhlv`, 27 min after start, serving default + monitor)
- Container: gateway RSS 265 MiB, dashboard 136 MiB; cgroup `memory.current` 475 MiB (anon 392 MiB, file 71 MiB), `memory.peak` 678 MiB; limit 2 GiB, request 512 Mi.
- Prometheus `container_memory_working_set_bytes`, 30-day max per pod: 283–897 MiB for most pods, **1176 MiB** peak (pod `588bf6d4b9-5wlkr`).
- Nodes: allocatable 12.57 GiB each; usage cuno 65 %, dubois 79 %, katsuragi 62 % (≈4.4 / 2.6 / 4.8 GiB free); memory requests 47–54 %.
- No "warm backends" setting exists in this gateway's config (none in `config_defaults.py`); the in-process cost of a served profile is config and state, and the real bursts are processes: a `hermes -p` hand-off CLI (≈ gateway-sized, 250 MiB), Claude Code (node), `execute_code` sandboxes.

---

## Rulings (decided here; change them in plder later if wanted)

1. **Tiers:** monitor cheap; homelab-ops strong (`claude-sonnet-5`, it acts); shopper and projects mid (`gemini-3.8-flash`, native image/PDF input for product pages, photos and datasheets); research strongest (`claude-opus-5`) for conversations, but its weekly digest job is pinned to `claude-sonnet-5` to cap recurring spend. Every non-cheap bot has `fallback_model` = the cheap tier.
2. **Auxiliary models, identical in root and every profile:** `vision` and `approval` → `google/gemini-3.8-flash`; `web_extract`, `compression`, `title_generation`, `skills_hub`, `mcp`, `memory_query_rewrite` → `deepseek/deepseek-v4-flash-0731`; all `provider: openrouter`.
3. **Allowlist semantics:** `skills.disabled = (installed ∩ bundled manifest) − allowed`. Skills that are not bundled (`skills/custom/`, skills a bot authored or installed in chat) are never disabled. Declared optional skills are allowed and refreshed from the image on every sync, overwriting local edits (backed up by Hermes). Curation is log-only in `result` (like `sync_skills`) so a transient failure never forces a re-clone on every boot; verification reads the live list.
4. **No terminal or code_execution for shopper, research, projects.** Any bot with a terminal can read the pod's read-write deploy keys under `/opt/data/home/.ssh`. Consequently those bots cannot start `hermes -p` hand-offs: research→shopper/projects and projects→shopper hand-offs are **operator-relayed** (the bot ends its answer with a `Hand-off → @<bot>:` block the operator forwards). monitor→homelab-ops is automated. The kanban dispatcher (`tools/kanban_tools.py`, `toolsets: [kanban]`, root `kanban.db`) is the native no-terminal alternative; not adopted here.
5. **Escalation of pushes touching `infra/hermes-agent/`:** a pre-push hook refuses such pushes to gitops-cluster `main`/`master`; the bot pushes `hermes/<topic>` and sends the operator the compare link. Guardrail, not a boundary; `approvals.deny` blocks the known bypasses in every profile.
6. **Skill lists** (names verified against the image):
   - monitor: bundled `hermes-agent` (the effective set today).
   - homelab-ops: bundled `claude-code`, `codebase-inspection`, `hermes-agent`, `plan`, `systematic-debugging`; optional `hermes-s6-container-supervision`; custom: nine captured root skills (below). Dropped from the spec: `github-*` (no `gh`, no GitHub API token in the pod), `docker-management` (no Docker daemon; k3s runs containerd).
   - shopper: bundled `product-price-monitor`, `blocked-page-recovery`. Dropped: `shop` (a Shop-app checkout skill; shopper never buys).
   - research: bundled `arxiv`, `blocked-page-recovery`, `competitor-news-monitor`, `grounded-citations`. Dropped: `blogwatcher` (needs `blogwatcher-cli` + terminal), `research-paper-writing` (terminal/code heavy), `obsidian` (no vault).
   - projects: bundled `architecture-diagram`, `document-to-action-items`, `ocr-and-documents`; optional `concept-diagrams`. Dropped: `excalidraw`, `tldraw-offline`, `pdf`, `nano-pdf`, `jupyter-notebook`, `canvas` (need terminal/code execution), `domain-intel` (DNS recon, unrelated to builds). No custom skill yet.
7. **homelab-ops custom skills** captured verbatim from root into `hermes/profiles/homelab-ops/skills/custom/`: `argocd-diff-and-ownership`, `argocd-operator-drift`, `homelab-gitops-drive`, `homelab-nixos-flake`, `homelab-pvc-storage-migration`, `homelab-tailscale`, `homelab-tailscale-remote-access`, `kubernetes-gitops-storage-ops`, `kubernetes-monitoring-troubleshooting`. Root keeps its copies. The monitor-report and telegram-gateway skills stay root-only.
8. **Toolsets** (every bot declares `cli`, `telegram`, `cron`; `known_builtin_toolsets` = all 27 configurable keys for each):
   | Bot | telegram | cli | cron |
   |---|---|---|---|
   | monitor | terminal file web skills memory todo session_search clarify | same − clarify | terminal file web skills memory todo |
   | homelab-ops | terminal file code_execution web skills memory todo session_search clarify delegation | same − clarify | same − clarify − session_search |
   | shopper | web vision skills memory todo session_search clarify cronjob | same − clarify | web vision skills memory |
   | research | web file vision skills memory todo session_search clarify delegation | same − clarify | web file vision skills memory delegation |
   | projects | web file vision skills memory todo session_search clarify | same − clarify | web file vision skills memory |
9. **MCP:** none. `mcp_servers` is forbidden in profile configs by CI; the mechanism is documented in "Verified facts" for later.
10. **Routines:** shopper weekly digest `0 10 * * 6`; research weekly digest `0 8 * * 1`; monitor `--no-agent` volsync watchdog `30 10 * * *` (after every ReplicationSource's daily window); homelab-ops and projects none. No tracked items are seeded: the operator's memory and 4,772 session messages mention no price or stock watch. Research topics are not seeded either; both digests answer `[SILENT]` until the bot's memory holds a watchlist/topics.
11. **Topics:** created in the pod with Bot API `createForumTopic`, names `Homelab Ops`, `Shopper`, `Research`, `Projects`; ids declared in plder root config routes.
12. **No avatars** for the new bots (`profile.yaml`/`ui_meta` stays Desktop-owned).
13. **Memory:** request 1 GiB, limit 3 GiB (sizing in Task 1). Gate after every bot: 1-hour max working set < 2.25 GiB.
14. **`SYNC_VERSION` → 3** with skill curation, so the first deploy re-applies even without a ref change.

## Operator-dependent checks (batched in Task 12)

Persona answers in each new topic. Everything else is verified on the real path by the executor.

## Global Constraints

- Everything in plan 3's "Global Constraints" applies verbatim, notably: image pinned, `HERMES_HOME=/opt/data`; `sync.py` never fails the pod; hermetic tests; `s6-setuidgid hermes` for in-pod writes; `</dev/null` inside `sh -s` heredocs; **no `profile install` or sync rehearsals in the live container**; private keys never on the workstation disk; scheduled (not `cron run`) probes checked by `last_status` **and** the `## Response` section; ArgoCD `Synced` can hide a ComparisonError; **no pod rolls 08:45–09:15 UTC**; volsync `hermes-data` `lastSyncTime` within 36 h before any roll; `git pull --ff-only` before any manual gitops commit (the CI bot commits to `main`).
- **Controller-only steps** (subagents must not run them): every `git push`, PR create/merge/close, anything reading a Secret or the Telegram token into a pod, and every step touching the live pod or rolling it. They are marked **(controller)**.
- **plder is CRLF in the working tree** (`core.autocrlf=true`): compare blobs with `git show <sha>:<path> | md5sum`; scripted edits of existing files detect and preserve the file's EOL.
- Every plder change to `hermes/**` except `hermes/ci/**` restarts the agent within ~10 minutes of reaching master. Bot rollouts therefore go **branch → PR (CI validates in the image) → throwaway-pod sync rehearsal → merge**.
- **Multiplex rule (plan 2):** any change to a served profile's config, the allowlist, or credentials is verified only when a scheduled agent-mode probe passes in **every** served profile's store.
- **The Kubernetes API on dubois drops long `kubectl exec` streams** (seen repeatedly 2026-09-14). Keep in-pod output short; re-run a read-only step that dies with `connection forcibly closed`; never re-run a write step without first checking whether it already applied.
- **Plan 3 status this plan assumes.** Part A is live (gitops `a1cd7bd`: `sync.py` installs plder `hermes/root/config.yaml`, `SYNC_VERSION = 2`, ConfigMap `hermes-config` holds only `ssh_config`; plder `306f2fe` pins `_config_version: 38`). Part B (`hermes/ci/validate.py`, `bump_ref.py`, `image_checks.sh`/`.py`, `.github/workflows/hermes-config.yml`) must be **merged to plder master before Task 2 Step 6**. Tasks 4 and 6 edit `validate.py`, `test_validate.py` and `image_checks.py`: read the merged versions first and, if review changed their shape, put the two roster call sites at the equivalent points (end of `validate()`, after the manifest loop in `image_checks.main`).
- **Shell state does not survive between tool calls.** Values printed by one step (job ids, fire times, thread ids, `DEPLOY_AT`, SHAs) appear as `REPLACE_…` in later steps; set them from the recorded output.
- Operator decisions in `.superpowers/planning/common-context.md` ("Operator decisions 2026-09-14") concern plan 5 (developer PAT scope) and plder documentation; nothing here conflicts with them.
- Workstation paths: gitops-check `C:\Users\Pol\projects\gitops-check` (= gitops-cluster clone), plder `C:\Users\Pol\projects\plder`. Git Bash needs `export MSYS_NO_PATHCONV=1` (the ops scripts set it).

## Recorded at execution (fill in)

| Item | Value |
|---|---|
| `HOMELAB_THREAD` | |
| `SHOPPER_THREAD` | |
| `RESEARCH_THREAD` | |
| `PROJECTS_THREAD` | |
| plder merge SHAs (T6, T7, T8, T9, T10, T11) | |
| gitops SHAs (T1, T3, T5) | |
| Memory 1 h max after each bot | |

---

### Task 1: Raise the pod memory limit (gitops-cluster)

**Files:**
- Modify: `infra/hermes-agent/deployment.yaml` (main container `resources`)

**Sizing.** Observed peak 1176 MiB over 30 days while serving two profiles. Four more served profiles add little in-process, but each can start its own turns concurrently, and the expensive events are processes: a `hermes -p` hand-off CLI (~250 MiB, the gateway's own size), Claude Code from homelab-ops (node, ~300–400 MiB), `execute_code` sandboxes. Plausible worst case ≈ 1.2 GiB observed + one hand-off 0.25 + Claude Code 0.4 + two more concurrent turns 0.2 ≈ 2.05 GiB, plus ~70 MiB page cache in the working set. A 3 GiB limit leaves ~30 % headroom over that without letting a runaway eat a node: dubois (where the pod runs) has ≈2.6 GiB free, enough for growth from today's 0.46 GiB to 3 GiB. The request rises to 1 GiB (≈ 2× today's steady state, which will grow with four bots) so the scheduler accounts for real use; node requests are 47–54 %, so it fits anywhere. Task 12 re-reads Prometheus; above 2.25 GiB (75 % of the limit) the rollout stops.

- [ ] **Step 1: Record the baseline**

```bash
export MSYS_NO_PATHCONV=1
kubectl top pod -n hermes --containers
q='max_over_time(container_memory_working_set_bytes{namespace="hermes",container="hermes-agent"}[30d])'
kubectl get --raw "/api/v1/namespaces/monitoring/services/prometheus-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=$(python -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "max($q)")" \
  | python -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print("30d max MiB:", round(float(r[0]["value"][1])/2**20))'
kubectl top nodes
```

Expected: roughly the numbers in "Capacity". Save them in the Task 12 record.

- [ ] **Step 2: Edit the resources**

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only
python - <<'PY'
p = "infra/hermes-agent/deployment.yaml"
s = open(p, encoding="utf-8").read()
old = """          resources:
            requests:
              cpu: 200m
              memory: 512Mi
            limits:
              cpu: 2000m
              memory: 2Gi
"""
new = """          # Sized 2026-09-14 for six served bots (docs/plans/2026-09-14-hermes-bot-roster.md,
          # Task 1): 30-day peak 1176Mi with two profiles; bursts are processes (a
          # `hermes -p` hand-off CLI ~250Mi, Claude Code, execute_code). Stop adding
          # bots if the 1h max working set passes 2.25Gi.
          resources:
            requests:
              cpu: 200m
              memory: 1Gi
            limits:
              cpu: 2000m
              memory: 3Gi
"""
assert s.count(old) == 1, "resources block not found verbatim"
open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
PY
kubectl kustomize infra/hermes-agent | python -c "
import sys, yaml
d = next(x for x in yaml.safe_load_all(sys.stdin) if x and x['kind'] == 'Deployment')
c = next(c for c in d['spec']['template']['spec']['containers'] if c['name'] == 'hermes-agent')
print(c['resources'])"
```

Expected: `{'requests': {'cpu': '200m', 'memory': '1Gi'}, 'limits': {'cpu': '2000m', 'memory': '3Gi'}}`

- [ ] **Step 3: Commit**

```bash
git add infra/hermes-agent/deployment.yaml
git commit -m "hermes: raise the agent memory limit to 3Gi for the bot roster"
```

- [ ] **Step 4 (controller): Push, roll, verify**

Gates: not 08:45–09:15 UTC; volsync within 36 h.

```bash
cd /c/Users/Pol/projects/gitops-check && git pull --rebase origin main && git push origin main
GITOPS_SHA=$(git rev-parse HEAD)
export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} conditions={.status.conditions}{"\n"}'
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
POD=$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
kubectl get pod -n hermes $POD -o jsonpath='{.spec.containers[?(@.name=="hermes-agent")].resources}{"\n"}'
kubectl exec -n hermes $POD -c hermes-agent -- cat /sys/fs/cgroup/memory.max
kubectl logs -n hermes $POD -c profile-sync | tail -2
```

Expected: `Synced Healthy conditions=`; resources show 1Gi/3Gi; `memory.max` = `3221225472`; profile-sync `… already applied; skipping`.

Then run the plan 3 agent-probe kit (Create, wait, Check, Remove) for root and monitor. Expected: both pass.

---
### Task 2: Workstation ops kit (gitops-cluster, not deployed)

Plans 2 and 3 repeated the same probe, rehearsal and deploy-wait blocks inline; four bots would repeat them four more times. They become small scripts, exercised against root and monitor here before any bot depends on them.

**Files:**
- Create: `infra/hermes-agent/ops/lib.sh`, `create-topic.sh`, `agent-probe.sh`, `noagent-probe.sh`, `probe-result.sh`, `probe-cleanup.sh`, `profile-state.sh`, `profile_state.py`, `rehearse-sync.sh`, `wait-deploy.sh`, `mem.sh`, `add_route.py`, `test_add_route.py`, `README.md`

`kustomization.yaml` does not reference `ops/`, so nothing here reaches the cluster and pushing it never rolls the pod.

**Interfaces (used by Tasks 3–12):**
- `ops/create-topic.sh NAME` → prints the numeric `message_thread_id`.
- `ops/agent-probe.sh PROFILE NAME PROMPT [DELIVER] [-- extra cron-create args]` → prints a job id; fires ~3 min later via the gateway ticker.
- `ops/noagent-probe.sh PROFILE NAME SCRIPT DELIVER` → prints a job id.
- `ops/probe-result.sh PROFILE ID [MARKER]` → `last_status`, `last_error`, `delivery_error`, `marker_in_response`.
- `ops/probe-cleanup.sh PROFILE ID...`
- `ops/profile-state.sh PROFILE...` → per home: model, resolved toolsets per platform, enabled skills, disabled count, `api_server_enabled`, aux vision, MCP servers.
- `ops/rehearse-sync.sh PLDER_SHA` → runs this working tree's `sync.py` against a plder clone in a throwaway pod and prints the same capability surface plus roster checks.
- `ops/wait-deploy.sh PLDER_SHA` → waits for CI, the gitops pin, ArgoCD and the rollout; prints the profile-sync log.
- `ops/mem.sh [RANGE]` → max working set of the agent container over RANGE (default `1h`).
- `python ops/add_route.py ROOT_CONFIG PROFILE CHAT_ID THREAD_ID` → appends the profile to `gateway.multiplex_profile_allowlist` and a `<profile>-topic` route, preserving comments and EOLs.

`PROFILE` is `default` for the root home.

- [ ] **Step 1: Write the failing test for `add_route.py`**

```python
# infra/hermes-agent/ops/test_add_route.py
import pytest
import yaml

from add_route import add_route

ROOT = """# Non-secret behaviour settings
model:
  default: deepseek/deepseek-v4-flash-0731
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist:
    - monitor
  profile_routes:
    - name: monitor-topic
      platform: telegram
      chat_id: "7850573137"
      thread_id: "5332"
      profile: monitor

# Credentials comment kept
secrets:
  command:
    enabled: true
"""


def test_appends_allowlist_entry_and_route():
    new = add_route(ROOT, "homelab-ops", "7850573137", "6001")
    cfg = yaml.safe_load(new)
    assert cfg["gateway"]["multiplex_profile_allowlist"] == ["monitor", "homelab-ops"]
    assert cfg["gateway"]["profile_routes"][-1] == {
        "name": "homelab-ops-topic", "platform": "telegram",
        "chat_id": "7850573137", "thread_id": "6001", "profile": "homelab-ops"}
    assert "# Credentials comment kept" in new and new.startswith("# Non-secret")


def test_preserves_crlf():
    text = ROOT.replace("\n", "\r\n")
    new = add_route(text, "shopper", "7850573137", "6002")
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")


def test_second_bot_goes_after_the_first():
    once = add_route(ROOT, "homelab-ops", "7850573137", "6001")
    twice = add_route(once, "shopper", "7850573137", "6002")
    cfg = yaml.safe_load(twice)["gateway"]
    assert cfg["multiplex_profile_allowlist"] == ["monitor", "homelab-ops", "shopper"]
    assert [r["profile"] for r in cfg["profile_routes"]] == ["monitor", "homelab-ops", "shopper"]


@pytest.mark.parametrize("profile, thread, needle", [
    ("monitor", "6001", "already"),
    ("shopper", "5332", "already routed"),
    ("shopper", "12a", "numeric"),
    ("Bad_Name", "6001", "invalid profile name"),
])
def test_refusals(profile, thread, needle):
    with pytest.raises(ValueError, match=needle):
        add_route(ROOT, profile, "7850573137", thread)


def test_refuses_a_config_without_the_blocks():
    with pytest.raises(ValueError, match="not found"):
        add_route("gateway:\n  multiplex_profiles: true\n", "shopper", "7850573137", "6002")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops 2>/dev/null || mkdir -p /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops && python -m pytest -q test_add_route.py 2>&1 | tail -3
```

Expected: `ModuleNotFoundError: No module named 'add_route'`.

- [ ] **Step 3: Implement `add_route.py`**

```python
#!/usr/bin/env python3
"""Serve one more bot: allowlist it and route its Telegram topic in plder's root config.

    python add_route.py <hermes/root/config.yaml> <profile> <chat_id> <thread_id>

Edits the text (comments and CRLF/LF survive), then re-parses both versions and
refuses to write unless exactly the allowlist entry and the route were added.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import yaml

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _block_end(lines: list[str], header: str) -> int:
    idx = next((i for i, line in enumerate(lines) if line.rstrip() == header), None)
    if idx is None:
        raise ValueError(f"{header.strip()} not found")
    indent = len(header) - len(header.lstrip())
    end = idx + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        end += 1
    while end > idx + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def add_route(text: str, profile: str, chat_id: str, thread_id: str) -> str:
    if not _PROFILE_RE.match(profile):
        raise ValueError(f"invalid profile name {profile!r}")
    if not thread_id.isdigit() or not chat_id.lstrip("-").isdigit():
        raise ValueError("chat_id and thread_id must be numeric")
    before = yaml.safe_load(text) or {}
    gateway = before.get("gateway") or {}
    if profile in (gateway.get("multiplex_profile_allowlist") or []):
        raise ValueError(f"{profile} is already in multiplex_profile_allowlist")
    for route in gateway.get("profile_routes") or []:
        if str(route.get("thread_id")) == thread_id:
            raise ValueError(f"thread {thread_id} is already routed to {route.get('profile')}")

    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)
    lines.insert(_block_end(lines, "  multiplex_profile_allowlist:"), f"    - {profile}")
    end = _block_end(lines, "  profile_routes:")
    lines[end:end] = [
        f"    - name: {profile}-topic",
        "      platform: telegram",
        f'      chat_id: "{chat_id}"',
        f'      thread_id: "{thread_id}"',
        f"      profile: {profile}",
    ]
    new = eol.join(lines)

    expected = copy.deepcopy(before)
    expected["gateway"]["multiplex_profile_allowlist"].append(profile)
    expected["gateway"]["profile_routes"].append({
        "name": f"{profile}-topic", "platform": "telegram",
        "chat_id": chat_id, "thread_id": thread_id, "profile": profile})
    if yaml.safe_load(new) != expected:
        raise ValueError("the edit changed more than the allowlist and the routes; refusing")
    return new


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        new = add_route(path.read_bytes().decode("utf-8"), argv[2], argv[3], argv[4])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path.write_bytes(new.encode("utf-8"))
    print(f"added {argv[2]} -> thread {argv[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: Run the tests**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops && python -m pytest -q test_add_route.py 2>&1 | tail -2
```

Expected: `8 passed`.

- [ ] **Step 5: Write the shell helpers**

`infra/hermes-agent/ops/lib.sh`:

```bash
# Sourced by the other ops scripts. Workstation side (Git Bash). See README.md.
set -euo pipefail
export MSYS_NO_PATHCONV=1
NS=hermes
VENV_PY=/opt/hermes/.venv/bin/python
CHAT_ID=7850573137
OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

pod() {
  kubectl get pods -n "$NS" -l app=hermes-agent --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
}

home_of() {
  case "$1" in
    default) echo /opt/data ;;
    *) echo "/opt/data/profiles/$1" ;;
  esac
}

# POSIX sh script on stdin, run as root in the main container; args become $1..$n.
# Inside, give every command that may read stdin </dev/null.
pexec() { kubectl exec -i -n "$NS" "$(pod)" -c hermes-agent -- sh -s -- "$@"; }

# Python script on stdin, run as uid 10000 in the main container.
ppy() { kubectl exec -i -n "$NS" "$(pod)" -c hermes-agent -- /command/s6-setuidgid hermes "$VENV_PY" - "$@"; }
```

`infra/hermes-agent/ops/create-topic.sh`:

```bash
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
```

`infra/hermes-agent/ops/agent-probe.sh`:

```bash
#!/usr/bin/env bash
# Usage: agent-probe.sh PROFILE NAME PROMPT [DELIVER] [-- extra `hermes cron create` args]
# Schedules an agent job ~3 minutes ahead so the GATEWAY's ticker runs it. Never
# `cron run`: that executes inline in the CLI and proves nothing about the gateway.
. "$(dirname "$0")/lib.sh"
profile=$1; name=$2; prompt=$3; deliver=${4:-local}
shift $(( $# < 4 ? $# : 4 )); if [ "${1:-}" = "--" ]; then shift; fi
when="$(date -u -d "@$(( $(date +%s) + 180 ))" +"%M %H") * * *"
pexec "$(home_of "$profile")" "$name" "$when" "$prompt" "$deliver" "$@" <<'SH'
home=$1; name=$2; when=$3; prompt=$4; deliver=$5; shift 5
/command/s6-setuidgid hermes env HERMES_HOME="$home" /opt/hermes/.venv/bin/hermes \
  cron create "$when" "$prompt" --name "$name" --deliver "$deliver" "$@" </dev/null >/dev/null 2>&1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python - "$home" "$name" <<'PY'
import json, sys
ids = [j["id"] for j in json.load(open(sys.argv[1] + "/cron/jobs.json"))["jobs"] if j.get("name") == sys.argv[2]]
print(ids[-1] if ids else "NOT-CREATED")
PY
SH
echo "fires at UTC ${when% \* \* \*} (now $(date -u +%H:%M))" >&2
```

`infra/hermes-agent/ops/noagent-probe.sh`:

```bash
#!/usr/bin/env bash
# Usage: noagent-probe.sh PROFILE NAME SCRIPT DELIVER   (SCRIPT relative to <home>/scripts/)
. "$(dirname "$0")/lib.sh"
profile=$1; name=$2; script=$3; deliver=$4
when="$(date -u -d "@$(( $(date +%s) + 180 ))" +"%M %H") * * *"
pexec "$(home_of "$profile")" "$name" "$when" "$script" "$deliver" <<'SH'
home=$1; name=$2; when=$3; script=$4; deliver=$5
/command/s6-setuidgid hermes env HERMES_HOME="$home" /opt/hermes/.venv/bin/hermes \
  cron create "$when" --name "$name" --script "$script" --no-agent --deliver "$deliver" </dev/null >/dev/null 2>&1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python - "$home" "$name" <<'PY'
import json, sys
ids = [j["id"] for j in json.load(open(sys.argv[1] + "/cron/jobs.json"))["jobs"] if j.get("name") == sys.argv[2]]
print(ids[-1] if ids else "NOT-CREATED")
PY
SH
echo "fires at UTC ${when% \* \* \*} (now $(date -u +%H:%M))" >&2
```

`infra/hermes-agent/ops/probe-result.sh`:

```bash
#!/usr/bin/env bash
# Usage: probe-result.sh PROFILE ID [MARKER]
. "$(dirname "$0")/lib.sh"
ppy "$(home_of "$1")" "$2" "${3:-}" <<'PY'
import glob, json, os, sys
home, jid, marker = sys.argv[1:4]
job = {j["id"]: j for j in json.load(open(f"{home}/cron/jobs.json"))["jobs"]}.get(jid)
if job is None:
    sys.exit(f"{jid}: no such job in {home}")
outs = sorted(glob.glob(f"{home}/cron/output/{jid}/*.md"), key=os.path.getmtime)
text = open(outs[-1], encoding="utf-8").read() if outs else ""
body = text.split("## Response", 1)[1] if "## Response" in text else text
print(f"{jid} last_status={job.get('last_status')} last_error={job.get('last_error')} "
      f"delivery_error={job.get('last_delivery_error')} outputs={len(outs)} "
      f"monitor_state={'set' if job.get('monitor_state') else None}")
if marker:
    print("marker_in_response:", marker in body)
print("response:", body.strip()[:300].replace("\n", " | "))
PY
```

`infra/hermes-agent/ops/probe-cleanup.sh`:

```bash
#!/usr/bin/env bash
# Usage: probe-cleanup.sh PROFILE ID [ID...]
. "$(dirname "$0")/lib.sh"
profile=$1; shift
pexec "$(home_of "$profile")" "$@" <<'SH'
home=$1; shift
for id in "$@"; do
  /command/s6-setuidgid hermes env HERMES_HOME="$home" /opt/hermes/.venv/bin/hermes cron remove "$id" </dev/null 2>&1 | tail -1
done
SH
```

`infra/hermes-agent/ops/profile_state.py`:

```python
"""Print the effective capability surface of Hermes homes.

    python - HOME [HOME...] < profile_state.py      (in the pod or a rehearsal pod)

Each home is inspected in a fresh interpreter with HERMES_HOME set: Hermes caches
config and the skill index per process. Read-only; run it as uid 10000.
"""
import os
import subprocess
import sys

PROBE = r'''
import json
from hermes_cli.config import load_config_readonly
from hermes_cli.tools_config import _get_platform_tools
from tools.skills_tool import _find_all_skills
from gateway.config import load_gateway_config, Platform
cfg = load_config_readonly() or {}
api = load_gateway_config().platforms.get(Platform.API_SERVER)
print(json.dumps({
    "model": (cfg.get("model") or {}).get("default"),
    "toolsets": {p: sorted(_get_platform_tools(cfg, p)) for p in ("telegram", "cli", "cron")},
    "skills_enabled": sorted(s["name"] for s in _find_all_skills()),
    "skills_disabled_count": len((cfg.get("skills") or {}).get("disabled") or []),
    "api_server_enabled": bool(api and api.enabled),
    "aux_vision": (cfg.get("auxiliary") or {}).get("vision"),
    "mcp_servers": sorted((cfg.get("mcp_servers") or {}).keys()),
}, indent=1))
'''

for home in sys.argv[1:]:
    r = subprocess.run(["/opt/hermes/.venv/bin/python", "-c", PROBE], cwd="/opt/hermes",
                       env={**os.environ, "HERMES_HOME": home},
                       capture_output=True, text=True, timeout=180)
    print(f"== {home} (exit {r.returncode})")
    print(r.stdout.strip() if r.returncode == 0 else r.stderr.strip()[-1500:])
```

`infra/hermes-agent/ops/profile-state.sh`:

```bash
#!/usr/bin/env bash
# Usage: profile-state.sh PROFILE [PROFILE...]
. "$(dirname "$0")/lib.sh"
homes=(); for p in "$@"; do homes+=("$(home_of "$p")"); done
ppy "${homes[@]}" < "$OPS_DIR/profile_state.py"
```

`infra/hermes-agent/ops/rehearse-sync.sh`:

```bash
#!/usr/bin/env bash
# Usage: rehearse-sync.sh PLDER_SHA
# Runs THIS working tree's sync.py against a real plder clone at PLDER_SHA in a
# throwaway pod (same image, `sleep`, uid 10000, no volumes) into a scratch
# HERMES_HOME, then prints the capability surface of every home and the roster
# checks. Never touches /opt/data or the live container (s6 /run/service).
. "$(dirname "$0")/lib.sh"
ref=$1
syncdir=$(cd "$OPS_DIR/../sync" && pwd)
P="bot-rehearsal-$(date +%s)"
image=$(kubectl get deploy hermes-agent -n "$NS" -o jsonpath='{.spec.template.spec.containers[?(@.name=="hermes-agent")].image}')
cleanup() { kubectl delete pod "$P" -n "$NS" --ignore-not-found --wait=true >/dev/null; kubectl get pod "$P" -n "$NS" 2>&1 | tail -1; }
trap cleanup EXIT
kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $P
  namespace: $NS
  labels: {purpose: plan4-rehearsal}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 10000, runAsGroup: 10000}
  containers:
    - name: rehearsal
      image: $image
      command: ["sleep", "1800"]
      env:
        - {name: HOME, value: /tmp/home}
YAML
kubectl wait --for=condition=Ready "pod/$P" -n "$NS" --timeout=600s
rx() { kubectl exec -i -n "$NS" "$P" -- "$@"; }
tar cf - -C "$syncdir" sync.py cron_upsert.py | rx sh -c 'mkdir -p /tmp/sync /tmp/home /tmp/stg /tmp/fh && tar xf - -C /tmp/sync && md5sum /tmp/sync/*.py'
( cd "$syncdir" && md5sum sync.py cron_upsert.py )
kubectl get configmap hermes-config -n "$NS" -o jsonpath='{.data.ssh_config}' | rx sh -c 'cat > /tmp/ssh_config'
kubectl get secret hermes-secrets -n "$NS" -o jsonpath='{.data.PLDER_DEPLOY_KEY_READ}' | base64 -d | rx sh -c 'umask 077; cat > /tmp/k'
rx env HERMES_HOME=/tmp/fh AGENT_CONFIG_REF="$ref" AGENT_IMAGE="$image" PYTHONPATH=/tmp/sync \
  GIT_SSH_COMMAND="ssh -F /tmp/ssh_config -i /tmp/k -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/tmp/stg \
  /opt/hermes/.venv/bin/python -c 'import pathlib, sync; sync.STAGING = pathlib.Path("/tmp/stg"); raise SystemExit(sync.main())' </dev/null 2>&1 | tail -40
echo "--- applied record"; rx cat /tmp/fh/.agent-config/applied </dev/null; echo
homes=$(rx sh -c 'echo /tmp/fh; ls -d /tmp/fh/profiles/* 2>/dev/null' </dev/null | tr -d '\r' | tr '\n' ' ')
echo "--- capability surface (API_SERVER_KEY set, to prove platforms.api_server.enabled: false wins)"
rx env API_SERVER_KEY=rehearsal-dummy-key-0123456789abcdef /opt/hermes/.venv/bin/python - $homes < "$OPS_DIR/profile_state.py"
echo "--- image checks + roster (plan 3's CI step, against the clone)"
rx sh -c 'cp -r /tmp/stg/hermes /tmp/src-hermes && HERMES_HOME=/tmp/ci-home HOME=/tmp/ci-user sh /tmp/src-hermes/ci/image_checks.sh /tmp/src-hermes 2>&1 | tail -15' </dev/null
```

`infra/hermes-agent/ops/wait-deploy.sh`:

```bash
#!/usr/bin/env bash
# Usage: wait-deploy.sh PLDER_FULL_SHA
# Waits for plder CI on that commit, the gitops pin, ArgoCD and the rollout, then
# prints the profile-sync log. Run with run_in_background if foreground sleep is blocked.
. "$(dirname "$0")/lib.sh"
sha=$1
until RUN=$(gh run list --repo Forgenn/plder --workflow hermes-config.yml --json databaseId,headSha \
             --jq ".[] | select(.headSha==\"$sha\") | .databaseId" | head -1) && [ -n "$RUN" ]; do sleep 10; done
gh run watch "$RUN" --repo Forgenn/plder --exit-status
until [ "$(kubectl get deploy hermes-agent -n "$NS" -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}')" = "${sha:0:12}" ]; do sleep 15; done
GITOPS_SHA=$(gh api repos/Forgenn/gitops-cluster/commits/main --jq .sha)
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} rev={.status.sync.revision} conditions={.status.conditions}{"\n"}'
echo "gitops main: $GITOPS_SHA"
kubectl rollout status deployment/hermes-agent -n "$NS" --timeout=900s
kubectl wait --for=condition=Ready pod -l app=hermes-agent -n "$NS" --timeout=600s
kubectl logs -n "$NS" "$(pod)" -c profile-sync
```

`infra/hermes-agent/ops/mem.sh`:

```bash
#!/usr/bin/env bash
# Usage: mem.sh [RANGE]   max working set of the hermes-agent container, e.g. 1h, 24h
. "$(dirname "$0")/lib.sh"
q="max(max_over_time(container_memory_working_set_bytes{namespace=\"hermes\",container=\"hermes-agent\"}[${1:-1h}]))"
kubectl get --raw "/api/v1/namespaces/monitoring/services/prometheus-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=$(python -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$q")" \
  | python -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; m=round(float(r[0]["value"][1])/2**20); print(f"max working set: {m} MiB", "(GATE FAILED: >= 2304 MiB)" if m >= 2304 else "(ok)")'
```

`infra/hermes-agent/ops/README.md`:

```markdown
# hermes-agent ops kit

Workstation helpers for the live Hermes pod (Git Bash). Not deployed: kustomization.yaml
does not reference this directory. Written by docs/plans/2026-09-14-hermes-bot-roster.md.

Rules the scripts encode: probes are scheduled, never `cron run`; rehearsals run in a
throwaway pod, never the live container; the Telegram token and deploy keys never leave
the cluster. `PROFILE` is `default` for the root home.
```

- [ ] **Step 6: Exercise the kit against root and monitor (controller)**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
chmod +x *.sh
./profile-state.sh default monitor
./mem.sh 24h
R=$(./agent-probe.sh default "ops kit probe" "Reply with exactly OPSKIT_OK and nothing else.")
M=$(./agent-probe.sh monitor "ops kit probe" "Reply with exactly OPSKIT_OK and nothing else.")
echo "root=$R monitor=$M"
```

Expected: `profile-state` prints JSON for both homes (monitor today: `skills_disabled_count 76`, `api_server_enabled true`, `aux_vision {provider: auto …}` with default fields); `mem.sh` prints a value with `(ok)`; two 12-hex ids.

At least 2 minutes after the printed fire time:

```bash
./probe-result.sh default "$R" OPSKIT_OK; ./probe-result.sh monitor "$M" OPSKIT_OK
./probe-cleanup.sh default "$R"; ./probe-cleanup.sh monitor "$M"
```

Expected: both `last_status=ok last_error=None` and `marker_in_response: True`; each cleanup prints a removal line.

Then the rehearsal against the currently pinned ref (proves the script and records monitor's pre-migration surface):

```bash
REF=$(kubectl get deploy hermes-agent -n hermes -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}')
./rehearse-sync.sh "$REF" 2>&1 | tail -60
```

Expected: matching md5s; `installed profile monitor`; `applied ref <REF> (result: ok)`; a JSON block per home; `image checks: 0 failure(s)`; the final line `Error from server (NotFound)`.

- [ ] **Step 7: Commit and push (controller)**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent/ops
git commit -m "hermes: add workstation ops kit for probes, rehearsals and bot routes"
git pull --rebase origin main && git push origin main
```

The push changes no manifest, so ArgoCD shows no diff and the pod does not roll.

---
### Task 3: Skill curation from a declared allowlist (TDD, gitops-cluster)

**Files:**
- Modify: `infra/hermes-agent/sync/sync.py`
- Test: `infra/hermes-agent/sync/test_sync.py`

**Interfaces:**
- Consumes: plan 3's `sync.py` (`install_profiles`, `sync_skills`, `log`, `VENV_PY`, `HERMES_HOME`, `SYNC_VERSION`).
- Produces:
  - `SKILL_ALLOW_FILE = "skills.allow.yaml"`, read from the **staged** profile directory (`hermes/profiles/<n>/skills.allow.yaml`, not distribution-owned, never copied to the PVC). Schema: a mapping with only `bundled` and `optional`, each a list of skill frontmatter names.
  - `installed_skill_names(skills_dir) -> set[str]`, `bundled_manifest_names(skills_dir) -> set[str]`, `read_skill_allowlist(path) -> (bundled, optional)` (raises `ValueError`), `restore_optional_skill(home, name) -> bool`, `write_disabled_skills(config_file, disabled)`, `curate_skills(home, src) -> None`.
  - `install_profiles` calls `curate_skills(home, src)` after `sync_skills(home)`.
  - `SYNC_VERSION = 3`.
  - Log lines Tasks 6–11 assert on: `skills curated for <name>: <a> allowed, <d> bundled disabled`; `WARNING allowlisted skill(s) not installed in <name>: …`; `WARNING optional skill restore failed for <name> …`.

- [ ] **Step 1: Branch**

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only && git checkout -b hermes-skill-allowlist
```

- [ ] **Step 2: Write the failing tests**

Add `import yaml` below `import json` at the top of `infra/hermes-agent/sync/test_sync.py`, then append:

```python
# ---- skill curation from skills.allow.yaml -----------------------------------

def _skill(home, rel, name=None, quoted=False):
    d = home / "skills" / rel
    d.mkdir(parents=True, exist_ok=True)
    n = name or Path(rel).name
    shown = f'"{n}"' if quoted else n
    (d / "SKILL.md").write_text(f"---\nname: {shown}\ndescription: test\n---\nbody\n")
    return d


def _manifest(home, *names):
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "skills" / ".bundled_manifest").write_text("".join(f"{n}:0123abcd\n" for n in names))


def _allow(src, bundled=(), optional=()):
    src.mkdir(parents=True, exist_ok=True)
    (src / "skills.allow.yaml").write_text(yaml.safe_dump({"bundled": list(bundled), "optional": list(optional)}))


def _profile_home(name="shopper"):
    home = sync_module.HERMES_HOME / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _disabled(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["skills"]["disabled"]


def test_curate_skills_disables_bundled_skills_outside_the_allowlist(tmp_path):
    home = _profile_home()
    for n in ("arxiv", "pdf", "product-price-monitor"):
        _skill(home, f"cat/{n}")
    _manifest(home, "arxiv", "pdf", "product-price-monitor")
    (home / "config.yaml").write_text("model:\n  default: m\n")
    src = tmp_path / "staged-src"
    _allow(src, bundled=["product-price-monitor"])
    sync_module.curate_skills(home, src)
    assert _disabled(home) == ["arxiv", "pdf"]


def test_curate_skills_disables_a_skill_newly_bundled_by_an_image_bump(tmp_path):
    home = _profile_home()
    _skill(home, "cat/arxiv")
    _manifest(home, "arxiv")
    (home / "config.yaml").write_text("{}\n")
    src = tmp_path / "staged-src"
    _allow(src, bundled=["arxiv"])
    sync_module.curate_skills(home, src)
    assert _disabled(home) == []
    # The next image ships a new bundled skill: skills_sync copies it and records it.
    _skill(home, "cat/shiny-new")
    _manifest(home, "arxiv", "shiny-new")
    sync_module.curate_skills(home, src)
    assert _disabled(home) == ["shiny-new"]


def test_curate_skills_never_disables_custom_or_chat_created_skills(tmp_path):
    home = _profile_home()
    _skill(home, "cat/arxiv")
    _skill(home, "custom/homelab-gitops-drive")
    _skill(home, "notes/made-in-chat")
    _manifest(home, "arxiv")
    (home / "config.yaml").write_text("{}\n")
    src = tmp_path / "staged-src"
    _allow(src)
    sync_module.curate_skills(home, src)
    assert _disabled(home) == ["arxiv"]


def test_curate_skills_without_an_allowlist_leaves_config_byte_identical(tmp_path):
    home = _profile_home()
    _skill(home, "cat/arxiv")
    _manifest(home, "arxiv")
    original = "# comment kept\nskills:\n  disabled: [arxiv]\n"
    (home / "config.yaml").write_text(original)
    src = tmp_path / "staged-src"
    src.mkdir()
    sync_module.curate_skills(home, src)
    assert (home / "config.yaml").read_text() == original


@pytest.mark.parametrize("body", [
    "bundled: arxiv\n", "bundled: [arxiv]\nextra: []\n", "- arxiv\n", "bundled: [1]\n", "bundled: [\n",
], ids=["scalar", "unknown-key", "list", "non-string", "unparseable"])
def test_curate_skills_with_an_invalid_allowlist_leaves_config_untouched(tmp_path, capsys, body):
    home = _profile_home()
    _skill(home, "cat/arxiv")
    _manifest(home, "arxiv")
    (home / "config.yaml").write_text("{}\n")
    src = tmp_path / "staged-src"
    src.mkdir()
    (src / "skills.allow.yaml").write_text(body)
    sync_module.curate_skills(home, src)
    assert (home / "config.yaml").read_text() == "{}\n"
    assert "WARNING invalid" in capsys.readouterr().out


def test_curate_skills_restores_each_declared_optional_skill_and_allows_it(tmp_path, monkeypatch):
    home = _profile_home("homelab-ops")
    _skill(home, "cat/claude-code")
    _manifest(home, "claude-code")
    (home / "config.yaml").write_text("{}\n")
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, (kwargs.get("env") or {}).get("HERMES_HOME")))
        _skill(home, "devops/hermes-s6-container-supervision")  # what the restore copies in
        return MagicMock(returncode=0)

    monkeypatch.setattr(sync_module.subprocess, "run", fake_run)
    src = tmp_path / "staged-src"
    _allow(src, optional=["hermes-s6-container-supervision"])
    sync_module.curate_skills(home, src)
    assert len(calls) == 1
    cmd, env_home = calls[0]
    assert cmd[0] == sync_module.VENV_PY and cmd[-1] == "hermes-s6-container-supervision"
    assert "restore_official_optional_skill" in cmd[2]
    assert env_home == str(home)
    assert _disabled(home) == ["claude-code"]


def test_curate_skills_still_writes_config_when_an_optional_restore_fails(tmp_path, monkeypatch, capsys):
    home = _profile_home("homelab-ops")
    _skill(home, "cat/claude-code")
    _manifest(home, "claude-code")
    (home / "config.yaml").write_text("{}\n")

    def boom(cmd, *args, **kwargs):
        raise sync_module.subprocess.CalledProcessError(1, cmd, stderr=b"no such skill")

    monkeypatch.setattr(sync_module.subprocess, "run", boom)
    src = tmp_path / "staged-src"
    _allow(src, bundled=["claude-code"], optional=["hermes-s6-container-supervision"])
    sync_module.curate_skills(home, src)
    assert _disabled(home) == []
    out = capsys.readouterr().out
    assert "WARNING optional skill restore failed for hermes-s6-container-supervision" in out
    assert "WARNING allowlisted skill(s) not installed in homelab-ops: hermes-s6-container-supervision" in out


def test_curate_skills_preserves_every_other_config_value(tmp_path):
    helper = 'for f in /etc/hermes-profile-secrets/*; do IFS= read -r v < "$f" || [ -n "$v" ]; printf "%s=%s\\n" "${f##*/}" "$v"; done'
    declared = {
        "model": {"default": "m", "provider": "openrouter"},
        "skills": {"external_dirs": ["/x"]},
        "secrets": {"command": {"enabled": True, "override_existing": True, "command": helper}},
        "platform_toolsets": {"telegram": ["web", "memory"]},
        "_config_version": 38,
    }
    home = _profile_home()
    _skill(home, "cat/arxiv")
    _manifest(home, "arxiv")
    (home / "config.yaml").write_text(yaml.safe_dump(declared, sort_keys=False))
    src = tmp_path / "staged-src"
    _allow(src)
    sync_module.curate_skills(home, src)
    live = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert live["skills"] == {"external_dirs": ["/x"], "disabled": ["arxiv"]}
    del live["skills"], declared["skills"]
    assert live == declared


def test_installed_skill_names_reads_frontmatter_and_skips_support_dirs():
    home = _profile_home()
    _skill(home, "cat/folder-name", name="real-name", quoted=True)
    _skill(home, "cat/plain")
    _skill(home, "cat/plain/references/archived")    # data inside a skill, not a skill
    _skill(home, ".hub/quarantine/pending")
    nofront = home / "skills" / "cat" / "nofront"
    nofront.mkdir(parents=True)
    (nofront / "SKILL.md").write_text("no frontmatter here\n")
    assert sync_module.installed_skill_names(home / "skills") == {"real-name", "plain", "nofront"}


def test_curate_skills_without_a_bundled_manifest_does_not_write(tmp_path, capsys):
    home = _profile_home()
    _skill(home, "cat/arxiv")
    (home / "config.yaml").write_text("{}\n")
    src = tmp_path / "staged-src"
    _allow(src)
    sync_module.curate_skills(home, src)
    assert (home / "config.yaml").read_text() == "{}\n"
    assert "no bundled manifest" in capsys.readouterr().out


def test_install_profiles_curates_skills_after_the_skills_sync(monkeypatch):
    staged = sync_module.STAGING
    src = _profile(staged, "shopper")
    _allow(src, bundled=["product-price-monitor"])
    home = sync_module.HERMES_HOME / "profiles" / "shopper"
    order = []

    def run(cmd, *args, **kwargs):
        if cmd[1:3] == ["profile", "install"]:
            order.append("install")
            home.mkdir(parents=True, exist_ok=True)
            (home / "config.yaml").write_text("model:\n  default: m\n")
        elif "sync_skills" in " ".join(map(str, cmd)):
            order.append("skills_sync")
            _skill(home, "cat/product-price-monitor")
            _skill(home, "cat/arxiv")
            _manifest(home, "product-price-monitor", "arxiv")
        return MagicMock(returncode=0)

    monkeypatch.setattr(sync_module.subprocess, "run", run)
    names, ok = sync_module.install_profiles(staged)
    assert names == ["shopper"] and ok is True
    assert order == ["install", "skills_sync"]
    assert _disabled(home) == ["arxiv"]


def test_sync_version_marks_skill_curation():
    assert sync_module.SYNC_VERSION == 3
```

- [ ] **Step 3: Run to verify they fail**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest -q -k "curate or installed_skill_names or sync_version_marks" 2>&1 | tail -5
```

Expected: FAIL with `AttributeError: module 'sync' has no attribute 'curate_skills'` (and `installed_skill_names`), and `assert 2 == 3`.

- [ ] **Step 4: Implement**

(a) Set `SYNC_VERSION = 3` and extend its comment: `# 2 = root config.yaml installed from plder; 3 = skills.disabled generated from skills.allow.yaml.`

(b) After `sync_skills`, add:

```python
# ---- skill curation ------------------------------------------------------------
#
# A profile declares which upstream skills it uses in plder's
# hermes/profiles/<n>/skills.allow.yaml. Hermes itself only has a denylist
# (skills.disabled), which silently enables every skill a newer image bundles.
# So the denylist is generated here at every sync, from the live inventory:
#
#     skills.disabled = (installed AND bundled manifest) - allowed
#
# Allowed skills stay installed and keep receiving skills_sync updates; newly
# bundled skills arrive disabled; skills that are not bundled (skills/custom/,
# skills a bot authored or installed in chat) are never disabled.

SKILL_ALLOW_FILE = "skills.allow.yaml"
# Directories inside a skills tree that hold data or Hermes metadata, never skills.
_SKILL_DATA_DIRS = frozenset({
    ".hub", ".restore-backups", "_org", ".git", "index-cache",
    "references", "templates", "assets", "scripts",
})
_FRONTMATTER_NAME_RE = re.compile(r"""^name:[ \t]*(['"]?)(.+?)\1[ \t]*$""", re.M)
_GENERATED_HEADER = (
    "# Installed by profile-sync from plder hermes/profiles/<name>/config.yaml.\n"
    "# skills.disabled is GENERATED from skills.allow.yaml at every sync; edit plder, not this file.\n"
)


def _skill_name(skill_md: Path) -> str:
    """Frontmatter `name`, else the directory name -- the key Hermes filters on."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
    except OSError:
        return skill_md.parent.name
    if text.startswith("---"):
        end = text.find("\n---", 3)
        match = _FRONTMATTER_NAME_RE.search(text[3:end] if end != -1 else "")
        if match:
            return match.group(2).strip()
    return skill_md.parent.name


def installed_skill_names(skills_dir: Path) -> set[str]:
    names: set[str] = set()
    if not skills_dir.is_dir():
        return names
    for skill_md in skills_dir.rglob("SKILL.md"):
        parents = skill_md.relative_to(skills_dir).parts[:-1]
        if any(part in _SKILL_DATA_DIRS for part in parents):
            continue
        names.add(_skill_name(skill_md))
    return names


def bundled_manifest_names(skills_dir: Path) -> set[str]:
    """Names skills_sync manages in this home (`<name>:<hash>` per line)."""
    manifest = skills_dir / ".bundled_manifest"
    if not manifest.is_file():
        return set()
    return {line.split(":", 1)[0].strip()
            for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()}


def read_skill_allowlist(path: Path) -> tuple[list[str], list[str]]:
    import yaml  # in the image's venv; lazy like the other YAML readers here
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict) or set(doc) - {"bundled", "optional"}:
        raise ValueError("expected a mapping with only 'bundled' and 'optional' lists")
    lists: list[list[str]] = []
    for key in ("bundled", "optional"):
        value = doc.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"'{key}' must be a list of skill names")
        lists.append(value)
    return lists[0], lists[1]


def restore_optional_skill(home: Path, name: str) -> bool:
    """Install or refresh one official optional skill from the image (no network).

    restore_official_optional_skill(restore=True) is a no-op when the installed
    copy matches the image's source; otherwise it backs the old copy up and copies
    the image's version in -- so it both installs and updates on an image bump.
    """
    try:
        subprocess.run(
            [VENV_PY, "-c",
             "import sys\n"
             "from tools.skills_sync import restore_official_optional_skill as restore\n"
             "sys.exit(0 if restore(sys.argv[1], restore=True).get('ok') else 1)",
             name],
            env={**os.environ, "HERMES_HOME": str(home)},
            cwd="/opt/hermes", check=True, capture_output=True, timeout=120,
        )
        return True
    except Exception as exc:
        log(f"WARNING optional skill restore failed for {name} in {home}: "
            f"{type(exc).__name__}: {str(exc)[:200]}")
        return False


def write_disabled_skills(config_file: Path, disabled: list[str]) -> None:
    import yaml
    text = config_file.read_text(encoding="utf-8") if config_file.is_file() else ""
    cfg = yaml.safe_load(text) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{config_file} is not a YAML mapping")
    skills = cfg.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        cfg["skills"] = skills
    skills["disabled"] = list(disabled)
    body = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=4096)
    tmp = config_file.with_suffix(".yaml.profile-sync.tmp")
    tmp.write_text(_GENERATED_HEADER + body, encoding="utf-8")
    os.replace(tmp, config_file)


def curate_skills(home: Path, src: Path) -> None:
    """Generate home/config.yaml's skills.disabled from src/skills.allow.yaml.

    Log-only, like sync_skills: a transient failure must not flip result to
    "partial" and force a re-clone on every boot. Failing open (the git config,
    all skills enabled) is loud in the log and caught by the rollout checks.
    """
    allow_file = src / SKILL_ALLOW_FILE
    if not allow_file.is_file():
        return
    try:
        bundled, optional = read_skill_allowlist(allow_file)
    except Exception as exc:
        log(f"WARNING invalid {allow_file}; skills left uncurated for {home.name}: {exc}")
        return
    for name in optional:
        restore_optional_skill(home, name)
    skills_dir = home / "skills"
    try:
        manifest = bundled_manifest_names(skills_dir)
        installed = installed_skill_names(skills_dir)
    except Exception as exc:
        log(f"WARNING could not read the skill inventory of {home.name}: {exc}")
        return
    if not manifest:
        log(f"WARNING no bundled manifest in {skills_dir}; skills left uncurated for {home.name}")
        return
    allowed = set(bundled) | set(optional)
    missing = sorted(allowed - installed)
    if missing:
        log(f"WARNING allowlisted skill(s) not installed in {home.name}: {', '.join(missing)}")
    disabled = sorted((installed & manifest) - allowed)
    try:
        write_disabled_skills(home / "config.yaml", disabled)
        log(f"skills curated for {home.name}: {len(allowed) - len(missing)} allowed, "
            f"{len(disabled)} bundled disabled")
    except Exception as exc:
        log(f"WARNING could not write skills.disabled for {home.name}: {exc}")
```

(c) In `install_profiles`, directly after the successful-install `sync_skills(home)` call, add:

```python
        # After sync_skills on purpose: curation reads the inventory that sync
        # just produced, so a skill newly bundled by this image is disabled
        # in the same run that installed it.
        curate_skills(home, src)
```

- [ ] **Step 5: Run the full suite**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync
python -m pytest -q 2>&1 | tail -3
ls -d /c/Users/Pol/AppData/Local/hermes/profiles/shopper 2>&1 | tail -1
```

Expected: `0 failed`; `No such file or directory`.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent/sync/sync.py infra/hermes-agent/sync/test_sync.py
git commit -m "hermes: generate profile skills.disabled from a declared allowlist"
```

- [ ] **Step 7 (controller): Rehearse against the pinned ref**

No plder profile has an allowlist yet, so curation must be a no-op and everything else identical to today.

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
REF=$(kubectl get deploy hermes-agent -n hermes -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="profile-sync")].env[?(@.name=="AGENT_CONFIG_REF")].value}')
./rehearse-sync.sh "$REF" 2>&1 | grep -E "md5|installed profile|skills curated|applied ref|sync_version|skills_disabled_count|NotFound"
```

Expected: md5s match; `installed profile monitor`; **no** `skills curated` line; `applied ref <REF> (result: ok)`; `"sync_version": 3`; monitor `skills_disabled_count: 76`; `NotFound`.

- [ ] **Step 8 (controller): Merge, deploy, verify**

Gates: volsync within 36 h; not 08:45–09:15 UTC.

```bash
cd /c/Users/Pol/projects/gitops-check
git checkout main && git pull --ff-only
git merge --no-ff hermes-skill-allowlist -m "hermes: curate profile skills from declared allowlists"
git push origin main
GITOPS_SHA=$(git rev-parse HEAD)
export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} conditions={.status.conditions}{"\n"}'
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
kubectl logs -n hermes "$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')" -c profile-sync
```

Expected: `Synced Healthy conditions=`; the log re-applies (sync version changed) and ends `applied ref <REF> (result: ok)`, with no `skills curated` line. Then run the agent-probe kit for root and monitor (`ops/agent-probe.sh`, `probe-result.sh`, `probe-cleanup.sh`). Expected: both pass.

**Rollback:** `git revert -m 1 <merge sha> && git push origin main`.

---
### Task 4: Roster checks for CI (TDD, plder `hermes/ci/`)

Plan 3's `validate.py` already checks manifests, the credentials helper, `_config_version`, `cron.preflight`, allowlist/route cross-references, route id types and the job schema. This task adds only what the roster introduces, as a separate module so plan 3's file stays readable. It is **written and tested here but wired into CI in Task 6**, in the same PR that migrates the monitor, because the new rules would fail on today's monitor config.

**Files:**
- Create: `plder/hermes/ci/roster.py`
- Create: `plder/hermes/ci/test_roster.py`

**Interfaces:**
- `check_roster(hermes: Path) -> list[tuple[Path, str]]` — pure (PyYAML only); called from `validate.validate()`.
- `check_image(hermes: Path, bundled_dir=Path("/opt/hermes/skills"), optional_dir=Path("/opt/hermes/optional-skills"), toolsets: set[str] | None = None) -> list[tuple[Path, str]]` — needs the image; called from `image_checks.main()`. `toolsets=None` imports `hermes_cli.tools_config.CONFIGURABLE_TOOLSETS`.
- `skill_inventory(skills_dir) -> dict[str, list[str]]` (name → `requires_toolsets`).
- `PLATFORMS = ("cli", "telegram", "cron")`.

Rules (`check_roster`), for every profile with a `distribution.yaml`:
1. `platforms.api_server.enabled` is `false`.
2. If root declares `auxiliary`, the profile's `auxiliary` is identical.
3. Root `approvals.deny` ⊆ profile `approvals.deny`.
4. `approvals.cron_mode` / `single_query_mode`, where declared (root included), are `deny`.
5. `platform_toolsets`, if declared, has exactly `cli`, `telegram`, `cron`, each a list of strings, and `known_builtin_toolsets` declares the same three.
6. No `mcp_servers`.
7. `skills.allow.yaml`, if present: only `bundled`/`optional` string lists, no duplicates; then `config.yaml` has no `skills.disabled` and does declare `platform_toolsets`.
8. A job delivering to `telegram:<chat>:<thread>` from profile P has a route with that chat and thread to P.
9. A job `script` exists at `profiles/P/scripts/<script>` and lies under a `scripts/…` entry of `distribution_owned`.

Rules (`check_image`): allowlisted bundled/optional names exist in the image; every allowlisted skill's `requires_toolsets` ⊆ `platform_toolsets.telegram`; toolset names are known; `known_builtin_toolsets.<platform>` equals the image's full configurable set (fails on an image bump that ships a toolset, forcing review).

- [ ] **Step 1: Branch**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git checkout -b hermes-roster-checks
```

- [ ] **Step 2: Write the failing tests**

```python
# hermes/ci/test_roster.py
"""Hermetic tests for roster.py: every tree and skill inventory is built under tmp_path."""
import json
from pathlib import Path

import pytest
import yaml

import roster as r

TOOLSETS = {"web", "terminal", "file", "vision", "skills", "memory", "todo", "clarify", "cronjob", "kanban-free"}
AUX = {"vision": {"provider": "openrouter", "model": "google/gemini-3.8-flash"}}
DENY = ["*--no-verify*", "*hookspath*"]


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) if path.suffix == ".json" else yaml.safe_dump(data, sort_keys=False)
    path.write_text(text, encoding="utf-8")


def _edit(path: Path, fn) -> None:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    fn(data)
    _dump(path, data)


def _skill(root: Path, rel: str, name: str, requires=None) -> None:
    front = {"name": name, "description": "t"}
    if requires:
        front["metadata"] = {"hermes": {"requires_toolsets": requires}}
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\n" + yaml.safe_dump(front) + "---\nbody\n", encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    h = tmp_path / "hermes"
    _dump(h / "root" / "config.yaml", {
        "gateway": {"multiplex_profiles": True, "multiplex_profile_allowlist": ["shopper"],
                    "profile_routes": [{"name": "shopper-topic", "platform": "telegram",
                                        "chat_id": "7850573137", "thread_id": "6002", "profile": "shopper"}]},
        "approvals": {"mode": "smart", "cron_mode": "deny", "deny": list(DENY)},
        "auxiliary": AUX,
    })
    s = h / "profiles" / "shopper"
    _dump(s / "distribution.yaml", {"name": "shopper", "version": "0.1.0",
                                     "distribution_owned": ["SOUL.md", "config.yaml", "scripts/plder/"]})
    (s / "SOUL.md").write_text("shopper\n", encoding="utf-8")
    (s / "scripts" / "plder").mkdir(parents=True)
    (s / "scripts" / "plder" / "watch.py").write_text("print()\n", encoding="utf-8")
    lists = {"cli": ["web", "skills"], "telegram": ["web", "skills", "clarify"], "cron": ["web", "skills"]}
    _dump(s / "config.yaml", {
        "platform_toolsets": lists,
        "known_builtin_toolsets": {p: sorted(TOOLSETS) for p in lists},
        "approvals": {"mode": "smart", "cron_mode": "deny", "single_query_mode": "deny", "deny": list(DENY)},
        "auxiliary": AUX,
        "platforms": {"api_server": {"enabled": False}},
    })
    _dump(s / "skills.allow.yaml", {"bundled": ["product-price-monitor"], "optional": ["concept-diagrams"]})
    _dump(s / "cron" / "jobs.json", {"jobs": [
        {"id": "96c4f98cdc65", "deliver": "telegram:7850573137:6002", "prompt": "digest"},
        {"id": "9ef0d35042ed", "deliver": "telegram:7850573137:6002", "no_agent": True, "script": "plder/watch.py"},
    ]})
    img = tmp_path / "img"
    _skill(img / "skills", "productivity/product-price-monitor", "product-price-monitor")
    _skill(img / "skills", "devops/sdlc-review", "sdlc-review", requires=["kanban"])
    _skill(img / "optional", "creative/concept-diagrams", "concept-diagrams")
    return h


def _image(h):
    return r.check_image(h, h.parent / "img" / "skills", h.parent / "img" / "optional", TOOLSETS)


def _msgs(found):
    return [f"{p.as_posix()}: {m}" for p, m in found]


def _has(found, *needles):
    msgs = _msgs(found)
    assert any(all(n in m for n in needles) for m in msgs), f"no {needles!r} in {msgs!r}"


SHOP_CFG = "profiles/shopper/config.yaml"


def test_valid_tree_passes_both_checks(tree):
    assert _msgs(r.check_roster(tree)) == []
    assert _msgs(_image(tree)) == []


def test_api_server_must_be_disabled(tree):
    _edit(tree / SHOP_CFG, lambda c: c.pop("platforms"))
    _has(r.check_roster(tree), "shopper/config.yaml", "platforms.api_server.enabled must be false")


def test_auxiliary_must_match_root(tree):
    _edit(tree / SHOP_CFG, lambda c: c["auxiliary"]["vision"].update(model="other"))
    _has(r.check_roster(tree), "auxiliary differs from hermes/root/config.yaml")


def test_root_deny_rules_are_required(tree):
    _edit(tree / SHOP_CFG, lambda c: c["approvals"]["deny"].pop())
    _has(r.check_roster(tree), "approvals.deny is missing", "*hookspath*")


@pytest.mark.parametrize("key", ["cron_mode", "single_query_mode"])
def test_non_interactive_approval_modes_must_deny(tree, key):
    _edit(tree / SHOP_CFG, lambda c: c["approvals"].update({key: "approve"}))
    _has(r.check_roster(tree), f"approvals.{key} must be deny")


def test_platform_toolsets_need_all_three_platforms(tree):
    _edit(tree / SHOP_CFG, lambda c: c["platform_toolsets"].pop("cli"))
    _has(r.check_roster(tree), "platform_toolsets must declare exactly cli, telegram, cron")


def test_mcp_servers_are_rejected(tree):
    _edit(tree / SHOP_CFG, lambda c: c.update(mcp_servers={"x": {"command": "y"}}))
    _has(r.check_roster(tree), "mcp_servers")


def test_allowlist_and_denylist_are_exclusive(tree):
    _edit(tree / SHOP_CFG, lambda c: c.update(skills={"disabled": ["arxiv"]}))
    _has(r.check_roster(tree), "skills.disabled is generated from skills.allow.yaml")


@pytest.mark.parametrize("doc", [{"bundled": "x"}, {"bundled": ["x"], "extra": []}, ["x"], {"bundled": ["x", "x"]}])
def test_allowlist_schema(tree, doc):
    _dump(tree / "profiles" / "shopper" / "skills.allow.yaml", doc)
    _has(r.check_roster(tree), "skills.allow.yaml")


def test_allowlisted_profile_must_declare_toolsets(tree):
    _edit(tree / SHOP_CFG, lambda c: (c.pop("platform_toolsets"), c.pop("known_builtin_toolsets")))
    _has(r.check_roster(tree), "a curated profile must declare platform_toolsets")


def test_delivery_thread_must_be_routed_to_the_owner(tree):
    _edit(tree / "profiles" / "shopper" / "cron" / "jobs.json",
          lambda d: d["jobs"][0].update(deliver="telegram:7850573137:5332"))
    _has(r.check_roster(tree), "job 96c4f98cdc65", "thread 5332 is not routed to shopper")


def test_job_script_must_exist(tree):
    (tree / "profiles" / "shopper" / "scripts" / "plder" / "watch.py").unlink()
    _has(r.check_roster(tree), "job 9ef0d35042ed", "script plder/watch.py not found")


def test_job_script_must_be_distribution_owned(tree):
    _edit(tree / "profiles" / "shopper" / "distribution.yaml",
          lambda m: m.update(distribution_owned=["SOUL.md", "config.yaml"]))
    _has(r.check_roster(tree), "not covered by a scripts/ entry in distribution_owned")


def test_unknown_skills_are_rejected(tree):
    _dump(tree / "profiles" / "shopper" / "skills.allow.yaml", {"bundled": ["nope"], "optional": ["nada"]})
    found = _image(tree)
    _has(found, "bundled skill 'nope' does not exist in the image")
    _has(found, "optional skill 'nada' does not exist in the image")


def test_required_toolsets_must_be_enabled(tree):
    _dump(tree / "profiles" / "shopper" / "skills.allow.yaml", {"bundled": ["sdlc-review"]})
    _has(_image(tree), "skill 'sdlc-review' requires kanban")


def test_unknown_toolset_is_rejected(tree):
    _edit(tree / SHOP_CFG, lambda c: c["platform_toolsets"]["telegram"].append("search"))
    _has(_image(tree), "platform_toolsets.telegram", "unknown toolset(s): search")


def test_known_builtin_toolsets_must_list_the_whole_image(tree):
    _edit(tree / SHOP_CFG, lambda c: c["known_builtin_toolsets"]["cron"].remove("web"))
    _has(_image(tree), "known_builtin_toolsets.cron must list every toolset of this image", "web")


def test_skill_inventory_reads_names_and_requirements(tree):
    inv = r.skill_inventory(tree.parent / "img" / "skills")
    assert inv == {"product-price-monitor": [], "sdlc-review": ["kanban"]}
```

- [ ] **Step 3: Run to verify failure**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest -q hermes/ci/test_roster.py 2>&1 | tail -3
```

Expected: `ModuleNotFoundError: No module named 'roster'`.

- [ ] **Step 4: Implement `hermes/ci/roster.py`**

```python
#!/usr/bin/env python3
"""Roster rules for the declared Hermes bots (see gitops-cluster
docs/plans/2026-09-14-hermes-bot-roster.md, "Verified facts" and "Rulings").

check_roster() needs only PyYAML and runs inside validate.py.
check_image() needs the pinned Hermes image and runs inside image_checks.py.
Both return (path, message) pairs; the callers format and count them.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

PLATFORMS = ("cli", "telegram", "cron")
_DATA_DIRS = frozenset({".hub", ".restore-backups", "_org", ".git", "index-cache",
                        "references", "templates", "assets", "scripts"})
Found = list[tuple[Path, str]]


def _yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None  # validate.py already reports unparseable files


def _profiles(hermes: Path) -> dict[str, Path]:
    d = hermes / "profiles"
    if not d.is_dir():
        return {}
    return {p.name: p for p in sorted(d.iterdir()) if p.is_dir() and (p / "distribution.yaml").is_file()}


def _allowlist(path: Path, found: Found) -> tuple[list[str], list[str]] | None:
    if not path.is_file():
        return None
    doc = _yaml(path)
    if not isinstance(doc, dict) or set(doc) - {"bundled", "optional"}:
        found.append((path, "skills.allow.yaml must be a mapping with only 'bundled' and 'optional' lists"))
        return [], []
    lists = []
    for key in ("bundled", "optional"):
        value = doc.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            found.append((path, f"skills.allow.yaml '{key}' must be a list of skill names"))
            value = []
        lists.append(value)
    names = lists[0] + lists[1]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        found.append((path, f"skills.allow.yaml lists {', '.join(dups)} more than once"))
    return lists[0], lists[1]


def check_roster(hermes: Path) -> Found:
    found: Found = []
    root_path = hermes / "root" / "config.yaml"
    root = _yaml(root_path) or {}
    if not isinstance(root, dict):
        return found
    root_approvals = root.get("approvals") or {}
    root_deny = list(root_approvals.get("deny") or [])
    routes = (root.get("gateway") or {}).get("profile_routes") or []
    routed = {(str(x.get("chat_id")), str(x.get("thread_id"))): x.get("profile")
              for x in routes if isinstance(x, dict)}

    for key in ("cron_mode", "single_query_mode"):
        if key in root_approvals and root_approvals[key] != "deny":
            found.append((root_path, f"approvals.{key} must be deny"))

    for name, pdir in _profiles(hermes).items():
        cfg_path = pdir / "config.yaml"
        cfg = _yaml(cfg_path)
        if not isinstance(cfg, dict):
            continue
        if ((cfg.get("platforms") or {}).get("api_server") or {}).get("enabled") is not False:
            found.append((cfg_path, "platforms.api_server.enabled must be false: a secondary profile that "
                                    "enables api_server is skipped for adapters under multiplex"))
        if "auxiliary" in root and cfg.get("auxiliary") != root["auxiliary"]:
            found.append((cfg_path, "auxiliary differs from hermes/root/config.yaml; keep them identical"))
        approvals = cfg.get("approvals") or {}
        missing = [d for d in root_deny if d not in (approvals.get("deny") or [])]
        if missing:
            found.append((cfg_path, f"approvals.deny is missing root rule(s): {', '.join(missing)}"))
        for key in ("cron_mode", "single_query_mode"):
            if key in approvals and approvals[key] != "deny":
                found.append((cfg_path, f"approvals.{key} must be deny"))
        pts = cfg.get("platform_toolsets")
        if pts is not None:
            ok = isinstance(pts, dict) and set(pts) == set(PLATFORMS) and all(
                isinstance(v, list) and all(isinstance(t, str) for t in v) for v in pts.values())
            if not ok:
                found.append((cfg_path, f"platform_toolsets must declare exactly {', '.join(PLATFORMS)} as lists"))
            known = cfg.get("known_builtin_toolsets")
            if not isinstance(known, dict) or set(known) != set(PLATFORMS):
                found.append((cfg_path, f"known_builtin_toolsets must declare exactly {', '.join(PLATFORMS)}"))
        if "mcp_servers" in cfg:
            found.append((cfg_path, "mcp_servers declared: no bot uses MCP (plan 4 ruling 9)"))
        allow = _allowlist(pdir / "skills.allow.yaml", found)
        if allow is not None:
            if "disabled" in (cfg.get("skills") or {}):
                found.append((cfg_path, "skills.disabled is generated from skills.allow.yaml; remove it"))
            if pts is None:
                found.append((cfg_path, "a curated profile must declare platform_toolsets"))

        jobs_path = pdir / "cron" / "jobs.json"
        try:
            jobs = json.loads(jobs_path.read_text(encoding="utf-8")).get("jobs", []) if jobs_path.is_file() else []
        except Exception:
            jobs = []
        owned = [str(e).strip().strip("/") for e in ((_yaml(pdir / "distribution.yaml") or {}).get("distribution_owned") or [])]
        script_roots = [o[len("scripts/"):] for o in owned if o.startswith("scripts/")]
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            where = f"job {job.get('id')}"
            parts = str(job.get("deliver") or "").split(":")
            if len(parts) == 3 and parts[0] == "telegram" and routed.get((parts[1], parts[2])) != name:
                found.append((jobs_path, f"{where}: delivers to thread {parts[2]}, but thread {parts[2]} "
                                         f"is not routed to {name}"))
            script = job.get("script")
            if isinstance(script, str) and script:
                if not (pdir / "scripts" / script).is_file():
                    found.append((jobs_path, f"{where}: script {script} not found under hermes/profiles/{name}/scripts/"))
                if not any(script.startswith(root_ + "/") for root_ in script_roots):
                    found.append((jobs_path, f"{where}: script {script} is not covered by a scripts/ entry "
                                             "in distribution_owned"))
    return found


def skill_inventory(skills_dir: Path) -> dict[str, list[str]]:
    """Skill frontmatter name -> metadata.hermes.requires_toolsets."""
    out: dict[str, list[str]] = {}
    if not skills_dir.is_dir():
        return out
    for md in sorted(skills_dir.rglob("SKILL.md")):
        if any(part in _DATA_DIRS for part in md.relative_to(skills_dir).parts[:-1]):
            continue
        text = md.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
        front = {}
        if text.startswith("---"):
            end = text.find("\n---", 3)
            try:
                front = yaml.safe_load(text[3:end] if end != -1 else "") or {}
            except yaml.YAMLError:
                front = {}
        if not isinstance(front, dict):
            front = {}
        meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
        hermes_meta = meta.get("hermes") if isinstance(meta.get("hermes"), dict) else {}
        out[str(front.get("name") or md.parent.name)] = [str(t) for t in hermes_meta.get("requires_toolsets") or []]
    return out


def check_image(hermes: Path, bundled_dir: Path = Path("/opt/hermes/skills"),
                optional_dir: Path = Path("/opt/hermes/optional-skills"),
                toolsets: set[str] | None = None) -> Found:
    found: Found = []
    if toolsets is None:
        from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS  # only inside the image
        toolsets = {key for key, _, _ in CONFIGURABLE_TOOLSETS}
    bundled, optional = skill_inventory(bundled_dir), skill_inventory(optional_dir)
    if not bundled:
        return [(bundled_dir, "no bundled skills found; run inside the Hermes image")]
    for name, pdir in _profiles(hermes).items():
        cfg_path = pdir / "config.yaml"
        cfg = _yaml(cfg_path) or {}
        pts = cfg.get("platform_toolsets") if isinstance(cfg.get("platform_toolsets"), dict) else {}
        known = cfg.get("known_builtin_toolsets") if isinstance(cfg.get("known_builtin_toolsets"), dict) else {}
        for platform, names in pts.items():
            unknown = sorted(set(names or []) - toolsets)
            if unknown:
                found.append((cfg_path, f"platform_toolsets.{platform} has unknown toolset(s): {', '.join(unknown)}"))
        for platform in pts:
            missing = sorted(toolsets - set(known.get(platform) or []))
            extra = sorted(set(known.get(platform) or []) - toolsets)
            if missing or extra:
                found.append((cfg_path, f"known_builtin_toolsets.{platform} must list every toolset of this image "
                                        f"(missing: {', '.join(missing) or '-'}; unknown: {', '.join(extra) or '-'})"))
        allow_path = pdir / "skills.allow.yaml"
        allow = _allowlist(allow_path, [])
        if allow is None:
            continue
        telegram = set(pts.get("telegram") or [])
        for kind, names, inventory in (("bundled", allow[0], bundled), ("optional", allow[1], optional)):
            for skill in names:
                if skill not in inventory:
                    found.append((allow_path, f"{kind} skill '{skill}' does not exist in the image"))
                    continue
                need = sorted(set(inventory[skill]) - telegram)
                if need:
                    found.append((allow_path, f"skill '{skill}' requires {', '.join(need)}, "
                                              "missing from platform_toolsets.telegram"))
    return found
```

- [ ] **Step 5: Run the tests**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest -q hermes/ci 2>&1 | tail -3
```

Expected: `0 failed` (plan 3's tests plus 22 here).

- [ ] **Step 6: Commit (the PR is opened in Task 6)**

```bash
git add hermes/ci/roster.py hermes/ci/test_roster.py
git commit -m "ci: add roster checks for curated bot profiles"
```

---

### Task 5: Pre-push guard for `infra/hermes-agent/` (TDD, gitops-cluster + plder root approvals)

**Files:**
- Create: `infra/hermes-agent/githooks/pre_push.py`, `infra/hermes-agent/githooks/gitconfig`, `infra/hermes-agent/githooks/test_pre_push.py`
- Modify: `infra/hermes-agent/kustomization.yaml` (generator), `infra/hermes-agent/deployment.yaml` (two mounts, two volumes)
- Modify (plder): `hermes/root/config.yaml` (`approvals`)

**Interfaces:**
- Produces: `/etc/gitconfig` (`core.hooksPath = /etc/hermes-githooks`) and `/etc/hermes-githooks/pre-push` in the main container. The hook exits 1 with a line starting `ESCALATE:` when a push updates `refs/heads/main` or `refs/heads/master` of a remote whose URL contains `gitops-cluster` with any change under `infra/hermes-agent/`; else 0.
- Produces: root `approvals` with explicit `cron_mode: deny`, `single_query_mode: deny` and four extra `deny` globs, which every bot copies (enforced by Task 4 rule 3).

- [ ] **Step 1: Branch and write the failing tests**

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only && git checkout -b hermes-push-guard
mkdir -p infra/hermes-agent/githooks
```

```python
# infra/hermes-agent/githooks/test_pre_push.py
"""Real-git tests: bare remotes and clones under tmp_path, hook wired via a repo-local core.hooksPath."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import pre_push  # noqa: E402

HOOK = Path(__file__).with_name("pre_push.py")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


@pytest.fixture
def make_repo(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    wrapper = hooks / "pre-push"
    wrapper.write_bytes(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{HOOK.as_posix()}" "$@"\n'.encode())
    wrapper.chmod(0o755)

    def make(remote_name="gitops-cluster.git", seed=True):
        remote = tmp_path / remote_name
        git(tmp_path, "init", "-q", "--bare", str(remote))
        work = tmp_path / f"work-{remote_name}"
        git(tmp_path, "clone", "-q", str(remote), str(work))
        git(work, "symbolic-ref", "HEAD", "refs/heads/main")
        for key, value in (("user.name", "t"), ("user.email", "t@example.com"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", hooks.as_posix())):
            git(work, "config", key, value)
        if seed:
            commit(work, "README.md")
            git(work, "push", "-q", "origin", "main")
        return work
    return make


def commit(work, path):
    f = work / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("change\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", f"touch {path}")


def test_push_to_main_touching_hermes_agent_is_refused(make_repo):
    work = make_repo()
    commit(work, "infra/hermes-agent/deployment.yaml")
    res = git(work, "push", "origin", "main", check=False)
    assert res.returncode != 0
    assert "ESCALATE" in res.stderr and "infra/hermes-agent/deployment.yaml" in res.stderr
    remote_main = git(work, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
    assert remote_main != git(work, "rev-parse", "HEAD").stdout.strip()


def test_push_to_main_elsewhere_is_allowed(make_repo):
    work = make_repo()
    commit(work, "infra/loomie/deployment.yaml")
    assert git(work, "push", "origin", "main", check=False).returncode == 0


def test_push_to_a_hermes_branch_is_allowed(make_repo):
    work = make_repo()
    commit(work, "infra/hermes-agent/deployment.yaml")
    assert git(work, "push", "origin", "main:refs/heads/hermes/raise-limit", check=False).returncode == 0


def test_other_repositories_are_not_guarded(make_repo):
    work = make_repo("nixos-config.git")
    commit(work, "infra/hermes-agent/x.yaml")
    assert git(work, "push", "origin", "main", check=False).returncode == 0


def test_first_push_of_main_is_inspected_too(make_repo):
    work = make_repo(seed=False)
    commit(work, "infra/hermes-agent/deployment.yaml")
    res = git(work, "push", "origin", "main", check=False)
    assert res.returncode != 0 and "ESCALATE" in res.stderr


def test_branch_deletion_is_ignored():
    zero = "0" * 40
    stdin = io.StringIO(f"(delete) {zero} refs/heads/main {'a' * 40}\n")
    assert pre_push.main(["pre-push", "origin", "git@github.com:Forgenn/gitops-cluster.git"], stdin) == 0
```

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/githooks && python -m pytest -q 2>&1 | tail -3
```

Expected: `ModuleNotFoundError: No module named 'pre_push'`.

- [ ] **Step 2: Implement the hook and the git config**

`infra/hermes-agent/githooks/pre_push.py`:

```python
#!/opt/hermes/.venv/bin/python3
"""git pre-push guard for the Hermes agent pod (installed via /etc/gitconfig core.hooksPath).

A push that updates main/master of Forgenn/gitops-cluster with changes under
infra/hermes-agent/ is refused. That directory is the agent's own deployment: its
credentials, RBAC, memory and config sync. The agent must escalate instead: push a
branch hermes/<topic> and send the operator the compare link.

Why a hook: Hermes approvals only review commands its detector flags, and a plain
`git push` is not flagged (tools/approval.py DANGEROUS_PATTERNS, v2026.8.19), so
approvals.smart_policy can never see it. This is a guardrail, not a security
boundary; approvals.deny blocks the obvious bypasses (--no-verify, hooksPath,
GIT_CONFIG_NOSYSTEM, GIT_CONFIG_SYSTEM). Design: gitops-cluster
docs/plans/2026-09-14-hermes-bot-roster.md, Task 5.
"""
from __future__ import annotations

import subprocess
import sys

PROTECTED_REMOTE = "gitops-cluster"
PROTECTED_PATH = "infra/hermes-agent/"
PROTECTED_REFS = frozenset({"refs/heads/main", "refs/heads/master"})
ZERO = "0" * 40
COMPARE = "https://github.com/Forgenn/gitops-cluster/compare/main...hermes/<topic>"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def changed_paths(local_sha: str, remote_sha: str) -> list[str]:
    if remote_sha != ZERO and _git("cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        out = _git("diff", "--name-only", remote_sha, local_sha)
    else:  # new branch, or a remote tip we have never fetched
        out = _git("log", "--format=", "--name-only", local_sha, "--not", "--remotes")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return sorted({line.strip() for line in out.stdout.splitlines() if line.strip()})


def blocked_updates(remote_url: str, updates: list[list[str]]) -> list[tuple[str, list[str]]]:
    if PROTECTED_REMOTE not in remote_url:
        return []
    blocked = []
    for _local_ref, local_sha, remote_ref, remote_sha in updates:
        if remote_ref not in PROTECTED_REFS or local_sha == ZERO:
            continue
        hits = [p for p in changed_paths(local_sha, remote_sha) if p.startswith(PROTECTED_PATH)]
        if hits:
            blocked.append((remote_ref, hits))
    return blocked


def main(argv: list[str], stdin) -> int:
    remote_url = argv[2] if len(argv) > 2 else ""
    updates = [line.split() for line in stdin.read().splitlines() if len(line.split()) == 4]
    try:
        blocked = blocked_updates(remote_url, updates)
    except Exception as exc:
        print(f"ESCALATE: the pre-push guard could not inspect this push ({exc}); refusing. "
              "Ask the operator.", file=sys.stderr)
        return 1
    for ref, hits in blocked:
        print(f"ESCALATE: this push to {ref} changes {len(hits)} file(s) under {PROTECTED_PATH} "
              f"({', '.join(hits[:3])}). That is the agent's own deployment and needs the operator. "
              f"Push a branch named hermes/<topic> instead and send the operator {COMPARE}.",
              file=sys.stderr)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
```

`infra/hermes-agent/githooks/gitconfig`:

```ini
# System git config for the hermes-agent container (mounted at /etc/gitconfig).
# Every repository the agent touches runs /etc/hermes-githooks/pre-push.
[core]
	hooksPath = /etc/hermes-githooks
```

- [ ] **Step 3: Run the tests**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/githooks && python -m pytest -q 2>&1 | tail -2
```

Expected: `6 passed`.

- [ ] **Step 4: Ship the hook**

In `infra/hermes-agent/kustomization.yaml`, under `configMapGenerator:` add:

```yaml
  # Git pre-push guard for the agent (githooks/pre_push.py). Generated, so an edit rolls the pod.
  - name: hermes-githooks
    files:
      - pre-push=githooks/pre_push.py
      - gitconfig=githooks/gitconfig
```

In `infra/hermes-agent/deployment.yaml`, in the `hermes-agent` container's `volumeMounts` (after the `profile-secrets` mount):

```yaml
            # Pre-push guard: pushes to gitops-cluster main touching infra/hermes-agent/
            # are refused and must go through a hermes/<topic> branch the operator merges.
            # Hermes approvals cannot express this (a plain git push is never flagged).
            - name: githooks
              mountPath: /etc/hermes-githooks
              readOnly: true
            - name: githooks-gitconfig
              mountPath: /etc/gitconfig
              subPath: gitconfig
              readOnly: true
```

and in `volumes`:

```yaml
        - name: githooks
          configMap:
            name: hermes-githooks
            defaultMode: 0555
            items:
              - key: pre-push
                path: pre-push
        - name: githooks-gitconfig
          configMap:
            name: hermes-githooks
            items:
              - key: gitconfig
                path: gitconfig
```

```bash
cd /c/Users/Pol/projects/gitops-check
kubectl kustomize infra/hermes-agent | python -c "
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
cm = [d for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'].startswith('hermes-githooks')]
spec = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
c = next(c for c in spec['containers'] if c['name'] == 'hermes-agent')
names = {v['name']: v for v in spec['volumes']}
print('cm:', [x['metadata']['name'] for x in cm], sorted(cm[0]['data']))
print('mounts:', [(m['mountPath'], names[m['name']]['configMap']['name']) for m in c['volumeMounts'] if m['name'].startswith('githooks')])"
git add infra/hermes-agent/githooks infra/hermes-agent/kustomization.yaml infra/hermes-agent/deployment.yaml
git commit -m "hermes: refuse agent pushes to main that touch infra/hermes-agent"
```

Expected: one `hermes-githooks-<hash>` ConfigMap with keys `['gitconfig', 'pre-push']`; mounts `/etc/hermes-githooks` and `/etc/gitconfig`, both referencing the hashed name.

- [ ] **Step 5: Add the bypass denials to the root approvals (plder)**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git checkout -b hermes-approvals-guard
python - <<'PY'
p = "hermes/root/config.yaml"
raw = open(p, "rb").read()
eol = b"\r\n" if b"\r\n" in raw else b"\n"
anchor = b'    - "*k3s kubectl* --all --force*"' + eol
assert raw.count(anchor) == 1, "deny list anchor not found"
extra = eol.join([
    b"    # Bypasses of the pod's git pre-push guard (gitops-cluster infra/hermes-agent/githooks).",
    b"    # Every served profile copies this list; plder CI (hermes/ci/roster.py) enforces it.",
    b'    - "*--no-verify*"',
    b'    - "*hookspath*"',
    b'    - "*git_config_nosystem*"',
    b'    - "*git_config_system*"',
]) + eol
raw = raw.replace(anchor, anchor + extra)
mode = b"  mode: smart" + eol
assert raw.count(mode) == 1
raw = raw.replace(mode, mode + b"  cron_mode: deny" + eol + b"  single_query_mode: deny" + eol)
open(p, "wb").write(raw)
PY
python -c "import yaml; a=yaml.safe_load(open('hermes/root/config.yaml',encoding='utf-8'))['approvals']; print(a['cron_mode'], a['single_query_mode'], a['deny'])"
python hermes/ci/validate.py hermes
git commit -am "hermes: deny bypasses of the agent's git pre-push guard"
```

Expected: `deny deny [... 8 entries ...]`; `OK: hermes is valid`.

- [ ] **Step 6 (controller): Deploy both**

Gates: volsync within 36 h; not 08:45–09:15 UTC. gitops first (the hook), then plder (the denials) — two rolls.

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only
git merge --no-ff hermes-push-guard -m "hermes: git pre-push guard for infra/hermes-agent" && git push origin main
GITOPS_SHA=$(git rev-parse HEAD); export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
```

```bash
cd /c/Users/Pol/projects/plder && git push -u origin hermes-approvals-guard
gh pr create --repo Forgenn/plder --base master --head hermes-approvals-guard \
  --title "hermes: deny bypasses of the agent's git pre-push guard" --body "Plan: gitops-cluster docs/plans/2026-09-14-hermes-bot-roster.md Task 5"
gh pr checks hermes-approvals-guard --repo Forgenn/plder --watch
gh pr merge hermes-approvals-guard --repo Forgenn/plder --merge --delete-branch
git checkout master && git pull --ff-only && SHA=$(git rev-parse HEAD)
/c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/wait-deploy.sh "$SHA"
```

Expected: checks pass; the profile-sync log shows `copied config.yaml …` and `applied ref <SHA:0:12> (result: ok)`.

- [ ] **Step 7 (controller): Verify the guard on the real container**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
. ./lib.sh
pexec <<'SH'
/command/s6-setuidgid hermes sh -c '
G=/tmp/guard-probe; rm -rf $G; mkdir -p $G; cd $G
echo "system hooksPath: $(git config --system --get core.hooksPath)"
echo "agent clone url has gitops-cluster: $(git -C /opt/data/home/gitops-cluster remote get-url origin | grep -c gitops-cluster)"
git init -q --bare gitops-cluster.git && git clone -q gitops-cluster.git work 2>/dev/null && cd work
git symbolic-ref HEAD refs/heads/main
git -c user.name=p -c user.email=p@x commit -q --allow-empty -m init && git push -q origin main 2>&1 | tail -1
mkdir -p infra/hermes-agent && echo x > infra/hermes-agent/probe.yaml && git add . && git -c user.name=p -c user.email=p@x commit -q -m probe
if git push origin main 2>/tmp/guard-probe/main.err; then echo "main push: ACCEPTED (guard FAILED)"; else echo "main push: refused"; fi
grep -E "ESCALATE" /tmp/guard-probe/main.err | cut -c1-160
if git push -q origin main:refs/heads/hermes/probe 2>/dev/null; then echo "branch push: accepted"; else echo "branch push: refused (unexpected)"; fi
git ls-remote origin | awk "{print \$2}"
' </dev/null
SH
```

Expected: `system hooksPath: /etc/hermes-githooks`; `… has gitops-cluster: 1`; `main push: refused` followed by `ESCALATE: this push to refs/heads/main changes 1 file(s) under infra/hermes-agent/ (infra/hermes-agent/probe.yaml)…`; `branch push: accepted`; `git ls-remote` lists `refs/heads/hermes/probe` and `refs/heads/main`.

Then the deny rule on the gateway path, in the root store:

```bash
ID=$(./agent-probe.sh default "deny probe" "Run exactly this terminal command and nothing else: git -C /tmp/guard-probe/work push --no-verify --dry-run origin main. If the tool refused to run it, reply DENY_PROBE_BLOCKED. If it ran, reply DENY_PROBE_RAN.")
```

At least 2 minutes after the fire time:

```bash
./probe-result.sh default "$ID" DENY_PROBE_BLOCKED; ./probe-cleanup.sh default "$ID"
. ./lib.sh; pexec <<'SH'
rm -rf /tmp/guard-probe
SH
```

Expected: `last_status=ok`, `marker_in_response: True`. Then the agent-probe kit in root and monitor: both pass.

**Rollback:** `git revert -m 1` the gitops merge (hook) and/or the plder merge (denials); both are independent.

---
### Task 6: Shared blocks, roster CI, and the monitor migrated onto them (plder)

One PR: auxiliary models in root and monitor, the monitor's allowlist/toolsets/api_server fix, and the roster checks wired into CI. After it, every rule a new bot must satisfy is enforced, and the monitor proves the mechanisms on the real path before any new bot relies on them.

**Files (plder, branch `hermes-roster-checks` from Task 4):**
- Modify: `hermes/root/config.yaml` (add `auxiliary`)
- Replace: `hermes/profiles/monitor/config.yaml`
- Create: `hermes/profiles/monitor/skills.allow.yaml`
- Modify: `hermes/ci/validate.py`, `hermes/ci/test_validate.py`, `hermes/ci/image_checks.py`

**Shared blocks** (copied verbatim into every bot config in Tasks 8–11):

```yaml
# Auxiliary side-models, pinned to OpenRouter so no task falls through the auto
# chain to the Nous portal (no credentials there; it logged payment errors every
# boot). Identical in root and every profile: plder CI (hermes/ci/roster.py) checks.
auxiliary:
  vision:
    provider: openrouter
    model: google/gemini-3.8-flash
  approval:
    provider: openrouter
    model: google/gemini-3.8-flash
  web_extract:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  compression:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  title_generation:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  skills_hub:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  mcp:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  memory_query_rewrite:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
```

```yaml
# Every configurable toolset of image v2026.8.19, per platform. Listing them marks
# them all "offered", so a toolset the image calls recently shipped is not
# auto-enabled on top of platform_toolsets (tools_config._enable_recently_shipped_toolsets).
# CI fails when an image bump adds one: review it, then add it here.
known_builtin_toolsets:
  cli: [web, browser, terminal, file, code_execution, vision, video, image_gen, video_gen, bfl, x_search, tts, stt, skills, todo, memory, context_engine, session_search, clarify, delegation, cronjob, homeassistant, spotify, discord, discord_admin, yuanbao, computer_use]
  telegram: [web, browser, terminal, file, code_execution, vision, video, image_gen, video_gen, bfl, x_search, tts, stt, skills, todo, memory, context_engine, session_search, clarify, delegation, cronjob, homeassistant, spotify, discord, discord_admin, yuanbao, computer_use]
  cron: [web, browser, terminal, file, code_execution, vision, video, image_gen, video_gen, bfl, x_search, tts, stt, skills, todo, memory, context_engine, session_search, clarify, delegation, cronjob, homeassistant, spotify, discord, discord_admin, yuanbao, computer_use]
```

```yaml
approvals:
  mode: smart
  cron_mode: deny
  single_query_mode: deny
  deny:
    - "kubectl delete namespace*"
    - "k3s kubectl delete namespace*"
    - "*kubectl* --all --force*"
    - "*k3s kubectl* --all --force*"
    - "*--no-verify*"
    - "*hookspath*"
    - "*git_config_nosystem*"
    - "*git_config_system*"
```

```yaml
# A secondary profile must not bind the shared HTTP listener. Without this, a
# usable API_SERVER_KEY in the profile's scope (the monitor's .env is a copy of
# root's) enables api_server and the gateway skips the profile's adapters with
# "port-binding config error". Serving, routing and cron are unaffected either way.
platforms:
  api_server:
    enabled: false
plugins:
  enabled: []
desktop:
  repo_scan_enabled: false
```

```yaml
# Credentials for agent turns under multiplex (plan 2): the scope is authoritative,
# so the helper reads the files mounted at /etc/hermes-profile-secrets with builtins only.
secrets:
  command:
    enabled: true
    override_existing: true
    command: 'for f in /etc/hermes-profile-secrets/*; do IFS= read -r v < "$f" || [ -n "$v" ]; printf "%s=%s\n" "${f##*/}" "$v"; done'
# Preflight builds the delivery check from this scope, which holds no Telegram token.
cron:
  preflight: false
_config_version: 38
```

- [ ] **Step 1: Capture the monitor's current surface (controller)**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
./profile-state.sh monitor | tee /tmp/monitor-before.json
```

Record `skills_enabled`. Expected to include `hermes-agent` and `kubernetes-monitoring-troubleshooting`; any other name listed here (e.g. a macOS-only skill this call does not platform-filter) is compared again in Step 7 and must be explained before the rollout continues.

- [ ] **Step 2: Add `auxiliary` to the root config**

```bash
cd /c/Users/Pol/projects/plder && git checkout hermes-roster-checks && git rebase master
python - <<'PY'
p = "hermes/root/config.yaml"
raw = open(p, "rb").read()
eol = b"\r\n" if b"\r\n" in raw else b"\n"
assert b"\nauxiliary:" not in raw
block = """
# Auxiliary side-models, pinned to OpenRouter so no task falls through the auto
# chain to the Nous portal (no credentials there; it logged payment errors every
# boot). Identical in root and every profile: plder CI (hermes/ci/roster.py) checks.
auxiliary:
  vision:
    provider: openrouter
    model: google/gemini-3.8-flash
  approval:
    provider: openrouter
    model: google/gemini-3.8-flash
  web_extract:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  compression:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  title_generation:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  skills_hub:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  mcp:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
  memory_query_rewrite:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
""".encode().replace(b"\n", eol)
if not raw.endswith(eol):
    raw += eol
open(p, "wb").write(raw + block)
PY
```

- [ ] **Step 3: Replace the monitor's config**

Write `hermes/profiles/monitor/config.yaml` with exactly this content (the 76-name denylist and the commented vendor templates go; the model, approvals mode, plugins, desktop, secrets and preflight carry over):

```yaml
# hermes/profiles/monitor/config.yaml -- Cluster Monitor (cheap tier, read-only)
model:
  default: deepseek/deepseek-v4-flash-0731
  provider: openrouter
agent:
  bot_mode_protocol: true

# Toolsets per platform. An explicit list is an allowlist
# (hermes_cli/tools_config.py _get_platform_tools). cli = `hermes -p monitor chat`.
platform_toolsets:
  telegram: [terminal, file, web, skills, memory, todo, session_search, clarify]
  cli: [terminal, file, web, skills, memory, todo, session_search]
  cron: [terminal, file, web, skills, memory, todo]
```

followed by the `known_builtin_toolsets`, `approvals`, `auxiliary`, `platforms`/`plugins`/`desktop` and `secrets`/`cron`/`_config_version` shared blocks above, in that order, verbatim.

`hermes/profiles/monitor/skills.allow.yaml`:

```yaml
# Upstream skills this bot uses. Everything else bundled is disabled at every sync
# (skills.disabled is generated by profile-sync); skills that are not bundled stay on.
# Names are skill frontmatter names; plder CI checks them against the pinned image.
bundled:
  - hermes-agent
optional: []
```

```bash
cd /c/Users/Pol/projects/plder
python - <<'PY'
import yaml
root = yaml.safe_load(open("hermes/root/config.yaml", encoding="utf-8"))
mon = yaml.safe_load(open("hermes/profiles/monitor/config.yaml", encoding="utf-8"))
assert mon["auxiliary"] == root["auxiliary"]
assert mon["secrets"] == root["secrets"]
assert set(root["approvals"]["deny"]) <= set(mon["approvals"]["deny"])
assert mon["platforms"]["api_server"]["enabled"] is False and mon["cron"]["preflight"] is False
assert "skills" not in mon and mon["_config_version"] == 38
assert all(len(v) == 27 for v in mon["known_builtin_toolsets"].values())
print("OK monitor config", sorted(mon))
PY
```

- [ ] **Step 4: Wire the roster checks into CI**

(a) `hermes/ci/validate.py`: add `import roster` after `import yaml`, and in `validate()` directly before `return errors` add:

```python
    # Roster rules for curated bots (docs/plans/2026-09-14-hermes-bot-roster.md).
    for path, msg in roster.check_roster(hermes):
        err(path, msg)
```

(b) `hermes/ci/test_validate.py`: plan 3's fixtures predate rule 1. In the `tree` fixture's monitor `config.yaml` dict and in `_add_profile`'s config dict, add `"platforms": {"api_server": {"enabled": False}}`.

(c) `hermes/ci/image_checks.py`: after the `for manifest_path in …` loop and before the final `print`, add:

```python
    import roster  # hermes/ci is sys.path[0] when this file runs as a script
    for path, msg in roster.check_image(src):
        fail(path, msg)
```

```bash
cd /c/Users/Pol/projects/plder
python -m pytest -q hermes/ci 2>&1 | tail -2
python hermes/ci/validate.py hermes; echo "exit=$?"
git add hermes/root/config.yaml hermes/profiles/monitor hermes/ci
git commit -m "hermes: pin auxiliary models and curate the monitor from an allowlist"
```

Expected: `0 failed`; `OK: hermes is valid`, `exit=0`.

- [ ] **Step 5 (controller): PR, CI, rehearsal**

```bash
cd /c/Users/Pol/projects/plder && git push -u origin hermes-roster-checks
gh pr create --repo Forgenn/plder --base master --head hermes-roster-checks \
  --title "hermes: roster checks, pinned auxiliary models, curated monitor" \
  --body "Plan: gitops-cluster docs/plans/2026-09-14-hermes-bot-roster.md Tasks 4 and 6"
gh pr checks hermes-roster-checks --repo Forgenn/plder --watch
BR=$(git rev-parse HEAD)
/c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/rehearse-sync.sh "$BR" 2>&1 | tail -70
```

Expected: `validate` passes (the image step shows `image checks: 0 failure(s)`). In the rehearsal: `installed profile monitor`, `skills curated for monitor: 1 allowed, 81 bundled disabled`, `applied ref <BR> (result: ok)`; monitor JSON: `toolsets.telegram` contains exactly the declared eight plus any recovered non-configurable toolsets (no `browser`, `delegation`, `cronjob`, `code_execution`, `vision`, `image_gen`, `tts`, `bfl`), `skills_enabled` = `["hermes-agent"]` (the scratch home has no hand-made skill), `api_server_enabled: false`, `aux_vision` shows `provider: openrouter`, `model: google/gemini-3.8-flash` **and** a `timeout` (deep merge kept the defaults), `mcp_servers: []`; image checks `0 failure(s)`; `NotFound`.

**If the rehearsal shows `api_server_enabled: true` for monitor or a missing `timeout`, STOP** and re-read `gateway/config.py` ~1507/~2212 and `hermes_cli/config.py::_deep_merge` before changing anything.

- [ ] **Step 6 (controller): Merge and deploy**

Gates: volsync within 36 h; not 08:45–09:15 UTC.

```bash
DEPLOY_AT=$(date -u +"%Y-%m-%d %H:%M")
gh pr merge hermes-roster-checks --repo Forgenn/plder --merge --delete-branch
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && SHA=$(git rev-parse HEAD)
/c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/wait-deploy.sh "$SHA"
echo "deployed after $DEPLOY_AT"
```

Expected log: `copied config.yaml …`, `installed profile monitor`, `skills curated for monitor: 1 allowed, 81 bundled disabled`, `applied ref <SHA:0:12> (result: ok)`.

- [ ] **Step 7 (controller): Verify on the real path**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
DEPLOY_AT="REPLACE_WITH_STEP_6_VALUE"   # e.g. "2026-09-15 14:02"
./profile-state.sh default monitor
. ./lib.sh; pexec "$DEPLOY_AT" <<'SH'
since=$1
echo "served: $(/opt/hermes/.venv/bin/python -c 'import json; print(json.load(open("/opt/data/gateway_state.json"))["served_profiles"])' </dev/null)"
echo "skipping-secondary lines since deploy: $(awk -v s="$since" 'substr($0,1,16) >= s' /opt/data/logs/gateway.log | grep -c "Skipping secondary profile")"
echo "nous warnings since deploy: $(cat /opt/data/logs/agent.log /opt/data/logs/errors.log 2>/dev/null | awk -v s="$since" 'substr($0,1,16) >= s' | grep -c "Auxiliary Nous client unavailable")"
SH
```

Expected: monitor `skills_enabled` = `["hermes-agent", "kubernetes-monitoring-troubleshooting"]` (the hand-made skill survives), `skills_disabled_count: 81`, `api_server_enabled: false`; root unchanged except `aux_vision`; `served: ['default', 'monitor']`; `skipping-secondary lines since deploy: 0`; `nous warnings since deploy: 0`.

Probes — agent turn in both stores, vision through the pinned auxiliary model in root, and delivery of an agent job to the monitor topic:

```bash
R=$(./agent-probe.sh default "roster probe" "Reply with exactly AGENT_PROBE_OK and nothing else.")
M=$(./agent-probe.sh monitor "roster probe" "Reply with exactly AGENT_PROBE_OK and nothing else.")
V=$(./agent-probe.sh default "vision probe" "Use the vision_analyze tool on https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png and reply VISION_OK followed by three words describing the image. If the tool is unavailable reply VISION_MISSING.")
T=$(./agent-probe.sh monitor "topic delivery probe" "Reply with exactly: monitor topic delivery probe, safe to ignore." "telegram:7850573137:5332")
echo "$R $M $V $T"
```

At least 3 minutes after the fire time:

```bash
./probe-result.sh default "$R" AGENT_PROBE_OK; ./probe-result.sh monitor "$M" AGENT_PROBE_OK
./probe-result.sh default "$V" VISION_OK;       ./probe-result.sh monitor "$T"
./probe-cleanup.sh default "$R" "$V"; ./probe-cleanup.sh monitor "$M" "$T"
. ./lib.sh; pexec "$DEPLOY_AT" <<'SH'
echo "nous warnings since deploy: $(cat /opt/data/logs/agent.log /opt/data/logs/errors.log 2>/dev/null | awk -v s="$1" 'substr($0,1,16) >= s' | grep -c "Auxiliary Nous client unavailable")"
SH
./mem.sh 1h
```

Expected: the three agent probes `last_status=ok last_error=None marker_in_response: True`; the topic probe `last_status=ok delivery_error=None` and the message visible in the monitor topic; nous warnings still `0`; memory `(ok)`.

The next morning, confirm the 09:00 report still arrived in the monitor topic (`/opt/data/profiles/monitor/cron/output/6270d3f018f2/` has a new file, `last_status ok`): it now runs with the curated toolsets.

**Rollback:** `git revert -m 1 <merge sha>` in plder and push; CI pins the revert.

---

### Task 7: Monitor volsync staleness watchdog (TDD, plder)

**Files:**
- Create: `hermes/profiles/monitor/scripts/plder/volsync_staleness.py`
- Create: `hermes/ci/test_volsync_staleness.py`
- Modify: `hermes/profiles/monitor/distribution.yaml` (own `scripts/plder/`), `hermes/profiles/monitor/cron/jobs.json` (add job `9ef0d35042ed`)

**Interfaces:** `stale_sources(items: list[dict], now: datetime) -> list[str]`, `main() -> int`, module global `MAX_AGE_HOURS = 36` read at call time. Stdout is the Telegram message; empty stdout = no delivery.

- [ ] **Step 1: Branch and write the failing tests**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git checkout -b hermes-volsync-watchdog
```

```python
# hermes/ci/test_volsync_staleness.py
import importlib.util
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "profiles" / "monitor" / "scripts" / "plder" / "volsync_staleness.py"
_spec = importlib.util.spec_from_file_location("volsync_staleness", SCRIPT)
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def rs(ns, name, last=None, paused=False):
    item = {"metadata": {"namespace": ns, "name": name}, "spec": {}, "status": {}}
    if last:
        item["status"]["lastSyncTime"] = last
    if paused:
        item["spec"]["paused"] = True
    return item


def test_fresh_sources_print_nothing():
    assert vs.stale_sources([rs("hermes", "hermes-data", "2026-09-14T02:06:15Z")], NOW) == []


def test_a_source_older_than_the_threshold_is_reported():
    assert vs.stale_sources([rs("hermes", "hermes-data", "2026-09-12T02:00:00Z")], NOW) == [
        "🔴 volsync hermes/hermes-data: last successful sync 2026-09-12T02:00:00Z (58h ago)"]


def test_a_source_that_never_synced_is_reported():
    assert vs.stale_sources([rs("zot", "zot-pvc")], NOW) == ["🔴 volsync zot/zot-pvc: no successful sync recorded"]


def test_a_paused_source_is_ignored():
    assert vs.stale_sources([rs("zot", "zot-pvc", paused=True)], NOW) == []


def test_the_threshold_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(vs, "MAX_AGE_HOURS", 0)
    assert len(vs.stale_sources([rs("hermes", "hermes-data", "2026-09-14T11:00:00Z")], NOW)) == 1


def test_main_reports_a_listing_failure(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise FileNotFoundError("kubectl")
    monkeypatch.setattr(vs.subprocess, "run", boom)
    assert vs.main() == 0
    assert "could not list ReplicationSources: FileNotFoundError" in capsys.readouterr().out


def test_main_reports_an_empty_listing(monkeypatch, capsys):
    monkeypatch.setattr(vs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout='{"items": []}'))
    assert vs.main() == 0
    assert "found no ReplicationSources" in capsys.readouterr().out


def test_main_is_silent_when_everything_is_fresh(monkeypatch, capsys):
    import json
    items = {"items": [rs("hermes", "hermes-data", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))]}
    monkeypatch.setattr(vs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps(items)))
    assert vs.main() == 0
    assert capsys.readouterr().out == ""
```

```bash
python -m pytest -q hermes/ci/test_volsync_staleness.py 2>&1 | tail -3
```

Expected: collection error, `FileNotFoundError` for the script path.

- [ ] **Step 2: Implement the script**

```python
#!/usr/bin/env python3
"""volsync staleness watchdog: a --no-agent cron job in the monitor profile.

Prints one line per volsync ReplicationSource whose last successful sync is older
than MAX_AGE_HOURS and prints nothing when all are fresh. The scheduler delivers
stdout verbatim and stays silent on empty output, so a healthy day costs no LLM
call and no message. A listing failure is itself reported: silence must mean
"checked and fine", never "could not check" (the hermes-data backup was dead for
days in September 2026 and nothing noticed).
Declared in plder hermes/profiles/monitor; tests in hermes/ci/test_volsync_staleness.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

MAX_AGE_HOURS = 36  # every source runs daily; 36h tolerates one slow run
KUBECTL = "/usr/local/bin/kubectl"


def stale_sources(items: list[dict], now: datetime) -> list[str]:
    limit = timedelta(hours=MAX_AGE_HOURS)
    lines = []
    for item in items:
        meta = item.get("metadata") or {}
        where = f"{meta.get('namespace', '?')}/{meta.get('name', '?')}"
        if (item.get("spec") or {}).get("paused"):
            continue
        last = (item.get("status") or {}).get("lastSyncTime")
        if not last:
            lines.append(f"🔴 volsync {where}: no successful sync recorded")
            continue
        age = now - datetime.fromisoformat(last.replace("Z", "+00:00"))
        if age > limit:
            lines.append(f"🔴 volsync {where}: last successful sync {last} ({int(age.total_seconds() // 3600)}h ago)")
    return sorted(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        proc = subprocess.run([KUBECTL, "get", "replicationsources.volsync.backube", "-A", "-o", "json"],
                              capture_output=True, text=True, timeout=60, check=True)
        items = json.loads(proc.stdout).get("items") or []
    except Exception as exc:
        print(f"🔴 volsync watchdog could not list ReplicationSources: {type(exc).__name__}: {str(exc)[:200]}")
        return 0
    if not items:
        print("🔴 volsync watchdog found no ReplicationSources (CRD, RBAC or namespace change?)")
        return 0
    for line in stale_sources(items, datetime.now(timezone.utc)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

```bash
python -m pytest -q hermes/ci 2>&1 | tail -2
```

Expected: `0 failed`.

- [ ] **Step 3: Declare it**

```bash
cd /c/Users/Pol/projects/plder
python - <<'PY'
import json
p = "hermes/profiles/monitor/distribution.yaml"
raw = open(p, "rb").read()
eol = b"\r\n" if b"\r\n" in raw else b"\n"
anchor = b"  - assets/" + eol
assert raw.count(anchor) == 1 and b"scripts/plder/" not in raw
open(p, "wb").write(raw.replace(anchor, anchor + b"  - scripts/plder/" + eol))

p = "hermes/profiles/monitor/cron/jobs.json"
doc = json.load(open(p, encoding="utf-8"))
assert all(j["id"] != "9ef0d35042ed" for j in doc["jobs"])
doc["jobs"].append({
    "id": "9ef0d35042ed",
    "name": "[bot:monitor] volsync staleness watchdog",
    "managed_by": "plder",
    "prompt": "",
    "skills": [], "skill": None, "model": None, "provider": None,
    "script": "plder/volsync_staleness.py",
    "no_agent": True,
    "monitor_script": None, "monitor_url": None, "context_from": None,
    "schedule": {"kind": "cron", "expr": "30 10 * * *", "display": "30 10 * * *"},
    "schedule_display": "30 10 * * *",
    "repeat": {"times": None, "completed": 0},
    "enabled": True,
    "deliver": "telegram:7850573137:5332",
    "workdir": None,
})
with open(p, "w", encoding="utf-8", newline="\n") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
python hermes/ci/validate.py hermes
git add hermes/profiles/monitor hermes/ci/test_volsync_staleness.py
git commit -m "hermes: add a no-agent volsync staleness watchdog to the monitor"
```

Expected: `OK: hermes is valid`.

- [ ] **Step 4 (controller): PR, rehearsal, merge, deploy**

```bash
git push -u origin hermes-volsync-watchdog
gh pr create --repo Forgenn/plder --base master --head hermes-volsync-watchdog \
  --title "hermes: monitor volsync staleness watchdog" --body "Plan: gitops-cluster docs/plans/2026-09-14-hermes-bot-roster.md Task 7"
gh pr checks hermes-volsync-watchdog --repo Forgenn/plder --watch
/c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/rehearse-sync.sh "$(git rev-parse HEAD)" 2>&1 | grep -E "installed profile|cron: .*monitor|applied ref|image checks|NotFound"
gh pr merge hermes-volsync-watchdog --repo Forgenn/plder --merge --delete-branch
git checkout master && git pull --ff-only && /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/wait-deploy.sh "$(git rev-parse HEAD)"
```

Expected: checks pass; rehearsal `cron: 2 job(s) in /tmp/fh/profiles/monitor/cron/jobs.json`, `result: ok`; deploy log `cron: 2 job(s) in /opt/data/profiles/monitor/cron/jobs.json`.

- [ ] **Step 5 (controller): Verify**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
. ./lib.sh
pexec <<'SH'
ls -l /opt/data/profiles/monitor/scripts/plder/
out=$(/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/data/profiles/monitor/scripts/plder/volsync_staleness.py </dev/null); echo "direct run: [${out}] exit=$?"
cat > /opt/data/profiles/monitor/scripts/plder-probe-volsync-zero.py <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("v", "/opt/data/profiles/monitor/scripts/plder/volsync_staleness.py")
v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
v.MAX_AGE_HOURS = 0
raise SystemExit(v.main())
PY
chown 10000:10000 /opt/data/profiles/monitor/scripts/plder-probe-volsync-zero.py
SH
ID=$(./noagent-probe.sh monitor "volsync zero-threshold probe" plder-probe-volsync-zero.py local)
```

Expected: the script listed; `direct run: [] exit=0` (all sources fresh; a non-empty value means a real stale backup — report it). The direct run proves the script, not the scheduler; the probe below proves the scheduler path with RBAC as the gateway sees it.

At least 3 minutes after the fire time:

```bash
./probe-result.sh monitor "$ID" "volsync hermes/hermes-data"
./probe-cleanup.sh monitor "$ID"
. ./lib.sh; pexec <<'SH'
rm -f /opt/data/profiles/monitor/scripts/plder-probe-volsync-zero.py
/opt/hermes/.venv/bin/python -c "import json; [print(j['id'], j['name'], j.get('next_run_at')) for j in json.load(open('/opt/data/profiles/monitor/cron/jobs.json'))['jobs']]" </dev/null
SH
```

Expected: `last_status=ok`, `marker_in_response: True` (every source is reported at a 0 h threshold); afterwards the monitor store lists `6270d3f018f2` and `9ef0d35042ed` (next run 10:30 UTC) only. The next day, `9ef0d35042ed` shows `last_status ok` and nothing was posted if backups are fresh.

---
### Bot rollout procedure (shared by Tasks 8–11)

Each bot task fills in the bot-specific files, then runs these steps with its own `BOT`, `TOPIC` and thread variable. A bot is done only when every **R** step passes; the next bot starts only then.

- **R1 (controller) — topic.** `cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops && ./create-topic.sh "$TOPIC"` → record the printed id in "Recorded at execution". Run once: a re-run creates a second topic (delete a stray one in Telegram).
- **R2 — route.** On the bot's plder branch: `python /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/add_route.py hermes/root/config.yaml "$BOT" 7850573137 "$THREAD"`, then `python hermes/ci/validate.py hermes` → `OK`.
- **R3 — local tests.** `python -m pytest -q hermes/ci` → `0 failed`.
- **R4 (controller) — PR and CI.** Push the branch, `gh pr create`, `gh pr checks <branch> --watch`. The image step must show `OK: installed <BOT>` and `image checks: 0 failure(s)`.
- **R5 (controller) — rehearsal.** `ops/rehearse-sync.sh "$(git rev-parse HEAD)"`. Expected: `installed profile <BOT>`; `skills curated for <BOT>: <n> allowed, <d> bundled disabled` with **no** `not installed` warning; `applied ref … (result: ok)`; the bot's JSON shows the declared model, the declared toolsets per platform (plus recovered non-configurable ones only), `skills_enabled` = the allowlist plus its custom skills, `api_server_enabled: false`, `aux_vision` pinned, `mcp_servers: []`; image checks `0 failure(s)`; `NotFound`.
- **R6 (controller) — merge and deploy.** Gates: volsync within 36 h; not 08:45–09:15 UTC. `gh pr merge <branch> --merge --delete-branch`, pull master, `ops/wait-deploy.sh "$(git rev-parse HEAD)"`. Expected log: `installed profile <BOT>`, the curated line, `applied ref <sha12> (result: ok)`.
- **R7 (controller) — served and curated.** `ops/profile-state.sh "$BOT"` matches R5 (live store: custom skills included). Then:
  ```bash
  . ./lib.sh; pexec <<'SH'
  /opt/hermes/.venv/bin/python -c 'import json; print(json.load(open("/opt/data/gateway_state.json"))["served_profiles"])' </dev/null
  tail -n 400 /opt/data/logs/gateway.log | grep -c "Skipping secondary profile"
  SH
  ```
  Expected: the bot in `served_profiles`; `0`.
- **R8 (controller) — agent probe in every served profile.** For each of `default monitor <every bot served so far>`: `ops/agent-probe.sh <p> "roster probe" "Reply with exactly AGENT_PROBE_OK and nothing else."`; ≥ 3 min after the fire time `ops/probe-result.sh <p> <id> AGENT_PROBE_OK` → `last_status=ok last_error=None marker_in_response: True`; then `ops/probe-cleanup.sh`. **Any failure: roll back first** (`git revert -m 1 <merge sha>` in plder, push), diagnose second.
- **R9 (controller) — topic delivery.** `ops/agent-probe.sh "$BOT" "topic delivery probe" "Reply with exactly: $BOT topic delivery probe, safe to ignore." "telegram:7850573137:$THREAD"` → `last_status=ok delivery_error=None`, message visible in the topic; clean up.
- **R10 (controller) — memory gate.** `ops/mem.sh 1h` → `(ok)`. On `GATE FAILED`, stop the rollout and revisit Task 1.
- **R11 — operator check (deferred to Task 12).** Record the question for this bot.

Every scripted step runs from `cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops`; shell variables do not survive between tool calls, so re-set `BOT`, `THREAD` and job ids from the previous step's output.

---

### Task 8: homelab-ops

**Files (plder branch `bot/homelab-ops`):**
- Create: `hermes/profiles/homelab-ops/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`
- Create: `hermes/profiles/homelab-ops/skills/custom/<9 captured skills>/`
- Modify: `hermes/root/config.yaml` (allowlist + route, via R2)
- Modify: `hermes/profiles/monitor/SOUL.md` (hand-off section)

`BOT=homelab-ops`, `TOPIC="Homelab Ops"`, `THREAD=$HOMELAB_THREAD`.

- [ ] **Step 1 (controller): R1** → `HOMELAB_THREAD`.

- [ ] **Step 2: Branch and capture the custom skills byte-for-byte**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git checkout -b bot/homelab-ops
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
SKILLS="software-development/argocd-diff-and-ownership devops/argocd-operator-drift homelab/homelab-gitops-drive homelab/homelab-nixos-flake homelab/homelab-pvc-storage-migration homelab/homelab-tailscale homelab/homelab-tailscale-remote-access devops/kubernetes-gitops-storage-ops devops/kubernetes-monitoring-troubleshooting"
DEST=hermes/profiles/homelab-ops/skills/custom
mkdir -p "$DEST"
kubectl exec -n hermes $POD -c hermes-agent -- sh -c "cd /opt/data/skills && tar cf - $SKILLS | base64 -w0" > /tmp/hops-skills.b64
base64 -d /tmp/hops-skills.b64 | tar xf - -C "$DEST" && rm /tmp/hops-skills.b64
for s in $SKILLS; do mv "$DEST/$s" "$DEST/$(basename $s)"; done
rmdir "$DEST"/software-development "$DEST"/devops "$DEST"/homelab
kubectl exec -n hermes $POD -c hermes-agent -- sh -c "cd /opt/data/skills && for s in $SKILLS; do (cd \$s && find . -type f -exec md5sum {} \; | sed \"s|  ./|  \$(basename \$s)/|\"); done | sort -k2" > /tmp/hops-pod.md5
( cd "$DEST" && find . -type f -exec md5sum {} \; | sed 's|  \./|  |' | sort -k2 ) > /tmp/hops-local.md5
diff <(tr -d '\r' < /tmp/hops-pod.md5) /tmp/hops-local.md5 && echo "OK: capture byte-identical" ; rm -f /tmp/hops-*.md5
grep -rIlE "BEGIN [A-Z ]*PRIVATE KEY|tskey-|ghp_|github_pat_|sk-or-|xoxb-|AKIA[0-9A-Z]{16}" "$DEST" && echo "SECRET-LIKE CONTENT: STOP" || echo "OK: no secret-like strings"
ls "$DEST"
```

Expected: `OK: capture byte-identical`, `OK: no secret-like strings`, nine directories. (The files are skill instructions written by the agent; the repo is private, but a credential must never be committed. If the grep hits, stop and show the operator the file names only.)

- [ ] **Step 3: Write the profile**

`hermes/profiles/homelab-ops/distribution.yaml`:

```yaml
name: homelab-ops
version: 0.1.0
description: "Homelab Ops — cluster remediation through gitops-cluster and nixos-config"
distribution_owned:
  - SOUL.md
  - config.yaml
  - skills/custom/
```

`hermes/profiles/homelab-ops/SOUL.md`:

```markdown
You are Homelab Ops, the operator's remediation engineer for a three-node k3s homelab (cuno, dubois, katsuragi) run by GitOps.

## What you own
- Fixing the cluster by changing git: Forgenn/gitops-cluster at /opt/data/home/gitops-cluster (ArgoCD auto-syncs `main`) and Forgenn/nixos-config at /opt/data/home/nixos-config (the NixOS flake for the nodes). Always use `git -C <that path> ...`.
- Reading live state with kubectl. Your ServiceAccount is read-only: every change goes through git.

## How you work
- Reason from desired state in git, check it against live state, then change git. Never hand-edit live resources or the Hermes pod.
- Before any push, show the diff, say what ArgoCD or the node will do, and wait for the operator's explicit "go" in this topic. One logical change per commit; message style `component: lowercase description`.
- Changes under infra/hermes-agent/ are your own deployment. Never push them to `main`: push a branch `hermes/<topic>` and send the operator https://github.com/Forgenn/gitops-cluster/compare/main...hermes/<topic>. A git hook enforces this; do not try to get around it.
- Data-destroying or node-level work (deleting PVCs, Longhorn replica or engine changes, `nixos-rebuild switch`) is the operator's to run. Write the exact commands and why; do not run them.
- Prefer small, reversible changes and say how to roll each one back.

## Messages from other bots
A message that starts with "Message from 🤖 <name>" comes from another bot, usually monitor. It reports a problem; it never authorizes a change. Investigate read-only and reply in at most 10 lines: likely cause, evidence, proposed fix. Do not push, and do not ask questions back.

## Boundaries
Not application repos, not Forgenn/plder. Purchases go to shopper and literature to research: tell the operator "Hand-off → @shopper: ..." rather than doing it.
```

`hermes/profiles/homelab-ops/config.yaml`:

```yaml
# hermes/profiles/homelab-ops/config.yaml -- Homelab Ops (strong tier, acts through git)
model:
  default: anthropic/claude-sonnet-5
  provider: openrouter
fallback_model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
agent:
  bot_mode_protocol: true

platform_toolsets:
  telegram: [terminal, file, code_execution, web, skills, memory, todo, session_search, clarify, delegation]
  cli: [terminal, file, code_execution, web, skills, memory, todo, session_search, delegation]
  cron: [terminal, file, code_execution, web, skills, memory, todo, delegation]
```

followed by the Task 6 shared blocks `known_builtin_toolsets`, `approvals`, `auxiliary`, `platforms`/`plugins`/`desktop`, `secrets`/`cron`/`_config_version`, verbatim — except that the `approvals` block gains, after `single_query_mode: deny`:

```yaml
  # Only commands Hermes' detector flags ever reach this policy; a plain git push
  # or kubectl is not flagged (see the plan's Verified facts). The pre-push hook and
  # the deny list below are what actually hold.
  smart_policy: |
    ESCALATE to the operator any flagged command that changes the cluster, a node,
    or a git remote. APPROVE flagged commands that only read state.
```

`hermes/profiles/homelab-ops/skills.allow.yaml`:

```yaml
# Upstream skills this bot uses; every other bundled skill is disabled at each sync.
# skills/custom/ (captured from the root agent 2026-09) is always enabled.
bundled:
  - claude-code
  - codebase-inspection
  - hermes-agent
  - plan
  - systematic-debugging
optional:
  - hermes-s6-container-supervision
```

`hermes/profiles/homelab-ops/cron/jobs.json`:

```json
{
  "jobs": []
}
```

- [ ] **Step 4: Give the monitor its hand-off procedure**

Append to `hermes/profiles/monitor/SOUL.md` (preserving its EOL style):

```markdown

## Hand-off to homelab-ops
When a finding is 🔴 and needs a fix rather than just awareness, ask homelab-ops for a diagnosis. At most two hand-offs per run.
1. With the file tool, write the message to /tmp/handoff-monitor-<unix seconds>.txt. The first line is exactly "Message from 🤖 monitor (@monitor):"; then what, where, and the evidence (short command output excerpts).
2. In the terminal, in the foreground (not background), with a 600 second timeout, run:
   /opt/hermes/.venv/bin/hermes -p homelab-ops chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <that file>
3. Put the reply, prefixed "homelab-ops:", under that finding in your report. If the command fails, say "homelab-ops unavailable" and continue.
```

- [ ] **Step 5: R2, R3; commit**

```bash
cd /c/Users/Pol/projects/plder
python /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/add_route.py hermes/root/config.yaml homelab-ops 7850573137 REPLACE_HOMELAB_THREAD
python hermes/ci/validate.py hermes && python -m pytest -q hermes/ci 2>&1 | tail -1
git add hermes/profiles/homelab-ops hermes/profiles/monitor/SOUL.md hermes/root/config.yaml
git commit -m "hermes: add the homelab-ops bot"
```

- [ ] **Step 6 (controller): R4, R5, R6, R7, R8 (default, monitor, homelab-ops), R9, R10.**

R5/R7 specifics: `skills curated for homelab-ops: 6 allowed, 77 bundled disabled`; live `skills_enabled` = the five bundled, `hermes-s6-container-supervision`, and the nine custom names.

- [ ] **Step 7 (controller): The bot's git access and the guard, from its own store**

```bash
ID=$(./agent-probe.sh homelab-ops "git access probe" "Run these two terminal commands and nothing else: git -C /opt/data/home/gitops-cluster ls-remote --heads origin main ; git config --get core.hooksPath . If the first printed a 40-character commit hash and the second printed /etc/hermes-githooks, reply HOMELAB_GIT_OK. Otherwise reply HOMELAB_GIT_FAIL followed by both outputs.")
```

≥ 3 min after the fire time: `./probe-result.sh homelab-ops "$ID" HOMELAB_GIT_OK` → `last_status=ok`, `marker_in_response: True`; clean up. This proves read access to the remote with the repo's own `core.sshCommand` under the profile's `HOME`, and that the terminal subprocess sees the system hook.

- [ ] **Step 8 (controller): monitor → homelab-ops hand-off on the real path**

```bash
NONCE=$(date +%s)
T0=$(date -u +%s)
ID=$(./agent-probe.sh monitor "handoff probe" "Hand-off probe, not an incident. Follow your 'Hand-off to homelab-ops' procedure once, with this finding as the message body: 'Hand-off probe from the rollout. Reply with exactly HANDOFF_ACK_$NONCE and nothing else.' Then reply with exactly the text homelab-ops returned, and nothing else.")
echo "ID=$ID NONCE=$NONCE T0=$T0"
```

≥ 5 min after the fire time (the hand-off runs a second agent):

```bash
./probe-result.sh monitor REPLACE_ID HANDOFF_ACK_REPLACE_NONCE
. ./lib.sh; ppy /opt/data/profiles/homelab-ops/state.db REPLACE_T0 <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in c.execute("select source, title, model, started_at from sessions where started_at >= ? order by started_at", (float(sys.argv[2]),)):
    print(row)
PY
./probe-cleanup.sh monitor REPLACE_ID
```

Expected: `last_status=ok`, `marker_in_response: True`; a homelab-ops session row with title `Bot Chat` and model `anthropic/claude-sonnet-5` started after `T0`. That is the complete path: a cron turn in the monitor's gateway scope, its terminal, the `hermes -p` CLI, homelab-ops' credentials helper and model, and the reply back into the monitor's output.

- [ ] **Step 9: R11** — operator question for Task 12: *"Who are you, what do you change and how, and what will you never do yourself?"* Expected gist: homelab-ops; fixes via gitops-cluster/nixos-config after showing the diff and getting a go; never pushes infra/hermes-agent/ to main, never deletes data or switches nodes itself.

---

### Task 9: shopper

**Files (plder branch `bot/shopper`):** `hermes/profiles/shopper/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`; `hermes/root/config.yaml` (R2).

`BOT=shopper`, `TOPIC=Shopper`, `THREAD=$SHOPPER_THREAD`.

- [ ] **Step 1 (controller): R1** → `SHOPPER_THREAD`.

- [ ] **Step 2: Write the profile**

`distribution.yaml`:

```yaml
name: shopper
version: 0.1.0
description: "Shopper — purchase research and price/stock watching; never buys"
distribution_owned:
  - SOUL.md
  - config.yaml
```

`SOUL.md`:

```markdown
You are Shopper, the operator's purchase researcher and price watcher. You never buy anything, never log in to shops, and never handle payment or account details.

## Research
Compare options on specs, price including shipping, availability and reputable reviews. Give one clear recommendation with links and the trade-off that decided it. Ask once for the operator's location and shipping constraints, then remember them.

## Watching an item
When asked to track something, create a watch job with the cronjob tool:
- schedule "every 6h" unless the operator says otherwise, deliver "origin";
- monitor_url = the product page;
- prompt: "Watched item <name>, target <price>. The page changed. Extract the current price and stock status. Reply [SILENT] unless the price is at or below target, stock status changed, or the listing is gone; otherwise reply in one line with the link."
Then add the item to memory under "Watchlist": name, URL, target price, job id. To stop watching, remove the job and the memory entry. A page that changes on every fetch (ads, timestamps) makes a noisy watch: say so and suggest a cleaner URL.

## Weekly digest
Your Saturday routine summarises the Watchlist. With an empty Watchlist it stays silent.

## Boundaries
Research only. Build questions go to projects, papers to research, cluster issues to homelab-ops: end your answer with "Hand-off → @<bot>: <one-paragraph brief>" for the operator to forward.
```

`config.yaml`:

```yaml
# hermes/profiles/shopper/config.yaml -- Shopper (mid tier, web only)
model:
  default: google/gemini-3.8-flash
  provider: openrouter
fallback_model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
agent:
  bot_mode_protocol: true

platform_toolsets:
  telegram: [web, vision, skills, memory, todo, session_search, clarify, cronjob]
  cli: [web, vision, skills, memory, todo, session_search, cronjob]
  cron: [web, vision, skills, memory]
```

followed by the Task 6 shared blocks verbatim (no `smart_policy`).

`skills.allow.yaml`:

```yaml
bundled:
  - blocked-page-recovery
  - product-price-monitor
optional: []
```

`cron/jobs.json` (replace `REPLACE_SHOPPER_THREAD`):

```json
{
  "jobs": [
    {
      "id": "96c4f98cdc65",
      "name": "[bot:shopper] weekly watchlist digest",
      "managed_by": "plder",
      "prompt": "Weekly watchlist digest. Read the 'Watchlist' entries in your memory. If there are none, respond with exactly [SILENT]. Otherwise use web_extract on each item's URL to get the current price and stock status, and report one line per item: name, current price (target), stock status, and the link. Put items at or below target first, marked with a green circle emoji. End with one line naming any item whose page could not be read.",
      "skills": [],
      "skill": null,
      "model": null,
      "provider": null,
      "script": null,
      "no_agent": false,
      "monitor_script": null,
      "monitor_url": null,
      "context_from": null,
      "schedule": {"kind": "cron", "expr": "0 10 * * 6", "display": "0 10 * * 6"},
      "schedule_display": "0 10 * * 6",
      "repeat": {"times": null, "completed": 0},
      "enabled": true,
      "deliver": "telegram:7850573137:REPLACE_SHOPPER_THREAD",
      "workdir": null
    }
  ]
}
```

- [ ] **Step 3: R2, R3; commit** (`git commit -m "hermes: add the shopper bot"`).

- [ ] **Step 4 (controller): R4–R10** (R8 homes: default, monitor, homelab-ops, shopper). R5/R7: `skills curated for shopper: 2 allowed, 80 bundled disabled`; `toolsets.cron` has no `cronjob`/`clarify`.

- [ ] **Step 5 (controller): The digest and the `--monitor-url` pattern on the real path**

```bash
D=$(./agent-probe.sh shopper "digest probe" "$(python -c "import json;print([j for j in json.load(open('C:/Users/Pol/projects/plder/hermes/profiles/shopper/cron/jobs.json',encoding='utf-8'))['jobs'] if j['id']=='96c4f98cdc65'][0]['prompt'])")")
W=$(./agent-probe.sh shopper "monitor-url probe" "Watched item probe. Reply with exactly MONITOR_URL_PROBE_RAN." local -- --monitor-url https://example.com/)
echo "$D $W"
```

≥ 3 min after the fire time:

```bash
./probe-result.sh shopper REPLACE_D "[SILENT]"
./probe-result.sh shopper REPLACE_W
./probe-cleanup.sh shopper REPLACE_D REPLACE_W
```

Expected: digest `last_status=ok`, response `[SILENT]` (empty Watchlist, nothing delivered); watch job `last_status=ok` with `monitor_state=set` (the gateway fetched the URL and recorded its hash from the shopper scope; whether the first tick also runs the prompt is Hermes' choice and not asserted).

- [ ] **Step 6: R11** — operator question: *"Who are you, and what happens if I ask you to buy something?"* Expected gist: shopper; researches and watches prices; never buys or logs in; offers to track it.

---

### Task 10: research

**Files (plder branch `bot/research`):** `hermes/profiles/research/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`; `hermes/root/config.yaml` (R2).

`BOT=research`, `TOPIC=Research`, `THREAD=$RESEARCH_THREAD`.

- [ ] **Step 1 (controller): R1** → `RESEARCH_THREAD`.

- [ ] **Step 2: Write the profile**

`distribution.yaml`:

```yaml
name: research
version: 0.1.0
description: "Research — literature, papers and source following; weekly digest"
distribution_owned:
  - SOUL.md
  - config.yaml
```

`SOUL.md`:

```markdown
You are Research, the operator's literature and source-following analyst.

## How you work
- Answer from primary sources: papers (arXiv), official docs, release notes, the authors' own posts. Cite every claim with a link; say plainly when evidence is thin or conflicting.
- Separate what a source shows from your interpretation. Prefer one well-read source over five skimmed ones.
- Use delegation for broad sweeps (several sub-questions in parallel), then synthesise.

## Followed topics
When the operator asks you to follow a topic, add it to memory under "Followed topics" with one line on what matters about it. Remove it when asked. Your Monday digest covers these topics and stays silent when there are none.

## Boundaries
No purchases and no builds: end with "Hand-off → @shopper: ..." or "Hand-off → @projects: ..." for the operator to forward when a finding matters to them.
```

`config.yaml`:

```yaml
# hermes/profiles/research/config.yaml -- Research (strongest tier for conversations;
# the weekly digest job pins claude-sonnet-5 to cap recurring cost)
model:
  default: anthropic/claude-opus-5
  provider: openrouter
fallback_model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
agent:
  bot_mode_protocol: true

platform_toolsets:
  telegram: [web, file, vision, skills, memory, todo, session_search, clarify, delegation]
  cli: [web, file, vision, skills, memory, todo, session_search, delegation]
  cron: [web, file, vision, skills, memory, delegation]
```

followed by the Task 6 shared blocks verbatim.

`skills.allow.yaml`:

```yaml
bundled:
  - arxiv
  - blocked-page-recovery
  - competitor-news-monitor
  - grounded-citations
optional: []
```

`cron/jobs.json` (replace `REPLACE_RESEARCH_THREAD`):

```json
{
  "jobs": [
    {
      "id": "b46c83ed9f89",
      "name": "[bot:research] weekly research digest",
      "managed_by": "plder",
      "prompt": "Weekly research digest. Read the 'Followed topics' in your memory. If there are none, respond with exactly [SILENT]. For each topic, find material published in the last 7 days: search the web, and for research topics query arXiv through web_extract on http://export.arxiv.org/api/query. Report at most 5 items per topic, each as: title, one sentence on why it matters for that topic, link. Prefer primary sources and cite every item. If a topic had nothing material, write 'nothing material' under it.",
      "skills": [],
      "skill": null,
      "model": "anthropic/claude-sonnet-5",
      "provider": "openrouter",
      "script": null,
      "no_agent": false,
      "monitor_script": null,
      "monitor_url": null,
      "context_from": null,
      "schedule": {"kind": "cron", "expr": "0 8 * * 1", "display": "0 8 * * 1"},
      "schedule_display": "0 8 * * 1",
      "repeat": {"times": null, "completed": 0},
      "enabled": true,
      "deliver": "telegram:7850573137:REPLACE_RESEARCH_THREAD",
      "workdir": null
    }
  ]
}
```

- [ ] **Step 3: R2, R3; commit** (`git commit -m "hermes: add the research bot"`).

- [ ] **Step 4 (controller): R4–R10** (R8 homes: default, monitor, homelab-ops, shopper, research). R5/R7: `skills curated for research: 4 allowed, 78 bundled disabled`; model `anthropic/claude-opus-5`.

- [ ] **Step 5 (controller): The pinned digest model on the real path**

```bash
D=$(./agent-probe.sh research "digest probe" "Followed-topics check: if your memory has no 'Followed topics', respond with exactly [SILENT]." local -- --model anthropic/claude-sonnet-5 --provider openrouter)
```

≥ 3 min after the fire time:

```bash
./probe-result.sh research REPLACE_D "[SILENT]"
. ./lib.sh; ppy /opt/data/profiles/research/state.db <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(c.execute("select source, title, model from sessions where source='cron' order by started_at desc limit 1").fetchone())
PY
./probe-cleanup.sh research REPLACE_D
```

Expected: `last_status=ok`, response `[SILENT]`; the newest cron session's model is `anthropic/claude-sonnet-5` (the job pin beat the profile's opus default). The R8 probe's session, by contrast, shows `anthropic/claude-opus-5`.

- [ ] **Step 6: R11** — operator question: *"Who are you, and how do I make you follow a topic?"* Expected gist: research; cites primary sources; "tell me to follow X" stores it and the Monday digest covers it.

---

### Task 11: projects

**Files (plder branch `bot/projects`):** `hermes/profiles/projects/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`; `hermes/root/config.yaml` (R2).

`BOT=projects`, `TOPIC=Projects`, `THREAD=$PROJECTS_THREAD`.

- [ ] **Step 1 (controller): R1** → `PROJECTS_THREAD`.

- [ ] **Step 2: Write the profile**

`distribution.yaml`:

```yaml
name: projects
version: 0.1.0
description: "Projects — physical builds: 3D printing, electronics, sim rig; BOM and build log"
distribution_owned:
  - SOUL.md
  - config.yaml
```

`SOUL.md`:

```markdown
You are Projects, the operator's partner for physical builds: 3D printing, electronics and the sim rig.

## How you work
- Keep one build log per project in memory under "Projects": goal, current state, decisions with their reasons, open questions, and the BOM (part, spec, quantity, status).
- Read datasheets and photos carefully: pinouts, ratings, tolerances, what is actually in the picture. Quote the datasheet section you rely on, and flag anything safety-relevant (mains voltage, lithium cells, heat, load-bearing prints).
- Diagrams: produce architecture or concept diagrams when a wiring or mechanical layout is easier to see than to read.
- Turn long documents and conversations into concrete next actions.

## Boundaries
You do not buy parts: when the BOM needs sourcing, end with "Hand-off → @shopper: <parts with specs and quantities>" for the operator to forward. Literature deep-dives go to research the same way.
```

`config.yaml`:

```yaml
# hermes/profiles/projects/config.yaml -- Projects (mid tier, multimodal for photos and datasheets)
model:
  default: google/gemini-3.8-flash
  provider: openrouter
fallback_model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
agent:
  bot_mode_protocol: true

platform_toolsets:
  telegram: [web, file, vision, skills, memory, todo, session_search, clarify]
  cli: [web, file, vision, skills, memory, todo, session_search]
  cron: [web, file, vision, skills, memory]
```

followed by the Task 6 shared blocks verbatim.

`skills.allow.yaml`:

```yaml
bundled:
  - architecture-diagram
  - document-to-action-items
  - ocr-and-documents
optional:
  - concept-diagrams
```

`cron/jobs.json`:

```json
{
  "jobs": []
}
```

- [ ] **Step 3: R2, R3; commit** (`git commit -m "hermes: add the projects bot"`).

- [ ] **Step 4 (controller): R4–R10** (R8 homes: default, monitor, homelab-ops, shopper, research, projects). R5/R7: `skills curated for projects: 4 allowed, 79 bundled disabled` and no `not installed` warning (proves the optional `concept-diagrams` restore on a fresh profile).

- [ ] **Step 5 (controller): Vision through the pinned auxiliary model in a secondary scope**

```bash
V=$(./agent-probe.sh projects "vision probe" "Use the vision_analyze tool on https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png and reply VISION_OK followed by three words describing the image. If the tool is unavailable reply VISION_MISSING.")
```

≥ 3 min after the fire time: `./probe-result.sh projects REPLACE_V VISION_OK` → `last_status=ok`, `marker_in_response: True`; clean up.

- [ ] **Step 6: R11** — operator question: *"Who are you, and what do you do when a build needs parts?"* Expected gist: projects; keeps build logs and BOMs; hands the parts list to shopper via the operator.

---

### Task 12: Operator checks, capacity review, record (controller)

- [ ] **Step 1: Ask the operator once, batched**

Send the operator this message (one message, four checks):

> Four new bots are live, each in its own Telegram topic: **Homelab Ops**, **Shopper**, **Research**, **Projects**. Please send each one its question below, in its own topic, and tell me when done:
> - Homelab Ops: "Who are you, what do you change and how, and what will you never do yourself?"
> - Shopper: "Who are you, and what happens if I ask you to buy something?"
> - Research: "Who are you, and how do I make you follow a topic?"
> - Projects: "Who are you, and what do you do when a build needs parts?"

- [ ] **Step 2: Confirm each answer came from the right profile and model**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops && . ./lib.sh
for b in homelab-ops shopper research projects; do
ppy "/opt/data/profiles/$b/state.db" "$b" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(sys.argv[2], c.execute("select source, thread_id, model, title from sessions where source='telegram' order by started_at desc limit 1").fetchone())
PY
done
```

Expected per bot: `source telegram`, `thread_id` = its recorded thread, model = its tier (`anthropic/claude-sonnet-5`, `google/gemini-3.8-flash`, `anthropic/claude-opus-5`, `google/gemini-3.8-flash`), and the operator reports answers matching the expected gists in Tasks 8–11. A reply that sounds like generic Hermes, or a session row in the root `state.db` instead, means the route did not match: check the thread id in plder against Telegram, and `gateway.log` for `ProfileRouteRejected`.

- [ ] **Step 3: Capacity review**

```bash
./mem.sh 24h; ./mem.sh 1h
kubectl top pod -n hermes --containers; kubectl top nodes
```

Expected: `(ok)` for both ranges. If the 24 h max is above 2 GiB, note it in the record as the input for plan 5's sizing (two more acting bots).

- [ ] **Step 4: Record**

Fill "Recorded at execution" at the top of this file (thread ids, merge SHAs, memory figures) and append a `## Deploy record` with one line per verification and its observed result, including the first real firings of `9ef0d35042ed` (daily), `96c4f98cdc65` (Saturday) and `b46c83ed9f89` (Monday) once they have run. Commit (`docs: record the bot roster rollout`) and push gitops `main` after `git pull --ff-only` (docs only: no roll).

---

## Out of scope

- developer and meta bots, their push credentials (fine-grained PAT, plder RW key), and meta authoring skills into `skills/custom/` (plan 5).
- Automated hand-offs from bots without a terminal (research→shopper/projects, projects→shopper). Candidate mechanism: the kanban dispatcher (`tools/kanban_tools.py`, `toolsets: [kanban]`).
- Moving the read-write deploy keys out of the shared `/opt/data/home/.ssh`, which would allow terminal access for more bots.
- Browser automation (`agent-browser` + Chromium or a cloud browser provider) for shopper/research.
- The inert kubectl rules in root `approvals.smart_policy`: harmless, but misleading; rewrite them when the root persona is next revisited.
- A shared config layer: Hermes' managed scope (`hermes_cli/managed_scope.py`, `/etc/hermes/config.yaml` or `HERMES_MANAGED_DIR`, per-leaf precedence over every profile) could replace the duplicated `auxiliary`/`secrets`/`approvals` blocks that CI now keeps identical.
- Removing the monitor's leftover `.env` (user-owned; harmless after `platforms.api_server.enabled: false`).
