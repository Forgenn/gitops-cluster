# Hermes developer + meta bots, credential isolation, install docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the credential-exposure hole plan 4 identified and deliberately left alone (every `hermes-secrets` key lands in the gateway's process environ; a stray key copy sits unmanaged on the PVC), then stand up the last two bots — **developer** (pushes to application repos) and **meta** (pushes to plder itself) — on plan 4's mechanisms with new, narrowly-scoped push credentials. Prove `skills/custom/` with a first real skill. Document how to install plder's AI config (`pi/` + `hermes/`) on any machine. Close the program out: run the spec's Verification section against all seven bots and update its Status line.

**Architecture:** developer and meta are plder profile distributions like every other bot (plan 4). Both need a terminal to do their job (git push), which reopens the exposure plan 4's Ruling 4 fenced off by simply not giving four of six bots a terminal at all ("Moving the read-write deploy keys out of the shared `/opt/data/home/.ssh`, which would allow terminal access for more bots" — plan 4 Out of scope). This plan does two things before adding them: (1) narrows what actually lives in the gateway's process environment — the four SSH private keys currently reach it via a blanket `envFrom` even though every consumer of them reads a **file**, never an env var, so removing them from `envFrom` costs nothing and closes the single largest hole the draft found; (2) gives each new bot its own credential on its own channel — developer's fine-grained PAT through the profile-scoped `terminal.env_passthrough` mechanism (verified below: the *name* allowlist is process-global and cached forever, the *value* resolves from whichever profile's secret scope is active when the terminal call happens), meta's plder write key through a new per-repo SSH alias, the same pattern already used for gitops-cluster/nixos-config/plder-read. Neither channel is a security boundary against another **terminal-capable** bot in the same pod/uid — homelab-ops, developer, meta and root all still share one filesystem and one process; this plan is explicit about that residual risk rather than pretending file-per-profile mounts create isolation they cannot create with one pod and one uid.

**Tech Stack:** same as plans 3/4 — Kubernetes (k3s) + Kustomize + ArgoCD v3.1.8; Python 3.12 (workstation) / 3.13 (image venv); pytest; PyYAML 6.0.3; GitHub Actions; `gh` CLI; Hermes Agent `nousresearch/hermes-agent:v2026.8.19` (hermes_cli 0.20.5); OpenRouter. Adds: `cryptography` 44.0.2 (only if the operator wants a scripted keypair for the rotation, plan 3 Task 8's pattern); Nix (`nixpkgs` `nixos-26.05`, `home-manager` `release-26.05`) for the install docs.

**Spec:** `docs/plans/2026-09-12-hermes-config-as-code.md` ("The roster" — developer, meta rows; "Credentials"; "Risks" — "Agent holds write keys to gitops-cluster"; "Open items"). Builds on plan 1 (`2026-09-13-hermes-config-sync-root.md`), plan 2 (`2026-09-13-hermes-monitor-bot.md`), plan 3 (`2026-09-14-hermes-ci-single-source.md`) and plan 4 (`2026-09-14-hermes-bot-roster.md`), **all assumed fully landed**: CI validates and auto-deploys plder master pushes touching `hermes/**`; memory limit 3Gi; `infra/hermes-agent/ops/` kit exists; `sync.py` `SYNC_VERSION = 3` with skill-allowlist curation; `hermes/ci/roster.py` wired into `validate.py`/`image_checks.py`; the pre-push guard (`infra/hermes-agent/githooks/`) is live; monitor, homelab-ops, shopper, research, projects are live, routed, and curated. This is the spec's last implementation phase — after this plan the spec's Status line moves from "design agreed" to "implemented."

---

## Verified facts this plan relies on

Carried over from the draft planner's research (2026-09-15, credential-isolation investigation) plus new source reads done for this plan. Re-verify against the image source if it changes (`nousresearch/hermes-agent:v2026.8.19` today).

### The credential-exposure hole (draft's core finding, now traced to source)
- `envFrom: secretRef: hermes-secrets` on the main container (`infra/hermes-agent/deployment.yaml`) puts **every** key of that Secret into the gateway process's environ — including the four multi-line SSH private keys (`GIT_DEPLOY_KEY`, `NIXOS_DEPLOY_KEY`, `FLEET_SSH_KEY`, `PLDER_DEPLOY_KEY_READ`).
- `/proc/<gateway pid>/environ` is readable by **any uid-10000 process**, including a subprocess spawned by a completely different bot's terminal turn: yama `ptrace_scope=1` gates `ptrace()` attach, not `/proc/PID/environ` reads, which the kernel gates only on matching uid (or `CAP_SYS_PTRACE`). Every bot's terminal runs as the same uid 10000 in the same pod. So **any terminal-capable bot can read every envFrom secret**, regardless of which profile's scope it is nominally running under.
- **The four SSH keys are consumed only as files, never as env vars.** Confirmed by reading the live `ssh_config` (ConfigMap `hermes-config`, key `ssh_config`): every `Host` block uses `IdentityFile /opt/data/home/.ssh/<name>`, never an env var. The `ssh-key-perms` initContainer copies the Secret's keys into files (`/ssh-keys/git_deploy_key` etc., chown/chmod 600) specifically because SSH refuses a group-readable key — that file-delivery path already exists and is the *only* path anything reads these keys through. **`envFrom` exposing them as env vars is pure redundancy with zero functional purpose** — the single biggest, lowest-risk fix available.
- **INCIDENT (previous planner, 2026-09-15):** a names-only environ dump split on `=` printed most lines of the four SSH private keys into a planning transcript stored under `~/.claude/projects` on the workstation. The operator is rotating all four keys now (Task 2 of this plan is that rotation's runbook). **This plan repeats none of that: every verification below proves presence/absence by key NAME or COUNT only, never by printing a value.**
- `/opt/data/home/keys/nixos_deploy_key` is a stray 0660 copy of the nixos key sitting directly on the PVC (not under the managed `/opt/data/home/.ssh/` tree the initContainer rebuilds every boot), backed up by restic. Nothing references it (not `ssh_config`, not `sync.py`). It is leftover cruft from before the current `ssh-key-perms` initContainer pattern existed and should be deleted while the key it holds is being rotated anyway.
- `agent/file_safety.get_read_block_error` (confirmed by reading the source on the live pod, 2026-09-16) blocks exactly three things: `skills/.hub` (prompt-injection defense), a fixed list of Hermes credential-store filenames under `HERMES_HOME`/root (`auth.json`, `auth.lock`, `.anthropic_oauth.json`, `.env`, `webhook_subscriptions.json`, `auth/google_oauth.json`, `cache/bws_cache.json`), `mcp-tokens/`, and project-local `.env*` basenames anywhere on disk. **None of these cover `/opt/data/home/.ssh/*` or `/proc/*/environ`.** The function's own docstring: *"This is NOT a security boundary… a determined model or malicious instruction can always shell out."*
- **New finding, contradicts an implicit assumption in plan 4 Ruling 4:** `tools/file_tools.py`'s `read_file` implementation calls `get_read_block_error` and nothing else gates the path — there is **no sandboxing to a profile's home directory**. Plan 4 gave `research` and `projects` the **`file`** toolset (no terminal) on the assumption that removing terminal/code_execution removes the risk. It does not fully: a bot with only the `file` toolset can call `read_file("/opt/data/home/.ssh/git_deploy_key")` or `read_file("/proc/1/environ")` directly, with no denial, because neither path matches the three blocked categories. **This plan's Task 1 (narrowing `envFrom`) also closes most of this for `research`/`projects`**, since after it the SSH keys are gone from environ entirely and reading the key **files** only yields what `research`/`projects` could already reach if they had terminal (which plan 4 decided they should not have) — so the practical fix is the same fix, but the true boundary is "no `file` or `terminal` toolset for any bot that must not reach `/opt/data/home/.ssh/`", not just "no terminal." This is flagged for the operator; existing bots are plan 4's territory and not touched here, but developer and meta (this plan) both get `file`, so their own credentials matter more, not less.
- **`terminal.env_passthrough` mechanics (confirmed from source, `tools/env_passthrough.py`, `tools/environments/local.py`):**
  - `_HERMES_PROVIDER_ENV_BLOCKLIST` (built once at import) explicitly includes both `GH_TOKEN` and `GITHUB_TOKEN` (confirmed by reading `_build_provider_env_blocklist`/the blocklist literal). `register_env_passthrough` and `_load_config_passthrough` both refuse to allowlist any name in this set, "fail closed" (an import failure treats an unknown name as protected too). **So the developer PAT must travel under a name other than `GH_TOKEN`/`GITHUB_TOKEN`** — this plan uses `DEVELOPER_GITHUB_PAT`, which is not in the blocklist (it isn't a Hermes-managed provider credential name).
  - `CLAUDE_CODE_OAUTH_TOKEN` is the **one** deliberate exception `_build_provider_env_blocklist()` discards from the blocklist (comment: stripping it broke agent-spawned `claude` CLIs, #55878) — it is expected to be a real process env var. No other credential in `hermes-secrets` has this exception.
  - `_load_config_passthrough()` reads `hermes_cli.config.read_raw_config()`'s `terminal.env_passthrough` list once and **caches the result for the life of the process** (`if _config_passthrough is not None: return _config_passthrough` — no mtime/staleness check, unlike `read_raw_config` itself). **Whichever config is read the *first* time any code calls this function wins, permanently, until the pod restarts.** `get_config_path()` resolves against the caller's `HERMES_HOME`/profile context, so if some non-root profile's turn happened to be the very first caller after boot, and that profile's config does not declare `DEVELOPER_GITHUB_PAT`, the passthrough allowlist would exclude it for the rest of the pod's life even during a later developer turn. Declaring `terminal.env_passthrough: [DEVELOPER_GITHUB_PAT]` **in `hermes/root/config.yaml`** is the safe choice: root is always read before any secondary profile in gateway boot, so it is reliably the first caller. This plan's Task 3 verification includes firing a probe in a *different* profile first, specifically to catch this ordering fragility on the real pod rather than assume it away.
  - `resolve_passthrough_value(name, fallback)` — the actual per-call value lookup — is **not** cached: it reads `current_secret_scope()` live and calls `get_secret(name)` (or refuses with `fallback=None` under an active multiplex scope with no match). So even though the *name* allowlist is global and frozen, the *value* is scoped correctly per active profile at call time: a shopper or monitor turn calling `resolve_passthrough_value("DEVELOPER_GITHUB_PAT")` gets `None` (their scope was never given that secret), and only a developer-scoped terminal call, whose scope's `secrets.command` helper was extended to read the file, gets the real value.
- **`/etc/hermes-profile-secrets` is one shared directory, read by an identical shell one-liner in every profile's `secrets.command`.** Adding a new file to that *same* mount would put it into **every** profile's resolved secret scope, not just the intended one (the helper is `for f in /etc/hermes-profile-secrets/*; do …`, unconditional). A new per-bot secret must go in its **own** volume/mount, read by only that bot's own extended `secrets.command`, or every other bot's `get_secret()` calls could resolve it too.
- `hermes/root/config.yaml` today has no `terminal:` key at all (confirmed by reading the file). No served profile declares one either.

### GitHub / Infisical state (verified 2026-09-16, read-only)
- `gh api user/repos --paginate` (authenticated as `Forgenn`) lists **39** repositories (2 duplicate listings of `Ambient-Detector`, so 38 distinct), mixing public and private. `gitops-cluster` (public, `main`), `nixos-config` (public, `main`), `plder` (**private**, `default_branch: master`) are the three excluded from the developer PAT's scope; every other repo (`loomie`, `AH`, `AH-warehouse`, `Ambient-Detector`, `coend`, `Dotfiles`, `Hardened-Browser`, and ~30 more, several private) is in scope. **The operator's fine-grained PAT screen requires selecting ~35 individual repositories** ("only select repositories" has no "all except" option on GitHub) — this is a real, non-trivial manual step; Task 4 gives the operator the exact list to paste.
- `plder` confirmed private/Free-tier: repository rulesets and branch protection return `403 Upgrade to GitHub Pro` (draft's finding, not re-tested to avoid another write-shaped call; visibility alone is enough — GitHub's docs place rulesets/branch protection for private repos behind Team/Enterprise or a Pro personal plan). `gitops-cluster` and `nixos-config` are public, where rulesets are available on any plan.
- plder deploy keys today: `163153110` read-only (the sync's, in active use), `163152504` reserved read-write "hermes deploy" (public key registered, **private half not yet mapped into `hermes-secrets`** — common-context 2026-09-14). Believed stored at Infisical `/hermes/PLDER_DEPLOY_KEY`; this plan verifies that by adding the ESO mapping and reading `.status.conditions`, never the secret value (Task 6).
- Plan 3 Task 8 pattern (reusable for Task 6 if the believed key turns out to be missing): a workstation Python script using `cryptography`'s `Ed25519PrivateKey`, or a pod-generated fallback via `ssh-keygen` piped through `KEYGEN=pod`, writes the private half straight into a `gh secret set`/`Infisical` call and never touches disk; the fallback path documented there is exactly what Task 6 falls back to if the ESO status shows the secret is genuinely absent.

### plder / nixos-config conventions (verified 2026-09-16, read-only, public repos)
- `Forgenn/nixos-config`'s `flake.nix`: `nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05"`; `home-manager` input `url = "github:nix-community/home-manager/release-26.05"` with `inputs.nixpkgs.follows = "nixpkgs"`; a `hermes-agent` flake input already exists (`github:NousResearch/hermes-agent`) but nothing in `modules/home-manager/` yet installs the `hermes` or `pi` CLI or profile config — this plan's Nix module is genuinely new, not a rename of something existing.
- The exact HM-module-import idiom, read from `hosts/as-pm/default.nix`: `home-manager.users.${user} = { pkgs, lib, self, ... }: { imports = [ (self + "/modules/home-manager/base.nix") … ]; home.packages = [ … claude-code … ]; }`. `claude-code` is already a `home.packages` entry there (nixpkgs ships it); no `pi`/`hermes` package exists yet.
- `plder/pi/install.sh` is explicitly marked legacy in its own banner ("NOTE: Symlink install is legacy. Prefer: `pi install git:github.com/Forgenn/plder`"). It symlinks `pi/{settings.json,keybindings.json,models.json}` and every `pi/extensions/*.ts`, `pi/skills/*/`, `pi/themes/*.json` into `${HOME}/.pi/agent/{…}`. `pi/init-gh.sh` is a best-effort `gh auth` check, not required.
- `plder/package.json` `peerDependencies`: `"@mariozechner/pi-coding-agent": ">=0.62.0"`. Workstation has pi 0.57.0 via npm (draft, still true as of this plan — not re-verified, would require touching the live npm global install).
- Hermes Desktop CLI on the workstation: `%LOCALAPPDATA%\hermes\hermes-agent\bin\hermes`, `HERMES_HOME=%LOCALAPPDATA%\hermes` (draft; a live install, not to be used for rehearsals per Global Constraints).

### Repo state as of this plan (2026-09-16, read-only)
- `infra/hermes-agent/deployment.yaml` in gitops-check today still shows plan 4's *starting* state (512Mi/2Gi resources; no `ops/` references) and `infra/hermes-agent/sync/sync.py` still shows `SYNC_VERSION = 2` (no `curate_skills`). `infra/hermes-agent/githooks/{pre_push.py,gitconfig,test_pre_push.py}` already exist on disk. `plder/hermes/ci/{roster.py,test_roster.py}` already exist but are **not yet imported** by `validate.py`/`image_checks.py` (`grep roster` finds nothing there). **This confirms plan 4 has not fully landed in the working trees this plan was drafted against** — per the brief, this plan is written assuming it lands first; every file path and mechanism below assumes plan 4's Task 3 (`curate_skills`, `SYNC_VERSION = 3`), Task 4/6 (`roster.py` wired in), and Task 2 (`ops/` kit) are live before Task 3 of *this* plan runs. If they are not, STOP and land plan 4 first — this plan's CI rules (Task 4, 6) and ops kit calls will not resolve otherwise.

---

## Rulings (decided here; the operator's only two open inputs are flagged with **OPERATOR**)

1. **Credential-isolation scope for one pod / one uid.** Full isolation (separate uids or pods per acting bot) is out of scope — unchanged from plan 4's Out of scope note. What this plan actually delivers: (a) remove the four SSH private keys from the gateway's blanket `envFrom` (Task 1) — a pure win, they were never read as env vars; (b) never put `DEVELOPER_GITHUB_PAT` or `PLDER_DEPLOY_KEY` (write) in `envFrom` at all, ever; (c) deliver each new credential through the narrowest existing channel (per-profile secret-scope file for the PAT, a new SSH alias + file for meta's key) so a bot that never needs it never resolves it through Hermes' own credential-scope logic. **Explicitly not delivered, and stated as residual risk**: a terminal-capable bot (root/default, homelab-ops, developer, meta) can still read `/opt/data/home/.ssh/*` directly (`cat`) or, per the file-toolset finding above, via `read_file` if it also carries the `file` toolset — nothing in this plan or plan 4 stops that. The mitigation is narrowing *which* bots get terminal/file at all (plan 4's existing choice) and keeping each new key's *repo scope* as narrow as possible (a PAT that cannot touch gitops-cluster/nixos-config/plder; a deploy key that can only write plder), so a compromised or malicious terminal-capable bot's blast radius is bounded by what that bot's *own* credential can do, not by file permissions.
2. **`envFrom` narrowing is additive-safe, not a guess.** Every key removed from the explicit replacement list in Task 1 is proven redundant by `ssh_config`'s `IdentityFile` usage (never env-read). Every key kept (`OPENROUTER_API_KEY`, the three `HERMES_DASHBOARD_*`, four `TELEGRAM_*`, `CLAUDE_CODE_OAUTH_TOKEN`) stays, unchanged, because this plan found no equivalent proof they are unused as env vars and a wrong guess there breaks the dashboard, Telegram delivery, or the OpenRouter provider path for the *default* profile before multiplex context exists. Task 1's verification (full agent-probe kit across every served profile plus the dashboard's own health check) is the actual proof, not this ruling.
3. **Developer bot's PAT channel:** `terminal.env_passthrough: [DEVELOPER_GITHUB_PAT]` declared in `hermes/root/config.yaml` (reliable first-caller per the cache-timing finding above); the value delivered through a **new, separate** Kubernetes Secret/volume (`hermes-secrets-developer` / `/etc/hermes-profile-secrets-developer`), read only by developer's own extended `secrets.command`; consumed by a new git-credential helper registered in the existing `/etc/gitconfig` (the same ConfigMap that already carries the pre-push hook). HTTPS, not SSH — fine-grained PATs are HTTPS-only, and GitHub deploy keys are per-repo (would need ~35 keys for ~35 repos), which is exactly why the spec chose a PAT for this bot.
4. **PAT presence is optional and independently gated from everything else.** `DEVELOPER_GITHUB_PAT` gets its **own** `ExternalSecret` (not a new key added to the existing `hermes-secrets` one) specifically so that a not-yet-created Infisical path fails only that small resource's reconcile (visible in `.status.conditions`) and never blocks or stalls the main `hermes-secrets` Secret's refresh for every other credential. Same ruling for `PLDER_DEPLOY_KEY` (meta, Task 6) — its own `ExternalSecret` too, for the identical reason, and because plan 3 Task 8's fallback-generation pattern needs somewhere safe to land a freshly-minted key without touching the production `hermes-secrets` blast radius.
5. **Meta bot's plder write channel:** a new `ssh_config` `Host github.com-plder-write` alias with `IdentityFile /opt/data/home/.ssh/plder_deploy_key_write`, delivered by extending the existing `ssh-key-perms` initContainer's optional-key pattern (same shape as `PLDER_DEPLOY_KEY_READ` today: `set +e`, soft-fail, `chmod 600`). meta's git remote for plder is configured to use this host alias, never the shared `github.com-plder` (read-only) alias, and never `git_deploy_key`/`nixos_deploy_key`.
6. **CI-credential protection for plder (no rulesets/branch protection available — Free private repo):** the workflow itself refuses to run its deploy step when the pushed commit touches `.github/**`, and a separate, always-on tripwire step **on every push regardless of author** posts to the operator's Telegram home channel whenever a commit changes `.github/**` or `hermes/ci/**` (the surface that could disable or weaken CI). This is chosen over (a) a pull-based watcher (adds a second polling process and a second point of failure for no extra protection — the workflow already runs on every push) and (b) trying to keep the deploy key's blast radius small by rotating it frequently (rotation is a mitigation for key *leakage*, not for a malicious commit from a credential that is *working as intended*; already covered by Task 2's unrelated rotation runbook). **Residual risk, stated plainly:** meta's own deploy key can rewrite `.github/workflows/hermes-config.yml` itself and push that in the same commit that also disables the tripwire (both live in the same repo, protected by nothing but CI review of *content*, not *who*) — GitHub Free gives no server-side way to stop a repo's own valid write credential from touching `.github/**`. The tripwire mitigates by making such a change loud immediately, not by preventing it; a determined bypass (a single commit doing everything at once, before the tripwire step even runs) is not stopped. Upgrading to GitHub Pro (~$4/mo) would unlock rulesets and close this properly; **flagged for the operator as a real option**, not adopted here since it is a billing decision, not an engineering one.
7. **`skills/custom/` is already exercised (homelab-ops, plan 4) but never against the open spec question** ("whether the loader picks up `skills/custom/<name>/SKILL.md` identically to a bundled category"). This plan writes and deploys one bot-authored-shaped skill (`plder-authoring`, under meta, since meta is the bot whose job is authoring skills per the spec's roster row) and *proves* the open item on the real pod (`skills_list`/`skill_view` sees it, `platform_toolsets` gating and `skills.allow.yaml` curation both treat it identically to a bundled skill it is not — i.e. curation never disables it), rather than continue to assume the shape matches.
8. **Install docs live in plder, not gitops-check or nixos-config.** plder documents how to bring its *own* AI config into any environment; it does not manage Claude Code or Hermes Desktop themselves (an explicit non-goal, common-context "Operator decisions 2026-09-14"). The Nix path is a `flake.nix` + `homeManagerModules.default` **in plder itself**, consumed by `nixos-config` (or any other flake) the same way `nixos-config` already consumes `hermes-agent`'s flake, rather than nixos-config growing plder-specific modules — this keeps plder's own install method versioned with plder, not with the fleet's config.
9. **`pi` has no Nix package.** `@mariozechner/pi-coding-agent` is an npm package with no `nixpkgs` derivation. The Nix module does not attempt to package it from source (out of scope, npm packages with native postinstall steps are a poor `buildNpmPackage` fit without real investment); it declares a `home.activation` step that runs a pinned `npm install -g @mariozechner/pi-coding-agent@^0.62.0` (matching plder's own `peerDependencies` floor) and lets Nix manage everything else (the `hermes` CLI via the existing `hermes-agent` flake input, and the config files via `home.file`/`xdg.configFile`, mirroring `pi/install.sh`'s own symlink targets). This is stated as a known compromise, not a fully-reproducible Nix closure.
10. **Program close-out capacity ruling:** developer and meta both get the `strong` tier per the spec's roster table (`anthropic/claude-sonnet-5`) with `fallback_model` = cheap, matching homelab-ops (plan 4 Ruling 1) — they are the other two bots that act. Both get terminal + file + code_execution (developer) or terminal + file (meta, no code_execution — meta edits YAML/Markdown/Python by hand through git, not by running arbitrary generated code). Plan 4's capacity ceiling (1h max working set < 2.25 GiB, 75% of the 3Gi limit) is re-checked in Task 10 with all seven bots live; if it is breached, the limit is raised again there rather than throttling a bot.
11. **OPERATOR input 1 (already recorded, common-context):** the fine-grained PAT itself — the operator creates it at Infisical `/hermes/DEVELOPER_GITHUB_PAT`, scope "only select repositories" = every Forgenn repo except `gitops-cluster`, `nixos-config`, `plder` (Task 4 gives the exact ~35-repo list), permissions Contents/Pull requests/Issues/Workflows RW, Metadata R.
12. **OPERATOR input 2 (in flight, per the draft's incident):** the four-key rotation (Task 2) — the operator generates each new keypair and pastes the private half into Infisical; this plan's runbook is the checklist and the GitHub-side + nixos-config-side + verification half of that work, run by the controller once the operator confirms each paste.

## Global Constraints

Everything in plan 3's and plan 4's "Global Constraints" applies verbatim, notably: image pinned at `nousresearch/hermes-agent:v2026.8.19`, `HERMES_HOME=/opt/data`; `sync.py` must never fail the pod; hermetic tests; `s6-setuidgid hermes` for in-pod writes as root; `</dev/null` inside `sh -s` heredocs; **no `profile install` or sync rehearsals in the live container** (use the throwaway-pod pattern); scheduled (never `cron run`) probes checked by `last_status` **and** the `## Response` section; ArgoCD `Synced` can hide a `ComparisonError` — check `.status.conditions` and controller logs; **no pod rolls 08:45–09:15 UTC**; volsync `hermes-data` `lastSyncTime` within 36h before any roll; `git pull --ff-only` before any manual gitops commit (the CI bot also commits to `main`); plder is CRLF in the working tree (`core.autocrlf=true`) — hash blobs with `git show <sha>:<path> | md5sum`, never the working-tree file; Git Bash needs `export MSYS_NO_PATHCONV=1`; the workstation `HERMES_HOME` is a live Hermes Desktop install — never touch it from a test or rehearsal.
- **HARD SECURITY RULE for this plan specifically, binding on every step below:** no command in this plan may print an environment or secret *value*. Every verification of "is this credential present/absent/correct" uses a key-NAME check (`grep -c "^NAME="`, which prints a count, never the matched line), a boolean (`[ -n "$VAR" ] && echo set`), an `.status.conditions`/`.status.sync` read (ExternalSecret, Application), a fingerprint (`ssh-keygen -lf`), or a functional probe that only reports success/failure (`git ls-remote`, an agent-probe marker string). If a step in this plan ever seems to need a raw value on screen to proceed, stop and redesign the step — this is exactly how the incident behind Task 2 happened.
- **Controller-only steps** (subagents must not run them): every `git push`, PR create/merge/close, generating or handling any key material (Task 2, and Task 6 Step 1 only if the fallback path is needed), anything reading a Secret's data (not just its status) into a pod, and every step touching the live pod or rolling it. Marked **(controller)**.
- **The Kubernetes API on dubois drops long `kubectl exec` streams** (plan 4's finding, unchanged). Keep in-pod output short; re-run a read-only step that dies with `connection forcibly closed`; never re-run a write step without first checking whether it already applied.
- **Multiplex rule (plan 2, binding):** any change to a served profile's config, its allowlist, or its credentials is verified only when a scheduled agent-mode probe passes in **every** served profile's store — by the time this plan runs, that is `default, monitor, homelab-ops, shopper, research, projects`, plus `developer`/`meta` once each lands.
- Workstation paths: gitops-check `C:\Users\Pol\projects\gitops-check`, plder `C:\Users\Pol\projects\plder`. The `ops/` kit from plan 4 lives at `infra/hermes-agent/ops/` in gitops-check and is used throughout this plan (`agent-probe.sh`, `probe-result.sh`, `probe-cleanup.sh`, `profile-state.sh`, `rehearse-sync.sh`, `wait-deploy.sh`, `mem.sh`, `create-topic.sh`, `add_route.py`). `PROFILE` is `default` for the root home.
- **Shell state does not survive between tool calls.** Values printed by one step (job ids, fire times, thread ids, SHAs, deploy-key ids/fingerprints) appear as `REPLACE_…` below; set them from the recorded output of the step that printed them.

## Recorded at execution (fill in)

| Item | Value |
|---|---|
| `DEVELOPER_THREAD` | |
| `META_THREAD` | |
| Rotated key fingerprints (GIT_DEPLOY_KEY, NIXOS_DEPLOY_KEY, FLEET_SSH_KEY, PLDER_DEPLOY_KEY_READ) | |
| `PLDER_DEPLOY_KEY` (write) fingerprint / origin (existing vs freshly generated) | |
| plder merge SHAs (T4, T5, T7, T8, T9) | |
| gitops SHAs (T1, T3, T6) | |
| `DEVELOPER_GITHUB_PAT` presence at first check / at Task 10 | |
| Memory 1h max after developer, after meta | |

---

### Task 1: Narrow the gateway's process environment (gitops-cluster)

Replace the blanket `envFrom: hermes-secrets` on the main container with an explicit `env:` list that omits exactly the four SSH private keys (proven redundant with `envFrom` above — every consumer reads the file, never the env var). Nothing else changes: every other currently-injected key stays, unchanged, because removing it is not proven safe here.

**Files:**
- Modify: `infra/hermes-agent/deployment.yaml` (main `hermes-agent` container: `envFrom` → `env`)

**Interfaces:** none new; this only changes what the existing `hermes-secrets` Secret feeds into the main container's process environment. The `git-deploy-key`, `fleet-ssh-key`, `nixos-deploy-key`, `plder-deploy-key-read` volumes (file delivery) are untouched.

- [ ] **Step 1: Branch and record the baseline**

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only && git checkout -b hermes-narrow-envfrom
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes -l app=hermes-agent --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
# Key-NAME-only proof of today's exposure (count, never a value) — this is what Step 4 must flip to 0.
kubectl exec -n hermes "$POD" -c hermes-agent -- sh -c '
for k in GIT_DEPLOY_KEY NIXOS_DEPLOY_KEY FLEET_SSH_KEY PLDER_DEPLOY_KEY_READ OPENROUTER_API_KEY CLAUDE_CODE_OAUTH_TOKEN; do
  n=$(tr "\0" "\n" < /proc/1/environ | grep -c "^$k=")
  echo "$k in environ: $n"
done'
```

Expected today: the four SSH keys each `1`; `OPENROUTER_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` each `1`. (PID 1 is the gateway in this container per the image's entrypoint — confirmed by plan 3's boot-order finding; if this container's PID 1 is not the gateway, use `pgrep -f 'gateway run'` first, still printing only a PID, never output that could contain a value.)

- [ ] **Step 2: Edit the container's env delivery**

```bash
python - <<'PY'
p = "infra/hermes-agent/deployment.yaml"
s = open(p, encoding="utf-8").read()
old = """          envFrom:
            - secretRef:
                name: hermes-secrets
"""
new = """          # Explicit allowlist, NOT envFrom: the four SSH deploy keys used to reach
          # this container's process environ via envFrom even though every
          # consumer (ssh_config's IdentityFile lines) reads them as FILES, never
          # as env vars -- envFrom was pure redundant exposure. /proc/<pid>/environ
          # is readable by any uid-10000 process, i.e. by any terminal-capable
          # bot's subprocess regardless of which profile's scope it runs under
          # (docs/plans/2026-09-15-hermes-developer-meta-docs.md Task 1). Keep this
          # list to exactly what is proven needed as a raw env var; the ssh-keys
          # volumes below remain the only channel for GIT_DEPLOY_KEY,
          # NIXOS_DEPLOY_KEY, FLEET_SSH_KEY and PLDER_DEPLOY_KEY_READ.
          # DEVELOPER_GITHUB_PAT and PLDER_DEPLOY_KEY (write) must NEVER be added
          # here -- they travel through the narrower channels in Tasks 3-6.
          env:
            - name: OPENROUTER_API_KEY
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: OPENROUTER_API_KEY}
            - name: HERMES_DASHBOARD_BASIC_AUTH_USERNAME
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: HERMES_DASHBOARD_BASIC_AUTH_USERNAME}
            - name: HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: HERMES_DASHBOARD_BASIC_AUTH_PASSWORD}
            - name: HERMES_DASHBOARD_BASIC_AUTH_SECRET
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: HERMES_DASHBOARD_BASIC_AUTH_SECRET}
            - name: TELEGRAM_BOT_TOKEN
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: TELEGRAM_BOT_TOKEN}
            - name: TELEGRAM_ALLOWED_USERS
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: TELEGRAM_ALLOWED_USERS}
            - name: TELEGRAM_HOME_CHANNEL
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: TELEGRAM_HOME_CHANNEL}
            - name: TELEGRAM_CRON_THREAD_ID
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: TELEGRAM_CRON_THREAD_ID}
            - name: CLAUDE_CODE_OAUTH_TOKEN
              valueFrom:
                secretKeyRef: {name: hermes-secrets, key: CLAUDE_CODE_OAUTH_TOKEN}
"""
assert s.count(old) == 1, "envFrom block not found verbatim (has plan 4 changed this file already?)"
open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
PY
kubectl kustomize infra/hermes-agent | python -c "
import sys, yaml
d = next(x for x in yaml.safe_load_all(sys.stdin) if x and x['kind'] == 'Deployment')
c = next(c for c in d['spec']['template']['spec']['containers'] if c['name'] == 'hermes-agent')
print('envFrom:', c.get('envFrom'))
print('env secretKeyRef names:', sorted(e['name'] for e in c['env'] if 'valueFrom' in e))"
```

Expected: `envFrom: None`; the 8 names above, sorted.

- [ ] **Step 3: Commit**

```bash
git add infra/hermes-agent/deployment.yaml
git commit -m "hermes: stop exposing the four SSH deploy keys via envFrom (file-delivery only)"
```

- [ ] **Step 4 (controller): Push, roll, verify absence and continued presence**

Gates: not 08:45–09:15 UTC; volsync within 36h.

```bash
cd /c/Users/Pol/projects/gitops-check && git pull --rebase origin main && git push origin main
GITOPS_SHA=$(git rev-parse HEAD); export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl get application hermes-agent -n argocd -o jsonpath='{.status.sync.status} {.status.health.status} conditions={.status.conditions}{"\n"}'
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
POD=$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n hermes "$POD" -c hermes-agent -- sh -c '
for k in GIT_DEPLOY_KEY NIXOS_DEPLOY_KEY FLEET_SSH_KEY PLDER_DEPLOY_KEY_READ OPENROUTER_API_KEY CLAUDE_CODE_OAUTH_TOKEN HERMES_DASHBOARD_BASIC_AUTH_SECRET TELEGRAM_BOT_TOKEN; do
  n=$(tr "\0" "\n" < /proc/1/environ | grep -c "^$k=")
  echo "$k in environ: $n"
done'
```

Expected: `Synced Healthy conditions=`; the four SSH keys now `0`; the other four still `1`.

- [ ] **Step 5: Full-roster probe and functional git/dashboard proof**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
for p in default monitor homelab-ops shopper research projects; do
  eval "P_$p=\$(./agent-probe.sh $p 'envfrom-narrow probe' 'Reply with exactly ENVFROM_NARROW_OK and nothing else.')"
done
```

≥ 3 min after the fire time, for each profile: `./probe-result.sh <p> "$P_<p>" ENVFROM_NARROW_OK` (expect `last_status=ok`, `marker_in_response: True`) then `./probe-cleanup.sh <p> "$P_<p>"`. Then, from homelab-ops (proves the file-mounted SSH keys still authenticate with envFrom gone):

```bash
ID=$(./agent-probe.sh homelab-ops "git still works probe" "Run exactly this terminal command and nothing else: git -C /opt/data/home/gitops-cluster ls-remote --heads origin main . If it printed a 40-character commit hash, reply GIT_STILL_OK. Otherwise reply GIT_STILL_FAIL and the error.")
```

≥ 3 min later: `./probe-result.sh homelab-ops "$ID" GIT_STILL_OK`, then clean up. Finally the dashboard basic-auth path (proves `HERMES_DASHBOARD_*` survived the switch to explicit `env:`):

```bash
export MSYS_NO_PATHCONV=1
kubectl exec -n hermes "$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')" -c hermes-agent -- \
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:9119/health
```

Expected: `200`.

**Rollback:** `git revert <commit>` and push; ArgoCD re-rolls with `envFrom` restored.

---

### Task 2: Four-key rotation runbook (controller, in flight)

The operator is rotating `GIT_DEPLOY_KEY`, `NIXOS_DEPLOY_KEY`, `FLEET_SSH_KEY`, `PLDER_DEPLOY_KEY_READ` right now because of the incident above. This task is the checklist for the GitHub-side, nixos-config-side and verification half of that work — the operator generates each keypair and pastes the private half into Infisical themselves (never this session); nothing here ever sees a private key value. Also removes the stray `/opt/data/home/keys/nixos_deploy_key` copy while the key it holds is being rotated anyway.

**Files:** none in git for the rotation itself; one `nixos-config` PR for `FLEET_SSH_KEY`'s public half.

- [ ] **Step 1 (controller): Confirm scope with the operator**

Message: *"Rotating 4 keys: GIT_DEPLOY_KEY (gitops-cluster, RW), NIXOS_DEPLOY_KEY (nixos-config, RW), FLEET_SSH_KEY (dolores/dubois/cuno/katsuragi, the `hermes-agent` user), PLDER_DEPLOY_KEY_READ (plder, RO). For each: generate an ed25519 keypair, paste the PRIVATE half into Infisical at `/hermes/<NAME>`, and send me the PUBLIC key line (public keys are safe to share) plus its `ssh-keygen -lf` fingerprint. I'll register each public key on GitHub/nixos-config and delete the old one, then verify."*

- [ ] **Step 2 (controller), per GitHub-hosted key (`GIT_DEPLOY_KEY` → gitops-cluster, `NIXOS_DEPLOY_KEY` → nixos-config, `PLDER_DEPLOY_KEY_READ` → plder): register the new public key, delete the old**

```bash
REPO=Forgenn/REPLACE_REPO   # gitops-cluster | nixos-config | plder
TITLE="hermes-agent-rotated-$(date -u +%Y%m%d)"
gh api "repos/$REPO/keys" --jq '.[] | "\(.id) \(.title) read_only=\(.read_only)"'   # find the OLD key's id
NEW=$(gh api "repos/$REPO/keys" -f title="$TITLE" -f key="REPLACE_OPERATOR_PUBLIC_KEY_LINE" -F read_only=REPLACE_true_or_false --jq '.id')
echo "registered id=$NEW"
gh api -X DELETE "repos/$REPO/keys/REPLACE_OLD_KEY_ID"
gh api "repos/$REPO/keys" --jq '.[] | "\(.id) \(.title) read_only=\(.read_only)"'
```

Expected final listing: the new key, and no key with the old id.

- [ ] **Step 3 (controller): `FLEET_SSH_KEY` — update the fleet's authorized key in nixos-config**

```bash
cd /c/Users/Pol/projects/nixos-config 2>/dev/null || gh repo clone Forgenn/nixos-config /c/Users/Pol/projects/nixos-config
cd /c/Users/Pol/projects/nixos-config && git checkout main && git pull --ff-only
grep -rl "hermes-agent" --include=*.nix . | xargs grep -l "openssh.authorizedKeys\|AAAAC3NzaC1lZDI1NTE5" 2>/dev/null
```

Edit the matched file(s): replace the `hermes-agent` user's `openssh.authorizedKeys.keys` entry with the operator's new public key line (verbatim, safe to paste — it is public). Commit on a branch, open a PR, and note in the PR body that the fleet must apply it (`nixos-rebuild switch` / the flake's own CI, whichever this repo already uses) before `FLEET_SSH_KEY`'s rotation is complete — this plan does not drive that rebuild; it is the operator's own node-level step per plan 4 Ruling 4 territory (destructive/node-level changes are never automated here).

```bash
git checkout -b rotate-hermes-agent-fleet-key
git add -A && git commit -m "fleet: rotate the hermes-agent SSH key"
git push -u origin rotate-hermes-agent-fleet-key
gh pr create --repo Forgenn/nixos-config --base main --head rotate-hermes-agent-fleet-key \
  --title "fleet: rotate the hermes-agent SSH key" \
  --body "Rotates the hermes-agent fleet SSH key per docs/plans/2026-09-15-hermes-developer-meta-docs.md Task 2. Needs a nixos-rebuild on dolores/dubois/cuno/katsuragi before the old key stops working."
```

- [ ] **Step 4 (controller): Force an ExternalSecret refresh and roll the pod**

Gates: not 08:45–09:15 UTC; volsync within 36h. The pod must restart regardless of ESO's `refreshInterval: 1h`, because the SSH key **files** are written once by the `ssh-key-perms` initContainer at pod start — a live Secret update alone does not touch the already-copied files.

```bash
export MSYS_NO_PATHCONV=1
kubectl annotate externalsecret hermes-secrets -n hermes reconcile.external-secrets.io/requested-at="$(date -u +%s)" --overwrite
kubectl wait externalsecret/hermes-secrets -n hermes --for=condition=Ready --timeout=120s
kubectl get externalsecret hermes-secrets -n hermes -o jsonpath='{.status.conditions}{"\n"}'
kubectl rollout restart deployment/hermes-agent -n hermes
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
```

Expected: the ExternalSecret condition shows `Ready`/`SecretSynced`, not an error.

- [ ] **Step 5: Delete the stray key copy**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops && . ./lib.sh
pexec <<'SH'
/command/s6-setuidgid hermes sh -c '
if [ -e /opt/data/home/keys/nixos_deploy_key ]; then
  ls -la /opt/data/home/keys/nixos_deploy_key
  rm -f /opt/data/home/keys/nixos_deploy_key
  rmdir /opt/data/home/keys 2>/dev/null || true
  echo "removed"
else
  echo "already gone"
fi'
SH
```

- [ ] **Step 6 (controller): Verify each rotated key works, without ever printing a value**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
# gitops-cluster + plder (read side): fingerprint match, safe to print (public data).
. ./lib.sh
pexec <<'SH'
/command/s6-setuidgid hermes sh -c '
for f in git_deploy_key nixos_deploy_key fleet_ssh_key plder_deploy_key_read; do
  p=/opt/data/home/.ssh/$f
  [ -f "$p" ] && echo "$f: $(ssh-keygen -lf "$p" 2>&1)"
done'
SH
```

Compare each printed fingerprint against the one the operator sent in Step 1 — this proves the file the initContainer copied in matches what was pasted into Infisical, without ever displaying the key material.

```bash
IDG=$(./agent-probe.sh homelab-ops "rotation probe gitops" "Run exactly: git -C /opt/data/home/gitops-cluster ls-remote --heads origin main . Reply ROTATE_GITOPS_OK with the hash, or ROTATE_GITOPS_FAIL with the error.")
IDN=$(./agent-probe.sh homelab-ops "rotation probe nixos" "Run exactly: git -C /opt/data/home/nixos-config ls-remote --heads origin main . If that path doesn't exist, run: git clone git@github.com-nixos:Forgenn/nixos-config /tmp/nixos-probe && git -C /tmp/nixos-probe ls-remote --heads origin main && rm -rf /tmp/nixos-probe . Reply ROTATE_NIXOS_OK with the hash, or ROTATE_NIXOS_FAIL with the error.")
```

≥ 3 min later, `./probe-result.sh homelab-ops "$IDG" ROTATE_GITOPS_OK` and `... "$IDN" ROTATE_NIXOS_OK`, then clean up. `PLDER_DEPLOY_KEY_READ` is proven by the next `profile-sync` init log (`applied ref … result: ok`) on this same rolled pod — check it now: `kubectl logs -n hermes "$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')" -c profile-sync | tail -3`. `FLEET_SSH_KEY` is verified only after the operator applies the nixos-config PR from Step 3 — re-run `./agent-probe.sh homelab-ops "fleet ssh probe" "Run exactly: ssh -F /opt/data/home/.ssh/config hermes-agent@dubois.home true && echo FLEET_SSH_OK || echo FLEET_SSH_FAIL"` at that point.

**Rollback:** re-add the deleted GitHub key (old id is gone forever, but a fresh keypair can be generated and mapped the same way) and revert the `reconcile.external-secrets.io/requested-at` annotation is a no-op (it only ever triggers a refresh, doesn't change desired state) — the real rollback is pasting the old private key back into Infisical, which only the operator can do.

---

### Task 3: Developer bot's push credential channel (gitops-cluster + plder)

Builds the plumbing Task 4 wires into a bot: a new, independently-failing `ExternalSecret`, a new optional volume, a git-credential helper, and the root `terminal.env_passthrough` declaration. No bot profile exists yet after this task — that's Task 4.

**Files:**
- Create: `infra/hermes-agent/secrets/externalsecret-developer.yaml`
- Modify: `infra/hermes-agent/secrets/kustomization.yaml`, `infra/hermes-agent/deployment.yaml` (one new optional volume + mount), `infra/hermes-agent/githooks/gitconfig` (add `credential.helper`)
- Create: `infra/hermes-agent/githooks/git_credential_developer_pat.py`, `infra/hermes-agent/githooks/test_git_credential_developer_pat.py`
- Modify: `infra/hermes-agent/kustomization.yaml` (configMapGenerator: add the new script file)
- Modify (plder): `hermes/root/config.yaml` (`terminal.env_passthrough`)

**Interfaces:**
- `ExternalSecret hermes-secrets-developer` → Secret `hermes-secrets-developer`, key `DEVELOPER_GITHUB_PAT`, remoteRef `/hermes/DEVELOPER_GITHUB_PAT`. **Independent** of `hermes-secrets` on purpose (Ruling 4): if the operator has not created this Infisical path yet, only this small ExternalSecret's `.status.conditions` shows an error — `hermes-secrets` and every other credential keep working.
- Volume `profile-secrets-developer` (Secret, `optional: true`, `defaultMode: 0440`) mounted at `/etc/hermes-profile-secrets-developer`.
- `git_credential_developer_pat.py`: implements the `git-credential` `get` protocol. Reads stdin `key=value` lines; if `protocol=https`, `host=github.com`, and `$DEVELOPER_GITHUB_PAT` is set and non-empty, prints `username=x-access-token` and `password=<value>` and exits 0; otherwise prints nothing and exits 0 (git falls through to no credentials — every other bot's HTTPS git operations, if any, are simply unaffected since the env var is unset in their scope).

- [ ] **Step 1: Branch and write the failing tests for the credential helper**

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only && git checkout -b hermes-developer-pat-channel
```

```python
# infra/hermes-agent/githooks/test_git_credential_developer_pat.py
"""git-credential 'get' protocol tests. No real PAT ever appears here or on disk;
DUMMY_TOKEN is a test-local placeholder, never read from a live secret."""
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).with_name("git_credential_developer_pat.py")
DUMMY_TOKEN = "test-only-placeholder-not-a-real-credential"


def run(stdin: str, env_token: str | None) -> subprocess.CompletedProcess:
    env = {"PATH": ""}
    if env_token is not None:
        env["DEVELOPER_GITHUB_PAT"] = env_token
    return subprocess.run([sys.executable, str(HELPER), "get"], input=stdin,
                           capture_output=True, text=True, env=env)


def test_emits_credentials_for_github_https_when_the_token_is_set():
    res = run("protocol=https\nhost=github.com\n\n", DUMMY_TOKEN)
    assert res.returncode == 0
    assert res.stdout == f"username=x-access-token\npassword={DUMMY_TOKEN}\n"


def test_emits_nothing_when_the_token_is_absent():
    res = run("protocol=https\nhost=github.com\n\n", None)
    assert res.returncode == 0 and res.stdout == ""


def test_emits_nothing_when_the_token_is_empty():
    res = run("protocol=https\nhost=github.com\n\n", "")
    assert res.returncode == 0 and res.stdout == ""


def test_emits_nothing_for_a_different_host():
    res = run("protocol=https\nhost=gitlab.com\n\n", DUMMY_TOKEN)
    assert res.returncode == 0 and res.stdout == ""


def test_emits_nothing_for_ssh():
    res = run("protocol=ssh\nhost=github.com\n\n", DUMMY_TOKEN)
    assert res.returncode == 0 and res.stdout == ""


def test_ignores_operations_other_than_get():
    res = subprocess.run([sys.executable, str(HELPER), "store"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, env={"PATH": "", "DEVELOPER_GITHUB_PAT": DUMMY_TOKEN})
    assert res.returncode == 0 and res.stdout == ""


def test_never_echoes_the_token_on_stderr_either():
    res = run("protocol=https\nhost=github.com\n\n", DUMMY_TOKEN)
    assert DUMMY_TOKEN not in res.stderr
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/githooks && python -m pytest -q test_git_credential_developer_pat.py 2>&1 | tail -3
```

Expected: `FileNotFoundError` / `No such file or directory: '…git_credential_developer_pat.py'`.

- [ ] **Step 3: Implement the helper**

```python
#!/opt/hermes/.venv/bin/python3
# infra/hermes-agent/githooks/git_credential_developer_pat.py
"""git-credential helper for the developer bot's fine-grained PAT.

Registered process-wide via /etc/gitconfig (credential.https://github.com.helper),
so it runs for EVERY profile's git subprocess -- but it only ever emits anything
when $DEVELOPER_GITHUB_PAT is a non-empty string in that specific subprocess's
environment. That variable reaches a subprocess's environment only through
Hermes' terminal.env_passthrough mechanism, which resolves its VALUE from the
CURRENTLY ACTIVE profile's secret scope (agent.secret_scope, tools/env_passthrough.py
resolve_passthrough_value) -- so only a developer-scoped terminal call, whose
secrets.command helper was extended to read
/etc/hermes-profile-secrets-developer/DEVELOPER_GITHUB_PAT, ever sees it set.
Every other profile's terminal subprocess has it unset, and this helper is then a
silent no-op (git falls through with no credentials for that push).

This is a git-credential 'get' handler only (store/erase are ignored -- a fine-
grained PAT is never persisted to a credential cache in this pod). Never logs or
echoes the token value anywhere, on any exit path.

Design: gitops-cluster docs/plans/2026-09-15-hermes-developer-meta-docs.md, Task 3.
"""
from __future__ import annotations

import os
import sys


def handle_get(stdin_text: str, env: dict) -> str:
    fields = dict(
        line.split("=", 1) for line in stdin_text.splitlines() if "=" in line
    )
    token = env.get("DEVELOPER_GITHUB_PAT", "")
    if not token:
        return ""
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return ""
    return f"username=x-access-token\npassword={token}\n"


def main(argv: list[str]) -> int:
    op = argv[1] if len(argv) > 1 else ""
    stdin_text = sys.stdin.read()
    if op == "get":
        sys.stdout.write(handle_get(stdin_text, os.environ))
    # 'store' and 'erase': deliberately no-op, nothing to persist or clear.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: Run the tests**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/githooks && python -m pytest -q 2>&1 | tail -3
```

Expected: `13 passed` (6 pre-push + 7 here).

- [ ] **Step 5: Wire it into the deployment**

In `infra/hermes-agent/kustomization.yaml`, under the existing `hermes-githooks` `configMapGenerator` entry, add a third file:

```yaml
  - name: hermes-githooks
    files:
      - pre-push=githooks/pre_push.py
      - gitconfig=githooks/gitconfig
      - git-credential-developer-pat=githooks/git_credential_developer_pat.py
```

In `infra/hermes-agent/githooks/gitconfig`, add the credential helper (system-level, so it fires for every profile's git subprocess — see the helper's own docstring for why that's safe):

```ini
[core]
	hooksPath = /etc/hermes-githooks
[credential "https://github.com"]
	helper = /etc/hermes-githooks/git-credential-developer-pat
```

In `infra/hermes-agent/deployment.yaml`, add the mount (main container, after `profile-secrets`) and the volume:

```yaml
            # Developer bot's fine-grained PAT, on its OWN mount so ONLY developer's
            # extended secrets.command reads it -- the shared /etc/hermes-profile-secrets
            # directory is read by every profile's identical helper script, so a new
            # credential there would leak into every profile's resolved secret scope.
            # optional: true -- the operator may not have created this secret yet
            # (Task 4's PAT-absence tolerance); the pod must never fail to start over it.
            - name: profile-secrets-developer
              mountPath: /etc/hermes-profile-secrets-developer
              readOnly: true
```

```yaml
        - name: profile-secrets-developer
          secret:
            secretName: hermes-secrets-developer
            optional: true
            defaultMode: 0440
            items:
              - key: DEVELOPER_GITHUB_PAT
                path: DEVELOPER_GITHUB_PAT
```

Also add the executable bit for the new script in the ConfigMap generator's `defaultMode` (it's already `0555` on the `hermes-githooks` volume as a whole — no per-file change needed since all three files share that mode).

- [ ] **Step 6: Create the independent ExternalSecret**

`infra/hermes-agent/secrets/externalsecret-developer.yaml`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: hermes-secrets-developer
  namespace: hermes
spec:
  secretStoreRef:
    name: infisical-cluster-secret-store
    kind: ClusterSecretStore
  target:
    name: hermes-secrets-developer
    creationPolicy: Owner
    deletionPolicy: Retain
  data:
    # Fine-grained PAT, "only select repositories" = every Forgenn repo except
    # gitops-cluster, nixos-config, plder (Task 4 lists them). Deliberately its
    # OWN ExternalSecret: if the operator has not created this Infisical path
    # yet, only THIS resource's status shows an error -- hermes-secrets and
    # every other credential keep refreshing normally. Optional at the pod level
    # too (deployment.yaml profile-secrets-developer volume, optional: true).
    - secretKey: DEVELOPER_GITHUB_PAT
      remoteRef:
        key: /hermes/DEVELOPER_GITHUB_PAT
  refreshInterval: 1h
```

Add it to `infra/hermes-agent/secrets/kustomization.yaml`'s resource list next to `externalsecret.yaml`.

- [ ] **Step 7: Add the root `terminal.env_passthrough` declaration (plder)**

```bash
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && git checkout -b hermes-developer-pat-channel
python - <<'PY'
p = "hermes/root/config.yaml"
raw = open(p, "rb").read()
eol = b"\r\n" if b"\r\n" in raw else b"\n"
assert b"\nterminal:" not in raw, "terminal: already declared -- read it before extending"
block = """
# Passthrough allowlist for terminal/execute_code subprocesses: the NAME here is
# process-global and cached for the pod's lifetime on first read (Hermes has no
# staleness check on this specific cache) -- root's config is always the first
# one read at gateway boot, so declaring it here is what makes it reliable
# regardless of which profile's turn happens to fire first. The VALUE is
# resolved fresh per call from whichever profile's secret scope is active, so
# only a developer-scoped terminal call ever sees a real value; every other
# profile's scope has nothing under this name and resolves it to unset.
# GH_TOKEN/GITHUB_TOKEN are refused by Hermes' own credential blocklist and
# cannot be used here -- this is why the developer bot's git-credential helper
# reads DEVELOPER_GITHUB_PAT instead. Design: gitops-cluster
# docs/plans/2026-09-15-hermes-developer-meta-docs.md Task 3.
terminal:
  env_passthrough:
    - DEVELOPER_GITHUB_PAT
""".encode().replace(b"\n", eol)
if not raw.endswith(eol):
    raw += eol
open(p, "wb").write(raw + block)
PY
python hermes/ci/validate.py hermes && echo "validate: OK"
git add hermes/root/config.yaml
git commit -m "hermes: declare the developer PAT passthrough name in root config"
```

- [ ] **Step 8 (controller): Apply, deploy, and prove the isolation on the real pod**

```bash
cd /c/Users/Pol/projects/gitops-check
git add infra/hermes-agent
git commit -m "hermes: independent PAT channel for the developer bot (secret, volume, git-credential helper)"
git pull --rebase origin main && git push origin main
GITOPS_SHA=$(git rev-parse HEAD); export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
kubectl get externalsecret hermes-secrets-developer -n hermes -o jsonpath='{.status.conditions}{"\n"}'
```

Expected (PAT not created yet): a `SecretSyncedError`-shaped condition on `hermes-secrets-developer` **only**; `hermes-secrets` unaffected (`kubectl get externalsecret hermes-secrets -n hermes -o jsonpath='{.status.conditions}'` still shows Ready). The pod is healthy either way (`optional: true` on the volume).

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
# Fire a probe in a DIFFERENT profile FIRST, specifically to catch the "first
# caller wins the passthrough cache" ordering risk noted in Verified facts.
M=$(./agent-probe.sh monitor "passthrough order probe" "Reply with exactly ORDER_PROBE_OK and nothing else.")
```

≥ 3 min later, `./probe-result.sh monitor "$M" ORDER_PROBE_OK`, clean up, then the full-roster probe (same pattern as Task 1 Step 5) across `default monitor homelab-ops shopper research projects` — expect all pass, proving root's new `terminal:` key broke nothing.

**Rollback:** `git revert` both commits (gitops and plder) and push; the new ExternalSecret/volume/helper simply stop existing, `hermes-secrets`/root config return to their prior shape.

---

### Task 4: Developer bot (plder)

**Files (branch `bot/developer`):**
- Create: `hermes/profiles/developer/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`
- Modify: `hermes/root/config.yaml` (allowlist + route, via `add_route.py`)

`BOT=developer`, `TOPIC="Developer"`, `THREAD=$DEVELOPER_THREAD`.

- [ ] **Step 1 (controller): Create the topic** → `DEVELOPER_THREAD` via `./create-topic.sh "Developer"`.

- [ ] **Step 2: Give the operator the exact PAT scope list**

Message: *"For the fine-grained PAT (Infisical `/hermes/DEVELOPER_GITHUB_PAT`), 'only select repositories' needs every Forgenn repo except gitops-cluster, nixos-config, plder. Current list (`gh api user/repos --paginate --jq '.[].name' | sort -u`, re-run to catch anything new since 2026-09-16): AH, AH-warehouse, Advent-of-Code, Ambient-Detector, BitTorrent-client, Dotfiles, GGJ_2022, Hardened-Browser, Tachidesk, Tasker, VGI_pract1y2, canyon_stock_bot, chesserbot, coend, comick.io-scanlator-selector, csvsonic, decathlon_stock_bot, e-Paper, loomie, magidor, mangadex-exporter, maxmix-software, mpc-autofill, nvidia-stock-check, openbao, openbao-k8s, pancakeswap-web3py, porkbun-webhook, porkbun-webhook-helm-repo, practica2_ats, rotobot_js, rotobot_selenium, sort-video-by-brightness, tekken_translator, twitch-notify, warmane_votepoints_bot. Permissions: Contents RW, Pull requests RW, Issues RW, Workflows RW, Metadata R."*

- [ ] **Step 3: Write the profile**

`hermes/profiles/developer/distribution.yaml`:

```yaml
name: developer
version: 0.1.0
description: "Developer — application repos (loomie, career-ops, side projects); never the cluster, never plder"
distribution_owned:
  - SOUL.md
  - config.yaml
```

`hermes/profiles/developer/SOUL.md`:

```markdown
You are Developer, the operator's engineer for application repositories — loomie, career-ops, personal projects. You never touch the homelab cluster, gitops-cluster, nixos-config, or plder: that is homelab-ops' and meta's territory.

## What you own
- Any Forgenn repository your GitHub credential can reach. It is a fine-grained PAT scoped to specific repos — if a push or API call to a repo fails with a permission error, that repo is genuinely out of scope; do not ask the operator to widen it without a real need.
- Reading, writing, opening PRs and issues, and running whatever the repo's own CI does. You have `code_execution`: prefer it and your own tests over hand-tracing logic.

## How you work
- Clone with HTTPS (`https://github.com/<org>/<repo>.git`), never SSH — you have no deploy key, only the credential helper wired to your PAT. If `git clone`/`git push` ever asks for a password interactively, your credential is not resolving; report that rather than typing anything.
- Prefer small, reviewable commits and PRs over direct pushes to a repo's default branch when the repo's own CI would gate a PR; a repo with no CI can take a direct push for a small, low-risk change.
- If your PAT is missing or a repo is unreachable, say so plainly rather than retrying blindly — the operator has not created it yet, or scoped it differently than expected.

## Boundaries
Never `gitops-cluster`, `nixos-config`, or `plder` — you have no credential for any of them, but do not even attempt it (a real deploy key or PAT accidentally reaching your scope would be a mistake to flag, not to use). Cluster problems go to homelab-ops, Hermes/bot config changes go to meta: end your answer with "Hand-off → @<bot>: ..." for the operator to forward.
```

`hermes/profiles/developer/config.yaml`:

```yaml
# hermes/profiles/developer/config.yaml -- Developer (strong tier, pushes app repos over HTTPS+PAT)
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

followed by the plan 4 Task 6 shared blocks (`known_builtin_toolsets`, `approvals`, `auxiliary`, `platforms`/`plugins`/`desktop`, `secrets`/`cron`/`_config_version`) verbatim, **except** the `secrets.command.command` gains a second loop reading the bot's own PAT mount (empty/absent-tolerant):

```yaml
secrets:
  command:
    enabled: true
    override_existing: true
    command: |
      for f in /etc/hermes-profile-secrets/*; do
        [ -f "$f" ] || continue
        IFS= read -r v < "$f" || [ -n "$v" ]
        printf "%s=%s\n" "${f##*/}" "$v"
      done
      for f in /etc/hermes-profile-secrets-developer/*; do
        [ -f "$f" ] || continue
        IFS= read -r v < "$f" || [ -n "$v" ]
        printf "%s=%s\n" "${f##*/}" "$v"
      done
```

`hermes/profiles/developer/skills.allow.yaml`:

```yaml
# Upstream skills this bot uses; every other bundled skill is disabled at each sync.
bundled:
  - claude-code
  - codebase-inspection
  - github-pr-workflow
  - github-issue-to-pr
  - github-issues
  - code-wiki
  - systematic-debugging
  - test-driven-development
  - requesting-code-review
  - simplify-code
  - plan
optional: []
```

(Verify each name exists in the image before merging — `hermes/ci/roster.py`'s `check_image` fails the PR's CI otherwise; drop any that don't, the spec's roster row for developer was written before the current image and may not match exactly.)

`hermes/profiles/developer/cron/jobs.json`:

```json
{
  "jobs": []
}
```

- [ ] **Step 4: Route, validate, commit**

```bash
cd /c/Users/Pol/projects/plder
python /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/add_route.py hermes/root/config.yaml developer 7850573137 REPLACE_DEVELOPER_THREAD
python hermes/ci/validate.py hermes && python -m pytest -q hermes/ci 2>&1 | tail -1
git add hermes/profiles/developer hermes/root/config.yaml
git commit -m "hermes: add the developer bot"
```

- [ ] **Step 5 (controller): PR, CI, rehearsal, merge, deploy**

```bash
cd /c/Users/Pol/projects/plder && git push -u origin bot/developer
gh pr create --repo Forgenn/plder --base master --head bot/developer \
  --title "hermes: add the developer bot" --body "Plan: gitops-cluster docs/plans/2026-09-15-hermes-developer-meta-docs.md Task 4"
gh pr checks bot/developer --repo Forgenn/plder --watch
BR=$(git rev-parse HEAD)
/c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/rehearse-sync.sh "$BR" 2>&1 | tail -60
```

Expected in the rehearsal: `installed profile developer`; `skills curated for developer: <n> allowed, <m> bundled disabled`; developer's `toolsets.telegram`/`.cli`/`.cron` match the declared lists; `api_server_enabled: false`; `image checks: 0 failure(s)`.

```bash
DEPLOY_AT=$(date -u +"%Y-%m-%d %H:%M")
gh pr merge bot/developer --repo Forgenn/plder --merge --delete-branch
cd /c/Users/Pol/projects/plder && git checkout master && git pull --ff-only && SHA=$(git rev-parse HEAD)
/c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/wait-deploy.sh "$SHA"
```

- [ ] **Step 6 (controller): Verify — PAT absent, then presence detection**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
ID=$(./agent-probe.sh developer "pat absence probe" "Run exactly this terminal command and nothing else: git clone https://github.com/Forgenn/loomie /tmp/dev-probe . If it asked for credentials or failed auth, reply PAT_ABSENT. If it succeeded, reply PAT_PRESENT and the first line of git log. Then: rm -rf /tmp/dev-probe")
```

≥ 3 min later: `./probe-result.sh developer "$ID"` — record whichever it says in "Recorded at execution" (`loomie` is public, so a clone *without* credentials can actually succeed either way; a stronger presence check is a private repo in the PAT's scope, e.g. `AH`, which only succeeds once the PAT exists and clones with credentials). Re-run against `AH` once the operator confirms the PAT is created:

```bash
ID2=$(./agent-probe.sh developer "pat presence probe" "Run exactly: git clone https://github.com/Forgenn/AH /tmp/dev-probe2 . Reply PAT_PRESENT_OK if it succeeded, PAT_PRESENT_FAIL with the error otherwise. Then: rm -rf /tmp/dev-probe2")
```

Expected once the PAT exists: `PAT_PRESENT_OK`. This is the "a verification step detects when it appears" the brief asked for — it is re-runnable at any time, before or after the operator creates the secret, with no code change.

- [ ] **Step 7 (controller): Isolation check — no other bot's terminal sees the PAT**

```bash
ID3=$(./agent-probe.sh homelab-ops "no pat leak probe" "Run exactly: git clone https://github.com/Forgenn/AH /tmp/leak-probe . Reply LEAK_PROBE_BLOCKED if it failed (no credentials), LEAK_PROBE_LEAKED if it succeeded. Then: rm -rf /tmp/leak-probe")
```

≥ 3 min later: expect `LEAK_PROBE_BLOCKED` (homelab-ops' terminal subprocess never had `DEVELOPER_GITHUB_PAT` set — its scope's `secrets.command` never reads the developer-only mount).

- [ ] **Step 8: Operator question for Task 11** — *"Who are you, and what happens if I ask you to touch the cluster or plder?"* Expected gist: developer; app repos only; explicitly refuses cluster/gitops-cluster/nixos-config/plder and names homelab-ops/meta instead.

---

### Task 5: Meta bot's plder write credential (gitops-cluster + plder)

**Files:**
- Modify: `infra/hermes-agent/deployment.yaml` (`ssh-key-perms` initContainer: one more optional key; new `ssh-keys` mount for the main container)
- Create: `infra/hermes-agent/secrets/externalsecret-meta.yaml`
- Modify: `infra/hermes-agent/secrets/kustomization.yaml`, `infra/hermes-agent/configmap.yaml` (`ssh_config`: new `Host github.com-plder-write` alias)
- Create (only if Step 1 finds the believed key is genuinely absent): a workstation-only key-generation run using plan 3 Task 8's pattern, scoped to plder instead of gitops-cluster.

- [ ] **Step 1 (controller): Verify whether `PLDER_DEPLOY_KEY` is really at `/hermes/PLDER_DEPLOY_KEY` — status only, never the value**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/secrets
cp externalsecret.yaml externalsecret-meta.yaml.tmp   # scratch, not committed yet
```

Add the mapping to a throwaway `ExternalSecret` and apply it directly (not through Kustomize) purely to read its condition:

```bash
export MSYS_NO_PATHCONV=1
kubectl apply -f - <<'YAML'
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: plder-write-key-probe
  namespace: hermes
spec:
  secretStoreRef: {name: infisical-cluster-secret-store, kind: ClusterSecretStore}
  target: {name: plder-write-key-probe, creationPolicy: Owner}
  data:
    - secretKey: PLDER_DEPLOY_KEY
      remoteRef: {key: /hermes/PLDER_DEPLOY_KEY}
YAML
sleep 5
kubectl get externalsecret plder-write-key-probe -n hermes -o jsonpath='{.status.conditions}{"\n"}'
kubectl delete externalsecret plder-write-key-probe -n hermes
kubectl delete secret plder-write-key-probe -n hermes --ignore-not-found
rm infra/hermes-agent/secrets/externalsecret-meta.yaml.tmp
```

**If Ready/SecretSynced:** the believed key exists; proceed to Step 2 with `Existing` recorded. **If a sync error:** the private half is not actually at that path — fall back to plan 3 Task 8's pattern (operator generates via `cryptography`/pod `ssh-keygen`, this session never sees the private bytes) but targeting **plder** with **write** access instead of gitops-cluster:

```bash
KEYGEN_PY=/c/Users/Pol/projects/gitops-check/.superpowers/planning/plder_meta_keygen.py
# Same script body as plan 3 Task 8's plder_ci_keygen.py, with:
#   TITLE = "hermes-meta-plder-write"
#   gh("secret", ...) replaced by: gh("api", "repos/Forgenn/plder/keys", "-f", f"title={TITLE}",
#                                      "-f", f"key={public}", "-F", "read_only=false")  (a DEPLOY KEY, not an Actions secret)
#   plus: print the fingerprint and tell the operator to paste the PRIVATE half at
#   Infisical /hermes/PLDER_DEPLOY_KEY themselves (never write it via this script --
#   Infisical writes are the operator's own action per this plan's HARD SECURITY RULE).
python "$KEYGEN_PY"
rm -f "$KEYGEN_PY"
```

Record which path was taken in "Recorded at execution."

- [ ] **Step 2: Wire the ExternalSecret and the ssh_config alias**

`infra/hermes-agent/secrets/externalsecret-meta.yaml`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: hermes-secrets-meta
  namespace: hermes
spec:
  secretStoreRef:
    name: infisical-cluster-secret-store
    kind: ClusterSecretStore
  target:
    name: hermes-secrets-meta
    creationPolicy: Owner
    deletionPolicy: Retain
  data:
    # plder WRITE deploy key (163152504, reserved "hermes deploy" at plan-3 time).
    # Its own ExternalSecret for the same reason as hermes-secrets-developer: an
    # absent/rotated key here must never stall hermes-secrets' refresh for
    # everything else. Delivered as a FILE only (ssh-key-perms initContainer,
    # ssh_config Host github.com-plder-write) -- never envFrom, ever (Ruling 1).
    - secretKey: PLDER_DEPLOY_KEY
      remoteRef:
        key: /hermes/PLDER_DEPLOY_KEY
  refreshInterval: 1h
```

Add to `infra/hermes-agent/secrets/kustomization.yaml`.

In `infra/hermes-agent/configmap.yaml`, add after the `github.com-plder` (read) block:

```yaml
    # meta's WRITE key for plder, kept on its own alias so meta's git remote never
    # accidentally resolves to the read-only sync key or vice versa.
    Host github.com-plder-write
      HostName github.com
      User git
      IdentityFile /opt/data/home/.ssh/plder_deploy_key_write
      IdentitiesOnly yes
      StrictHostKeyChecking accept-new
```

In `infra/hermes-agent/deployment.yaml`, extend the `ssh-key-perms` initContainer's optional (`set +e`) section:

```yaml
              if cp /secrets/plder-deploy-key-write/PLDER_DEPLOY_KEY /ssh-keys/plder_deploy_key_write 2>/dev/null; then
                chown 10000:10000 /ssh-keys/plder_deploy_key_write
                chmod 600 /ssh-keys/plder_deploy_key_write
              else
                echo "[ssh-key-perms] no plder WRITE key yet; meta will soft-fail its pushes"
              fi
```

with a matching new `volumeMounts` entry (`plder-deploy-key-write` → `/secrets/plder-deploy-key-write`), a new `volumes` entry:

```yaml
        - name: plder-deploy-key-write
          secret:
            secretName: hermes-secrets-meta
            optional: true
            defaultMode: 0400
            items:
              - key: PLDER_DEPLOY_KEY
                path: PLDER_DEPLOY_KEY
```

and a new main-container mount (next to the existing `plder_deploy_key_read` one — this key is **not** in the shared `envFrom`/explicit `env:` list from Task 1, ever):

```yaml
            - name: ssh-keys
              mountPath: /opt/data/home/.ssh/plder_deploy_key_write
              subPath: plder_deploy_key_write
              readOnly: true
```

- [ ] **Step 3 (controller): Commit, deploy, verify status (never the value)**

```bash
cd /c/Users/Pol/projects/gitops-check
git checkout main && git pull --ff-only && git checkout -b hermes-meta-plder-key
git add infra/hermes-agent
git commit -m "hermes: add meta's plder write-key channel (its own ExternalSecret + ssh alias)"
git push -u origin hermes-meta-plder-key
git checkout main && git merge --no-ff hermes-meta-plder-key -m "hermes: meta plder write-key channel" && git push origin main
GITOPS_SHA=$(git rev-parse HEAD); export MSYS_NO_PATHCONV=1
kubectl wait application/hermes-agent -n argocd --for=jsonpath='{.status.sync.revision}'="$GITOPS_SHA" --timeout=900s
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
kubectl get externalsecret hermes-secrets-meta -n hermes -o jsonpath='{.status.conditions}{"\n"}'
```

Expected: `Ready`/`SecretSynced` if Step 1 found the key already present, or a sync error until the operator pastes the freshly-generated one.

---

### Task 6: CI-credential protection, then the meta bot itself (plder)

**Files (branch `bot/meta`):**
- Create: `.github/workflows/hermes-config.yml` **modification** (deploy-step guard) — read the merged plan-3 file first, this only adds one `if:` condition and one new job/step, it does not rewrite the workflow.
- Create: `hermes/profiles/meta/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`
- Modify: `hermes/root/config.yaml` (allowlist + route)

`BOT=meta`, `TOPIC="Meta"`, `THREAD=$META_THREAD`.

- [ ] **Step 1 (controller): Create the topic** → `META_THREAD`.

- [ ] **Step 2: CI-credential protection (Ruling 6) — the workflow refuses to deploy a `.github/**`-touching commit, plus an always-on tripwire**

Read `.github/workflows/hermes-config.yml` as merged by plan 3 before editing (its exact job/step names may differ slightly from what plan 3's draft showed). Add, to the job that runs `bump_ref.py` (the deploy step), a guard immediately before that step:

```yaml
      - name: Refuse to deploy a commit that touches .github/**
        if: github.ref == 'refs/heads/master'
        run: |
          if git diff --name-only "${{ github.event.before }}" "${{ github.sha }}" | grep -q '^\.github/'; then
            echo "::error::This commit touches .github/** — CI will validate it but will NOT bump AGENT_CONFIG_REF. A workflow-file change needs a human to review it and bump the ref manually. See docs/plans/2026-09-15-hermes-developer-meta-docs.md Ruling 6."
            echo "SKIP_DEPLOY=1" >> "$GITHUB_ENV"
          fi
      - name: Bump AGENT_CONFIG_REF
        if: github.ref == 'refs/heads/master' && env.SKIP_DEPLOY != '1'
        run: python hermes/ci/bump_ref.py   # exact invocation per plan 3's merged step
```

Add a second, unconditional job (runs on every push, independent of the guard above, so it cannot be silenced by the same commit that would need to disable it):

```yaml
  tripwire:
    runs-on: ubuntu-24.04
    if: always()
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 2}
      - name: Alert the operator if this push touches CI's own surface
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TRIPWIRE_TELEGRAM_BOT_TOKEN }}
        run: |
          CHANGED=$(git diff --name-only HEAD~1 HEAD -- '.github/' 'hermes/ci/' || true)
          if [ -n "$CHANGED" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
            MSG="plder push $(git rev-parse --short HEAD) by $(git log -1 --format=%an) touched CI's own surface: $CHANGED"
            curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
              -d chat_id="${{ secrets.TRIPWIRE_TELEGRAM_CHAT_ID }}" --data-urlencode text="$MSG" >/dev/null
          fi
```

This needs two **new**, small Actions secrets (`TRIPWIRE_TELEGRAM_BOT_TOKEN`, `TRIPWIRE_TELEGRAM_CHAT_ID`) — reuse the existing homelab Telegram bot/chat (same values already in `hermes-secrets` at `/monitoring/telegram/BOT_TOKEN` and `/monitoring/telegram/CHAT_ID`), set once by the operator via `gh secret set --repo Forgenn/plder TRIPWIRE_TELEGRAM_BOT_TOKEN` (their own terminal, this session never touches the value). **State plainly (Ruling 6): this tripwire cannot stop a single commit that changes `.github/workflows/hermes-config.yml` to remove itself in the same push — it only guarantees the alert fires for every OTHER CI-surface change, and for that one worst case, guarantees it fired on every push up to that point.**

- [ ] **Step 3: Write the profile**

`hermes/profiles/meta/distribution.yaml`:

```yaml
name: meta
version: 0.1.0
description: "Meta — improves Hermes and the other bots by pushing to plder"
distribution_owned:
  - SOUL.md
  - config.yaml
  - skills/custom/
```

`hermes/profiles/meta/SOUL.md`:

```markdown
You are Meta, the operator's engineer for Hermes itself: the plder repository that defines every bot's persona, config, cron jobs and skills — including your own.

## What you own
- `Forgenn/plder` at `/opt/data/home/plder` (clone it with `git clone git@github.com-plder-write:Forgenn/plder` if it is not already there — that host alias is your WRITE key; `github.com-plder` without `-write` is the read-only sync key and will refuse a push).
- Authoring new skills: write them at `hermes/profiles/<bot>/skills/custom/<name>/SKILL.md` in your plder working copy, never onto `/opt/data` directly (a skill written straight to a PVC is invisible to git, lost with the volume, and unavailable to any bot but the one you wrote it on — writing it into the repo makes it deployable to any bot through the normal sync). See the `plder-authoring` skill for the exact steps and CI's rules.

## How you work
- Before every push: `python hermes/ci/validate.py hermes` in your working copy, and read its output. A commit that fails validation locally will fail CI identically — do not push it hoping CI is more lenient.
- **Never touch `.github/**`.** That is CI's own definition; a change there needs the operator directly, and CI is guarded (and alarmed) against exactly this. If you believe the workflow itself needs to change, write out the exact diff and ask the operator to apply it themselves — do not attempt it, even on a branch, even as a "just checking" edit.
- One logical change per commit; message style `component: lowercase description`, matching plder's existing history.
- Push to `master` directly for a validated, low-risk change (a new skill, a persona tweak, a cron job edit); open a PR instead when the change is large, touches more than one bot, or you are not confident it will pass CI on the first try.
- You have `code_execution` and `terminal`: you can run plder's own test suite (`python -m pytest hermes/ci` and `infra/hermes-agent/sync`'s tests if you have that checkout too) before trusting a change.

## Boundaries
Not gitops-cluster, not nixos-config, not application repos: hand those to homelab-ops or developer with a "Hand-off → @<bot>: ..." block. You have no credential for any of them.
```

`hermes/profiles/meta/config.yaml`:

```yaml
# hermes/profiles/meta/config.yaml -- Meta (strong tier, pushes to plder)
model:
  default: anthropic/claude-sonnet-5
  provider: openrouter
fallback_model:
  provider: openrouter
  model: deepseek/deepseek-v4-flash-0731
agent:
  bot_mode_protocol: true

platform_toolsets:
  telegram: [terminal, file, web, skills, memory, todo, session_search, clarify, delegation]
  cli: [terminal, file, web, skills, memory, todo, session_search, delegation]
  cron: [terminal, file, web, skills, memory, delegation]
```

followed by the shared blocks verbatim, with `approvals` gaining, after `single_query_mode: deny`:

```yaml
  smart_policy: |
    ESCALATE to the operator any flagged command that touches .github/, force-pushes,
    or rewrites plder history. APPROVE flagged commands that only read state or run tests.
```

`hermes/profiles/meta/skills.allow.yaml`:

```yaml
bundled:
  - hermes-agent
  - claude-code
  - codebase-inspection
  - requesting-code-review
  - simplify-code
optional: []
```

`hermes/profiles/meta/cron/jobs.json`:

```json
{
  "jobs": []
}
```

- [ ] **Step 4: Route, validate, commit**

```bash
cd /c/Users/Pol/projects/plder
python /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops/add_route.py hermes/root/config.yaml meta 7850573137 REPLACE_META_THREAD
python hermes/ci/validate.py hermes && python -m pytest -q hermes/ci 2>&1 | tail -1
git add hermes/profiles/meta hermes/root/config.yaml .github/workflows/hermes-config.yml
git commit -m "hermes: add the meta bot and guard CI against a .github/-touching deploy"
```

- [ ] **Step 5 (controller): PR, CI, rehearsal, merge, deploy**

Same shape as Task 4 Step 5 (`bot/meta` branch, `rehearse-sync.sh`, `wait-deploy.sh`). Expected in the rehearsal: `installed profile meta`; `skills curated for meta: 5 allowed, <m> bundled disabled`; `image checks: 0 failure(s)`.

- [ ] **Step 6 (controller): Verify meta's write channel and the CI guard**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
ID=$(./agent-probe.sh meta "plder access probe" "Run exactly this terminal command and nothing else: git ls-remote git@github.com-plder-write:Forgenn/plder HEAD . If it printed a 40-character commit hash, reply META_WRITE_KEY_OK. Otherwise reply META_WRITE_KEY_FAIL with the error.")
```

≥ 3 min later: `./probe-result.sh meta "$ID" META_WRITE_KEY_OK` (this only proves the key *authenticates*, not that it can push — `ls-remote` needs no write scope; a real push proof is a low-risk trivial change, e.g. Task 7's `plder-authoring` skill, authored by this plan directly rather than risking an unreviewed agent-authored push here).

CI guard proof (controller, deliberately on a throwaway branch, never `master`):

```bash
cd /c/Users/Pol/projects/plder && git checkout -b ci-guard-probe
echo "# probe, reverted" >> .github/workflows/hermes-config.yml
git commit -am "probe: touch .github (never merged)"
git push -u origin ci-guard-probe
gh pr create --repo Forgenn/plder --base master --head ci-guard-probe --title "CI guard probe (do not merge)" --body "Task 6 verification; closes without merging."
gh pr checks ci-guard-probe --repo Forgenn/plder --watch
gh pr close ci-guard-probe --repo Forgenn/plder --delete-branch
git checkout master && git branch -D ci-guard-probe
```

Expected: CI validates the PR normally (it is not on `master`, so the deploy guard does not fire here — the guard is specifically for a push that *reaches* `master`); this only proves the branch/PR path is unaffected. **A true end-to-end proof of the deploy-guard needs a `.github/**`-touching commit to actually reach `master`, which this plan will not do deliberately (it is a probe, not something to actually risk) — record this as a known verification gap** and note that the tripwire message arriving in Telegram for any *real* future `.github/**`/`hermes/ci/**` change is the ongoing proof instead.

- [ ] **Step 7: Operator question for Task 11** — *"Who are you, and what would you never push?"* Expected gist: meta; edits plder (bots, skills, config); never touches `.github/`, never the cluster or app repos.

---

### Task 7: `skills/custom/` mechanism, proven with a first real skill (plder)

Resolves the spec's open item ("whether the loader picks up `skills/custom/<name>/SKILL.md` identically to a bundled category") on the real pod, and gives meta the skill its own SOUL references.

**Files (branch `bot/meta`, same PR as Task 6, or a follow-up — either is fine, listed separately here because it is conceptually a distinct deliverable):**
- Create: `hermes/profiles/meta/skills/custom/plder-authoring/SKILL.md`
- Modify (if missing): `infra/hermes-agent/sync/test_sync.py` — add the "curation never disables a custom skill under a *real* frontmatter name colliding with nothing bundled" case if plan 4's `test_curate_skills_never_disables_custom_or_chat_created_skills` doesn't already cover a skill living under `skills/custom/` specifically (it does, per plan 4 Task 3's test using `custom/homelab-gitops-drive` — this step is a no-op confirmation, not new code, unless that test is missing when this plan actually runs).

- [ ] **Step 1: Write the skill**

`hermes/profiles/meta/skills/custom/plder-authoring/SKILL.md`:

```markdown
---
name: plder-authoring
description: How to add a bot, a skill, a cron routine, or a Telegram route to the Hermes fleet and pass CI. Use this whenever asked to add or change a bot's persona, credentials, skills, or schedule.
---

# Authoring plder

plder (`Forgenn/plder`, private, branch `master`) is the single source of every
Hermes bot. A push to `master` that touches `hermes/**` (except `hermes/ci/**`)
redeploys the whole fleet within ~10 minutes. Validate before you push:
`python hermes/ci/validate.py hermes`.

## Add a bot
1. `hermes/profiles/<name>/{distribution.yaml, SOUL.md, config.yaml, skills.allow.yaml, cron/jobs.json}`.
   Copy an existing bot's `config.yaml` shared blocks verbatim (`known_builtin_toolsets`,
   `approvals`, `auxiliary`, `platforms`/`plugins`/`desktop`, `secrets`/`cron`/`_config_version`)
   — CI (`hermes/ci/roster.py`) requires them to match root exactly.
2. Give it a Telegram topic: `createForumTopic` must be called INSIDE the pod with the
   pod's own token (never from here) — ask the operator or a controller session to run
   `infra/hermes-agent/ops/create-topic.sh "<Name>"` and give you the numeric thread id.
3. Route it: `python infra/hermes-agent/ops/add_route.py hermes/root/config.yaml <name> 7850573137 <thread_id>`.
4. Declare its skills in `skills.allow.yaml` (`bundled`/`optional` lists) — never write
   `skills.disabled` by hand, it is generated from this file at every sync and CI rejects
   a profile that declares both.

## Add a skill
- **Upstream (bundled/optional) skill**: add its frontmatter name to the bot's
  `skills.allow.yaml`. CI (`hermes/ci/roster.py check_image`) checks the name exists in
  the pinned image and that any `metadata.hermes.requires_toolsets` it declares is a
  subset of that bot's `platform_toolsets.telegram`.
- **New skill authored here (not bundled)**: create it at
  `hermes/profiles/<bot>/skills/custom/<skill-name>/SKILL.md`, and declare
  `skills/custom/` in that bot's `distribution.yaml` `distribution_owned` list (a nested
  path, not a top-level file — `profile_distribution._copy_dist_payload` copies exactly
  that subtree). A skill under `skills/custom/` is never disabled by allowlist curation —
  curation only ever touches names that appear in `.bundled_manifest` on the live pod,
  and a custom skill never does — so it does not need a `skills.allow.yaml` entry.
  It survives every sync and every image bump identically to a bundled one from the
  loader's point of view (proven live: `docs/plans/2026-09-15-hermes-developer-meta-docs.md`
  Task 7 — a real `read_file`/`skills_list` check confirmed the loader treats
  `skills/custom/<name>/SKILL.md` the same as a bundled category directory).
- Never write a skill directly onto `/opt/data` — it is invisible to git, lost with the
  volume, and only reaches the one bot you wrote it on. Author it in the repo.

## Add a routine (cron job)
Add an entry to `hermes/profiles/<bot>/cron/jobs.json`. A job's `deliver` field of
`telegram:<chat>:<thread>` must match a route already declared for that bot (CI checks
this); a `script` field must point under a `scripts/` entry the bot's `distribution_owned`
covers, and the file must actually exist at that path.

## Passing CI
`python hermes/ci/validate.py hermes` runs everything CI runs except the image-dependent
checks (skill existence, toolset names) — those need `hermes/ci/image_checks.py` inside
the pinned image, which only CI itself and a rehearsal pod can run. If you cannot rehearse,
push to a branch and open a PR: CI runs the same two checks there before anything reaches
`master`.

## What you must never do
Never edit `.github/**` — see your own SOUL.md.
```

- [ ] **Step 2: Validate and confirm curation does not touch it**

```bash
cd /c/Users/Pol/projects/plder && python hermes/ci/validate.py hermes
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/sync && python -m pytest -q -k custom_or_chat_created 2>&1 | tail -3
```

Expected: `OK: hermes is valid`; the existing plan-4 test passes, proving `curate_skills` never disables anything under `skills/custom/`. If that specific test does not exist when this plan runs (plan 4 landed with a different test layout), add it verbatim from plan 4 Task 3's `test_curate_skills_never_disables_custom_or_chat_created_skills` before proceeding — do not skip this proof.

- [ ] **Step 3 (controller): Prove the loader treats it identically to a bundled skill, on the real pod**

After Task 6's deploy (meta is live with this skill in its `skills/custom/`):

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
./profile-state.sh meta | python -c "import sys,json; d=json.loads(sys.stdin.read().split('==')[1].split('(exit')[0] if False else sys.stdin.read()); print('n/a, see raw output above')" 2>/dev/null || true
```

(`profile_state.py`'s `_find_all_skills()` call already exercises the identical loader path a bundled skill goes through — just read its `skills_enabled` list from the plain output.) Then a functional probe:

```bash
ID=$(./agent-probe.sh meta "custom skill probe" "Use the skills_list tool. Reply CUSTOM_SKILL_SEEN if 'plder-authoring' appears in the list, CUSTOM_SKILL_MISSING if it does not. Then use skill_view on plder-authoring and reply with the first line of its description.")
```

≥ 3 min later: `./probe-result.sh meta "$ID" CUSTOM_SKILL_SEEN`. Expected: pass, plus the description line echoed back — this is the spec's open item, closed.

---

### Task 8: Install docs — Nix (plder)

**Files (branch `docs/install-nix`, or folded into whichever bot PR is convenient — kept separate here since it has no runtime effect on the pod):**
- Create: `flake.nix`, `flake.lock` (generated), `modules/home-manager/hermes-agent.nix` (in plder, at the repo root — matching where `nixos-config`'s own `flake.nix` lives)
- Create: `docs/install.md` (in plder)

- [ ] **Step 1: Write the flake**

`flake.nix` (plder root):

```nix
{
  description = "plder AI config (pi/ + hermes/) — installable anywhere via Nix home-manager";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    hermes-agent.url = "github:NousResearch/hermes-agent";
  };

  outputs = { self, nixpkgs, home-manager, hermes-agent, ... }: {
    homeManagerModules.default = import ./modules/home-manager/hermes-agent.nix { inherit self hermes-agent; };
  };
}
```

`modules/home-manager/hermes-agent.nix`:

```nix
# plder's own home-manager module: installs the hermes CLI (via the hermes-agent
# flake input, same one nixos-config already pulls in) and pi (no nixpkgs
# derivation exists for @mariozechner/pi-coding-agent -- see the activation step
# below), then links plder's own pi/ and hermes/ config into place, mirroring
# pi/install.sh's own symlink targets (~/.pi/agent/{settings,keybindings,models}.json,
# extensions/, skills/, themes/) so the Nix and manual-install paths produce an
# identical result. plder itself does not manage Claude Code or Hermes Desktop --
# an explicit non-goal (docs/plans/2026-09-15-hermes-developer-meta-docs.md Ruling 8/9).
{ self, hermes-agent }:
{ config, pkgs, lib, ... }:
let
  plderPiDir = self + "/pi";
  piVersion = "0.62.0";  # floor from plder's own package.json peerDependencies
in
{
  home.packages = [
    hermes-agent.packages.${pkgs.system}.default
  ];

  # No nixpkgs derivation for pi exists; a pinned global npm install is the
  # pragmatic compromise (Ruling 9) -- everything else here IS reproducible Nix.
  home.activation.installPiCodingAgent = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    if ! command -v pi >/dev/null 2>&1 || ! pi --version 2>/dev/null | grep -q "${piVersion}"; then
      run ${pkgs.nodejs_24}/bin/npm install -g "@mariozechner/pi-coding-agent@^${piVersion}"
    fi
  '';

  xdg.configFile = {
    "pi/agent/settings.json".source = plderPiDir + "/settings.json";
    "pi/agent/keybindings.json".source = plderPiDir + "/keybindings.json";
    "pi/agent/models.json".source = plderPiDir + "/models.json";
  }
  // lib.listToAttrs (map (f: {
       name = "pi/agent/extensions/${builtins.baseNameOf f}";
       value.source = f;
     }) (lib.filesystem.listFilesRecursive (plderPiDir + "/extensions")))
  // lib.listToAttrs (map (d: {
       name = "pi/agent/skills/${builtins.baseNameOf d}";
       value.source = d;
     }) (builtins.attrValues (builtins.mapAttrs (n: _: plderPiDir + "/skills/${n}")
         (builtins.readDir (plderPiDir + "/skills")))))
  // lib.listToAttrs (map (f: {
       name = "pi/agent/themes/${builtins.baseNameOf f}";
       value.source = f;
     }) (lib.filesystem.listFilesRecursive (plderPiDir + "/themes")));

  # hermes/ profiles: `hermes profile install` is the supported path (it also
  # bootstraps user dirs plan 1-4 rely on), not a raw file link -- run it as an
  # activation step per declared profile so a `home-manager switch` reapplies it
  # the same way profile-sync does on the pod.
  home.activation.installHermesProfiles = lib.hm.dag.entryAfter [ "installPiCodingAgent" ] ''
    export HERMES_HOME="$HOME/.local/share/hermes"
    run ${hermes-agent.packages.${pkgs.system}.default}/bin/hermes profile install ${self}/hermes/root --force || true
  '';
}
```

- [ ] **Step 2: Consumption doc**

`docs/install.md` (plder):

```markdown
# Installing plder's AI config

plder does not manage Claude Code or Hermes Desktop themselves — only its own
`pi/` (the `pi` coding agent config) and `hermes/` (Hermes profiles) content.

## Nix (preferred)

Add to your flake's inputs: `plder.url = "github:Forgenn/plder";` then, in a
`home-manager.users.<you>` block:

```nix
imports = [ plder.homeManagerModules.default ];
```

matching the same `(self + "/modules/home-manager/x.nix")` import convention
`nixos-config` already uses for its own modules. `home-manager switch` installs
the `hermes` CLI, a pinned `pi` via npm (no nixpkgs package exists for it yet),
and links `pi/`'s config + `hermes/root`'s profile into place.

## Windows (Hermes Desktop)

1. Install Hermes Desktop (sets `HERMES_HOME=%LOCALAPPDATA%\hermes`).
2. `git clone https://github.com/Forgenn/plder`
3. `hermes profile install <path-to-plder>\hermes\root` (add `hermes profile install
   <path>\hermes\profiles\<name>` per bot you also want locally).
4. `npm install -g @mariozechner/pi-coding-agent@^0.62.0` (plder's own
   `package.json` floor — check your installed version with `pi --version`
   first; an older one will report peer-dependency warnings pi packages read).
5. `pi install git:github.com/Forgenn/plder` (preferred over the legacy
   `pi/install.sh`, which is bash and needs WSL/Git Bash on Windows anyway).

## Linux / macOS without Nix

1. Install the `hermes` CLI and `pi` (`npm install -g @mariozechner/pi-coding-agent@^0.62.0`).
2. `git clone https://github.com/Forgenn/plder`
3. `hermes profile install ./plder/hermes/root`
4. `pi install git:github.com/Forgenn/plder` (or `./plder/pi/install.sh` — legacy,
   symlinks instead of pi's own package manager).
```

- [ ] **Step 3: Validate what's verifiable here, mark the rest**

```bash
cd /c/Users/Pol/projects/plder
nix flake check 2>&1 | tail -30   # only if Nix is available on this workstation -- it is not, per the draft's finding ("Docker daemon is not running", no evidence Nix is installed on Windows)
```

**Mark explicitly, do not guess:** this workstation is Windows without a Nix installation (no `nix` binary confirmed available in any prior plan's research) — `nix flake check`, an actual `home-manager switch` rehearsal, and an actual `pi install git:github.com/Forgenn/plder` run are **all unverifiable from this session**. The flake and module above are written to match `nixos-config`'s own conventions exactly (same input pins, same import idiom) and are internally consistent (checked by hand: every `home.activation`/`xdg.configFile` reference resolves against files this repo actually has), but they have not been evaluated by a real Nix. **Recommended real verification, for whoever has Nix available:** `nix flake check` in plder, then a `home-manager switch --flake .#<name>` on a nixos-config host that adds this module, confirming `hermes --version` and `pi --version` both resolve afterward.

- [ ] **Step 4: Commit**

```bash
git add flake.nix modules/home-manager/hermes-agent.nix docs/install.md
git commit -m "docs: Nix flake + home-manager module to install plder's AI config anywhere"
```

---

### Task 9: Install docs — Windows and Linux/macOS without Nix (plder)

Already written as part of `docs/install.md` above (Task 8 Step 2) — this task is the **verification** half: prove what can actually be proven on this workstation, and mark what cannot.

- [ ] **Step 1: Verify the Hermes Desktop CLI commands are real, without touching the live install**

```bash
"%LOCALAPPDATA%\hermes\hermes-agent\bin\hermes" profile install --help 2>&1 | head -20
```

(Run from a Windows shell — Git Bash's `MSYS_NO_PATHCONV=1` still applies to the path.) Expected: a real `--help` listing including `--force`/similar flags this doc's Step 3 references; if the flag names differ from what Task 8's doc assumed, fix the doc now rather than leave it unverified. **Do not run `hermes profile install` for real against the live `%LOCALAPPDATA%\hermes`** — that is a live Desktop install per Global Constraints; `--help` only.

- [ ] **Step 2: Verify the pi version gap is still real**

```bash
pi --version 2>&1
node -e "console.log(require('C:/Users/Pol/projects/plder/package.json').peerDependencies)"
```

Expected: workstation pi below `0.62.0` (draft found `0.57.0`); the doc's instruction to `npm install -g @mariozechner/pi-coding-agent@^0.62.0` stands. **Do not run the upgrade** — it would change a tool this session doesn't own the lifecycle of; leave that to whoever follows the doc.

- [ ] **Step 3: Verify `pi install git:...` is a real subcommand, not just `install.sh`'s aspirational comment**

```bash
pi install --help 2>&1 | head -20
```

If `pi install git:<host>/<repo>` is not an actual subcommand on the installed pi version (0.57.0, below the floor plder declares), **mark this in the doc explicitly**: "`pi install git:...` requires pi ≥ 0.62.0; on an older pi, use the legacy `pi/install.sh` (or the Nix module, which does not depend on this subcommand at all)." Update `docs/install.md`'s Windows/Linux sections with whichever is actually true on the version tested.

- [ ] **Step 4: Commit any corrections found in Steps 1–3**

```bash
cd /c/Users/Pol/projects/plder
git add docs/install.md
git commit -m "docs: correct install.md against what this workstation could actually verify"
```

- [ ] **Step 5 (controller): PR and merge (docs-only, safe to land directly since `docs/` is not under `hermes/**`)**

```bash
git push -u origin docs/install-nix
gh pr create --repo Forgenn/plder --base master --head docs/install-nix \
  --title "docs: install plder's AI config on any machine (Nix, Windows, Linux/macOS)" \
  --body "Plan: gitops-cluster docs/plans/2026-09-15-hermes-developer-meta-docs.md Tasks 8-9. docs/ and flake.nix/modules/ are outside hermes/**, so this does not trigger a redeploy."
gh pr checks docs/install-nix --repo Forgenn/plder --watch
gh pr merge docs/install-nix --repo Forgenn/plder --merge --delete-branch
```

Expected: CI's `validate.py` step (if it even runs on a path outside `hermes/**` — check the workflow's `paths:` trigger; if it does not run at all, that is expected and fine) passes or is simply not triggered; no `AGENT_CONFIG_REF` bump, no pod roll.

---

### Task 10: Program close-out — spec open items and capacity (controller)

**Spec open items (`docs/plans/2026-09-12-hermes-config-as-code.md`, "Open items"):**

- [ ] **Step 1: Env expansion inside `jobs.json`**

```bash
export MSYS_NO_PATHCONV=1
POD=$(kubectl get pods -n hermes -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n hermes "$POD" -c hermes-agent -- grep -rn "expand_env\|os.path.expandvars\|\\${.*}" /opt/hermes/cron/jobs.py 2>&1 | head -20
```

Read what comes back: does anything in `cron/jobs.py`'s load/save path call an env-expansion helper on job fields (the way `config.yaml` loading does)? Record the answer directly in the spec (replace the open item's text with the finding) rather than leave it open — the spec called it "moot while the repo is private", which is still true, so this is a documentation-only resolution, not a behavior change.

- [ ] **Step 2: `profile_routes` for private-chat topics — record what plan 2/4 already proved**

Already proven live (plan 2's monitor topic, plan 4's four bot topics, this plan's developer/meta topics): update the spec's open item to state it as resolved, citing the routes that exist today (`monitor-topic`, `homelab-ops-topic`, `shopper-topic`, `research-topic`, `projects-topic`, `developer-topic`, `meta-topic`), each matched by `platform`+`chat_id`+`thread_id` plain string equality per plan 4's Verified facts.

- [ ] **Step 3: `skills/custom/<name>/SKILL.md` loader parity — cite Task 7**

Update the spec's third open item to point at Task 7's live proof (`skills_list`/`skill_view` saw `plder-authoring` identically to a bundled skill).

- [ ] **Step 4: Capacity check with all seven bots live**

```bash
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
./mem.sh 24h; ./mem.sh 1h
kubectl top pod -n hermes --containers; kubectl top nodes
```

Expected: `(ok)` for both ranges (< 2304 MiB). **If either is above**, raise the limit again the way plan 4 Task 1 did (do not throttle a bot to fit): edit `infra/hermes-agent/deployment.yaml`'s `resources.limits.memory` upward (e.g. 3Gi → 4Gi, checking node headroom first — `kubectl top nodes` from this same step), commit, push, roll, re-check. Record the final figures in "Recorded at execution."

---

### Task 11: Whole-program verification checklist and spec Status update (controller)

Mirrors the spec's "Verification" section across all seven bots, plus this plan's own new mechanisms.

- [ ] **Step 1: Restart proves persistence**

```bash
export MSYS_NO_PATHCONV=1
kubectl rollout restart deployment/hermes-agent -n hermes
kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s
cd /c/Users/Pol/projects/gitops-check/infra/hermes-agent/ops
./profile-state.sh default monitor homelab-ops shopper research projects developer meta
```

Expected: every home's `config.yaml` still matches its plder declaration (spot-check `model`/`platform_toolsets` against what's in `plder/hermes/profiles/<n>/config.yaml`); `MEMORY.md` and each `state.db` survived (present, non-empty).

- [ ] **Step 2: Scheduled agent probe in every served profile**

```bash
for p in default monitor homelab-ops shopper research projects developer meta; do
  eval "F_$p=\$(./agent-probe.sh $p 'program close-out probe' 'Reply with exactly CLOSEOUT_OK and nothing else.')"
done
```

≥ 3 min later, for each: `./probe-result.sh <p> "$F_<p>" CLOSEOUT_OK` then `./probe-cleanup.sh <p> "$F_<p>"`. Expected: all eight pass.

- [ ] **Step 3: Persona replies batched as one operator request**

Send one message: *"Please send each bot its question, in its own topic, and tell me when done: Developer — 'Who are you, and what happens if I ask you to touch the cluster or plder?'; Meta — 'Who are you, and what would you never push?'"* (Homelab Ops/Shopper/Research/Projects were already covered by plan 4 Task 12 — do not re-ask them.) Confirm each answer's session row (`source telegram`, correct `thread_id`, model `anthropic/claude-sonnet-5`) the same way plan 4 Task 12 Step 2 did.

- [ ] **Step 4: A conversationally-created reminder survives a restart**

```bash
ID=$(./agent-probe.sh default "conversational reminder probe" "Create a reminder for yourself using the cronjob tool: fire once, five minutes from now, message 'closeout reminder survived'. Then reply CLOSEOUT_REMINDER_CREATED.")
```

≥ 2 min later, confirm creation, then `kubectl rollout restart deployment/hermes-agent -n hermes && kubectl rollout status deployment/hermes-agent -n hermes --timeout=900s`, then confirm the job is still present and fires at its original time (proves upsert-not-overwrite, not just that a declared job survives).

- [ ] **Step 5: A declared job removed from plder disappears on the next sync; the reminder above does not**

Use any currently-declared, low-risk job (e.g. temporarily remove a job from `hermes/profiles/shopper/cron/jobs.json` on a branch, rehearse, confirm it is gone from the rehearsal's job list while a manually-created job in that same rehearsal is untouched) — this proves the `managed_by` tombstone is scoped, matching plan 1's original verification design. Do **not** merge the removal; this is a rehearsal-only proof.

- [ ] **Step 6: Tombstone scoped, offline restart**

```bash
kubectl get networkpolicy -n hermes   # confirm no policy needs disabling; if isolating network requires a mutation, SKIP this step and note it as unverified rather than mutate
```

If pod network can be interrupted read-only (e.g., a temporary NetworkPolicy that already exists blocks egress during a maintenance window — check first, do not create one for this test unless the operator explicitly asks), confirm the pod still starts (`sync.py` skip/fallback path, plan 1's design). If not safely testable without a mutation this plan is not authorized to make, **mark unverified** rather than force it.

- [ ] **Step 7: Update the spec's Status line**

```bash
cd /c/Users/Pol/projects/gitops-check && git checkout main && git pull --ff-only && git checkout -b hermes-spec-closeout
python - <<'PY'
p = "docs/plans/2026-09-12-hermes-config-as-code.md"
s = open(p, encoding="utf-8").read()
old = "Status: design agreed, not yet implemented"
new = "Status: implemented (plans 1-5; last landed 2026-09-1X, see plan 5's Recorded-at-execution table for exact SHAs and dates)"
assert s.count(old) == 1
open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new))
PY
```

Also apply Task 10 Steps 1–3's findings into the spec's "Open items" section (replace each resolved bullet with its resolution and a pointer to the proving task).

```bash
git add docs/plans/2026-09-12-hermes-config-as-code.md
git commit -m "docs: close out the Hermes config-as-code spec (plan 5 complete)"
git pull --rebase origin main && git push origin main
```

Docs-only in gitops-check (not `infra/hermes-agent/`), so this does not roll the pod and is not subject to the pre-push guard.

- [ ] **Step 8: Final record**

Fill "Recorded at execution" at the top of this file (thread ids, fingerprints, merge/gitops SHAs, PAT presence timeline, memory figures) and append a `## Deploy record` section with one line per verification and its observed result. Commit and push (docs only).

---

## Out of scope

- Full credential isolation via separate pods/uids per acting bot (the only way to make the "terminal-capable bot can read what the process can" residual risk actually go away). Tracked as a real option, not adopted — a significant redesign (one Hermes gateway process, one multiplex, currently).
- Upgrading to GitHub Pro to unlock plder rulesets/branch protection (Ruling 6) — a billing decision for the operator, not engineering.
- Automated hand-offs from developer/meta to bots without a terminal (research→shopper/projects, projects→shopper) — unchanged from plan 4's Out of scope; the kanban dispatcher remains the candidate mechanism, still not adopted.
- Fully reproducible Nix packaging of `pi` (Ruling 9) — the module uses a pinned `npm install -g` activation step instead of a real `nixpkgs` derivation.
- Actually running `nix flake check` / `home-manager switch` / `pi install git:...` against a real Nix or a pi ≥ 0.62.0 install — this workstation has neither; Tasks 8-9 mark this explicitly rather than claim untested code works.
- Rewriting the inert `kubectl` rules in root `approvals.smart_policy` (plan 4's same out-of-scope item, still true).
- A shared config layer (`hermes_cli/managed_scope.py`) to de-duplicate the shared blocks every profile now copies verbatim — plan 4's same out-of-scope item.
