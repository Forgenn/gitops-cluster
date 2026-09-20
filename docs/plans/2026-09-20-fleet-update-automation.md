# Fleet update automation

Date: 2026-09-20
Status: **DESIGN — approved, not implemented**
Scope: `Forgenn/gitops-cluster` (this repo), `Forgenn/nixos-config`, `Forgenn/plder`

Keep dependency and system updates moving without a human reading GitHub: deterministic
CI produces facts, Renovate produces proposals, and the `homelab-ops` bot applies a
declared policy — merging the low-risk classes itself and escalating everything else to
Telegram with a `claude -p` verdict.

## Problem

This repo has Renovate, not Dependabot, and its config is sound. The loop simply never
closes. Evidence gathered 2026-09-20:

- **Zero dependency commits have ever merged.** `git log --all | grep -c 'chore(deps)'`
  returns `0`. Renovate has been running since April 2026.
- Nine PRs are open, the oldest (#7, #8, #10, #11, #12, #13, #14, #15) from April; #13 is
  Longhorn 1.10.2. Ten `renovate/*` branches are parked on the remote.
- **Root cause of the stall:** `automergeType: 'branch'` with Renovate's default
  `ignoreTests: false`. Renovate waits for a passing status check before a branch
  automerge. `.github/` contains only `renovate.json5` — there is **no `workflows/`
  directory at all**, so no check ever appears and the branch waits forever.
- **Nothing validates a merge even if one happened.** ArgoCD auto-syncs `main` to
  production with no `kustomize build`, no schema check, no diff. 56 kustomizations,
  none exercised by CI.
- **No notification.** The Dependency Dashboard (issue #2) is the only surface and is
  not read.

`Forgenn/nixos-config` has no automation whatsoever — no CI, no `system.autoUpgrade`, no
flake update job. `flake.lock` staleness at design time: `systems` 1260d, `home-manager`
514d, `agenix` 410d, `nixpkgs-unstable` 386d; main `nixpkgs` 53d. k3s is **not pinned** —
it rides `nixpkgs`, so a lock bump moves the cluster's Kubernetes version. The 2026-09-19
incident began with nodes 24 days adrift (booted 25.05, running 26.05), so this path
needs a gate rather than a cron.

Both repos are **public**: GitHub Actions minutes are free and unlimited.

## Constraint that shaped the design

The obvious answer — `anthropics/claude-code-action` reviewing PRs in CI — is unusable
here. Subscription OAuth tokens expire in roughly a day and refresh support is still
unimplemented (`anthropics/claude-code-action#727`, open). Running Claude in Actions
means `ANTHROPIC_API_KEY` and metered billing, contradicting the model policy this
cluster already enforces (`hermes/ci/roster.py check_model_policy`).

Hermes already runs `claude -p` on the subscription, already owns routed Telegram topics,
and already has its config in git. So **judgement lives in Hermes; CI stays
deterministic**. This is the load-bearing decision of the design.

## Architecture

Three layers, each independently testable, with opinions confined to the third.

```
Layer 1  FACTS       GitHub Actions   builds, evals, schema checks   → status checks
Layer 2  PROPOSALS   Renovate         version bumps, labels          → pull requests
Layer 3  JUDGEMENT   homelab-ops bot  policy + claude -p             → merge or Telegram
```

---

## Layer 1 — Facts

### 1a. `gitops-cluster/.github/workflows/validate.yml`

Triggers on `pull_request`. Steps:

1. Build every kustomization (56, excluding `infra/cert-manager/charts/**` and
   `infra/opencloud/charts/**` per the existing `ignorePaths`).
2. `kubeconform` against upstream Kubernetes schemas plus the CRD catalog, covering the
   Longhorn / CNPG / cert-manager / Gateway API / ExternalSecret types in use.
3. Publish a single status check named **`validate`**.

This is the piece whose absence stalled everything. It gives Renovate and the bot
something real to gate on, and it closes the unguarded-merge-into-auto-syncing-prod gap
independently of any automation.

### 1b. `nixos-config/.github/workflows/validate.yml`

**No full builds, and no binary cache.** Every overlay in the repo is commented out
(`hosts/as-pm/overlays.nix` — "Not using overlays currently"; the openssh patch, cursor
and ghostty blocks are all disabled, and it is the only host importing an overlays file).
The fleet is stock `nixpkgs`. Therefore `nixos-rebuild build` **substitutes** rather than
compiles: it downloads prebuilt paths from cache.nixos.org. CI would not be duplicating
compute the hosts then repeat — it would be duplicating downloads, and both CI and the
hosts pull the same paths from the same public cache. A Cachix or Attic cache would
serve no purpose, because cache.nixos.org already is the cache.

What a bad lock bump actually breaks is **evaluation**: a removed module option, a
renamed or dropped package, a changed default. That needs no realised closure.

| Job | Command | Scope | Cost |
|---|---|---|---|
| `flake-check` | `nix flake check` | repo | seconds |
| `eval` | `nix eval .#nixosConfigurations.<h>.config.system.build.toplevel.drvPath` | **all 7 hosts** | ~1 min, zero downloads |
| `dry-run` | `nix build --dry-run .#nixosConfigurations.<h>...toplevel` | dubois, dolores, hatsum | seconds, zero downloads |

Eval is cheap enough to cover all seven hosts rather than sampling representatives. The
remaining gap — evaluates but a substitute is missing — is rare, and the host rebuild
catches it, which is authoritative regardless.

**The dry-run output is the review artifact.** It names the paths that would be fetched,
i.e. the packages actually moving, and is attached to the PR as a comment. That is what
the bot feeds to `claude -p`, and it is far more reviewable than a `flake.lock` hash
diff:

> *"This bump moves k3s 1.35.6 → 1.36.0 and the kernel 6.12 → 6.14. k3s is the control
> plane and the kernel touches the NVMe path on katsuragi. Escalating."*

Status checks: **`flake-check`**, **`eval`** and **`dry-run`**.

**Condition to watch:** if the overlays are ever re-enabled — particularly the openssh
patch, since much of the closure depends on openssh — CI begins real compilation and a
binary cache becomes worth revisiting. Do not build for it now.

---

## Layer 2 — Proposals

### 2a. Renovate rewrite in `gitops-cluster`

**Delete Renovate's automerge entirely.** `automerge`, `automergeType` and the
`vulnerabilityAlerts.automerge` block all go. Rationale: keeping Renovate-automerge
alongside bot-merge creates two independent mergers, two audit trails, and two places to
reason about trust. One merge authority is the point.

Every update becomes a PR. Renovate's job is now classification only, and its **labels
become the risk signal the policy reads**. Keep unchanged: grouping (`arr-suite`,
`media-apps`), `minimumReleaseAge`, the `core-infra` package list, the custom regex
managers, `ignorePaths`, schedule, timezone.

Add update-type labels so classification is readable from the API without parsing PR
bodies:

```json5
{ matchUpdateTypes: ['patch'],  addLabels: ['patch'] },
{ matchUpdateTypes: ['minor'],  addLabels: ['minor'] },
{ matchUpdateTypes: ['major'],  addLabels: ['major'] },
{ matchUpdateTypes: ['digest'], addLabels: ['digest'] },
```

The two existing `groupName` rules gain matching labels (`addLabels: ['group:media-apps']`
and `['group:arr-suite']`) so group membership is readable the same way. Labels become the
**single** source of classification — the policy never parses a branch name or PR title.

### 2b. New `nixos-config/.github/renovate.json5`

The Renovate **`nix` manager ships `enabled: false`** and must be turned on explicitly;
this is the usual reason people find it silently does nothing. Pair it with
`lockFileMaintenance`, which is what actually moves branch-tracking inputs to the head of
their ref.

```json5
nix: { enabled: true },
lockFileMaintenance: { enabled: true, schedule: [...] },
```

Known quirk: `lockFileMaintenance` does not refresh a `flake.lock` unless a
`github:NixOS/nixpkgs/...` string appears in `flake.nix` (`renovatebot/renovate#29721`).
The existing `nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05"` satisfies this — **the new
track inputs must use the same full `github:` form**, not the `nixpkgs/...` shorthand that
`nixpkgs-unstable` currently uses.

### 2c. Flake track split

Today `mkNixosSystem` hardcodes one `nixpkgs`, so a single lock bump moves all seven
machines including the k3s nodes. Three tracks:

| Input | Hosts | Cadence | Soak |
|---|---|---|---|
| `nixpkgs` | as-pm, t440, hatsum | weekly | 2 days |
| `nixpkgs-nas` | dolores | biweekly | 7 days |
| `nixpkgs-cluster` | dubois, cuno, katsuragi | monthly | 14 days |

All three start on `nixos-26.05`. The split is not about different branches — it is about
letting the tracks **drift independently**, so a workstation update never touches etcd.

Implementation: `mkNixosSystem` gains one `pkgsInput` parameter (default `nixpkgs`); each
`nixosConfigurations` entry passes its track. Three Renovate groups produce three PRs.

**Deliberate simplification:** `home-manager` stays shared rather than splitting three
ways. `useGlobalPkgs = true` is already set, so Home Manager consumes each host's own
`pkgs` and the `follows` mismatch is cosmetic. Splitting it would triple the lock for no
behavioural gain.

---

## Layer 3 — Judgement

### 3a. Owner: `homelab-ops`

The bot that owns cluster operations owns infra merges. This preserves the deliberate
`developer`-cannot-touch-infra boundary: the developer PAT excludes `gitops-cluster`,
`nixos-config` and `plder` by design, and that stays true.

`homelab-ops` is terminal-capable, so `claude -p` is available to it.

### 3b. Credential

A **new fine-grained PAT**, "only select repositories" = exactly `Forgenn/gitops-cluster`
and `Forgenn/nixos-config`. Permissions: **Contents read/write** and **Pull requests
read/write** (both required to merge).

Wired with the pattern already used twice — copy `secrets/externalsecret-developer.yaml`:

- Infisical path `/hermes/HOMELAB_OPS_GITHUB_PAT`, its **own** ExternalSecret
  (`hermes-secrets-homelab-ops`) so a missing path cannot degrade other credentials.
- `argocd.argoproj.io/ignore-healthcheck: "true"` while the path is optional.
- `optional: true` volume `profile-secrets-homelab-ops`, mode 0440, mounted on the main
  container and on `profile-sync`.
- A named `secrets.command` override on the profile reading the second mount.

**Known accepted risk, unchanged:** all profiles share one pod and one uid, so a mounted
token is readable by every profile. This is the same trade already accepted for the
developer PAT and the meta write key.

### 3c. The skill

`plder/hermes/profiles/homelab-ops/skills/custom/update-review/` containing `SKILL.md`
and `review.py`.

Pod inventory confirmed 2026-09-20: `curl`, `git`, `python3`, `claude` present; **`gh`,
`jq` and `nix` absent**. So `review.py` uses `urllib` against the GitHub REST API — no new
installs, no initContainer change.

Daily cron (`managed_by: plder`, `cron.preflight: false` as every secondary profile
requires):

1. List open PRs labelled `renovate` in both repos; fetch each diff and its check runs.
2. Pipe the diff (or, for nixos-config, the dry-run comment) into **`claude -p`** for a
   verdict. Per the model policy this is exactly the Opus-class judgement reserved for the
   subscription: deepseek orchestrates, Claude reads.
3. Apply the policy: merge, or escalate to Telegram with the verdict and a link.

Weekly digest Monday 09:00 to the `homelab-ops` topic: what merged itself, what is
waiting, what has been open more than 14 days.

### 3d. `update-policy.yaml`

Lives at `plder/hermes/profiles/homelab-ops/update-policy.yaml`, validated by plder CI.
Widening trust is a reviewable one-line commit.

```yaml
version: 1
dry_run: true                     # phase 4: report only, never merge

repos:
  Forgenn/gitops-cluster:
    enabled: true
    require_checks: [validate]    # every listed check must be success
    auto_merge:
      - name: low-risk image patches
        labels_all: [renovate, patch]
        labels_none: [core-infra, helm-major, helm-minor, security]
        paths_deny:
          - "infra/longhorn/**"
          - "infra/cert-manager/**"
          - "infra/argocd/**"
          - "infra/external-secrets/**"
          - "infra/cloudnative-pg/**"
          - "infra/hermes-agent/**"
      - name: curated media groups
        labels_any: [group:media-apps, group:arr-suite]
        labels_none: [major]
    escalate_all_else: true

  Forgenn/nixos-config:
    enabled: true
    require_checks: [flake-check, eval, dry-run]
    auto_merge: []                # nothing auto-merges, by design
    escalate_all_else: true

notify:
  digest: "mon 09:00"
  stale_after_days: 14
```

Matcher semantics, so the implementation is unambiguous: a rule fires only when **every**
key it declares holds — `labels_all` all present, `labels_any` at least one present,
`labels_none` none present, and no changed file matching `paths_deny`. A PR auto-merges
when **any one** rule fires and every check in `require_checks` reports success. Path
patterns are gitignore-style POSIX globs where `**` spans directory separators, matched
against repo-relative paths from the PR's changed-files list.

**Security updates escalate rather than auto-merge**, and are flagged priority in
Telegram. A security bump is urgent but also unreviewed, and a security-motivated
Longhorn bump can still take down storage. The design chooses same-day human attention
over unattended speed. Revisit if the escalation proves noisy.

### 3e. Guardrails

- **Fail closed.** Missing check, red check, or `claude -p` unavailable → escalate, never
  merge. The September lesson is that silent failure is the expensive kind.
- **Merge only.** Never force-push, never close, never edit a PR, never push a commit.
- **Path allowlist is independent of labels.** A PR labelled `patch` that touches
  `infra/longhorn/**` escalates regardless of its labels.
- **Every auto-merge is announced**, not only escalations, so the audit trail sits where
  it is read.
- `dry_run: true` is the shipped default; flipping it is a deliberate, separate commit.

---

## Deploys stay manual

Nothing auto-deploys to a NixOS host. Add `docs/runbooks/cluster-nixos-rebuild.md` to
`nixos-config`: `nixos-rebuild boot` rather than `switch`, one node at a time, reboot and
verify etcd health before the next. The 24-day booted-25.05/running-26.05 drift
contributed to the 2026-09-19 incident, and a runbook is the cheap fix.

## Rollout

| Phase | Work | Gate |
|---|---|---|
| 0 | `validate.yml` in gitops-cluster; `validate.yml` in nixos-config | Checks green on a scratch PR |
| 1 | Renovate rewrite (gitops); new `renovate.json5` (nixos) | Renovate reopens PRs with update-type labels |
| 2 | Flake track split, 3 inputs, `pkgsInput` param | Phase 0 CI green on the split PR |
| 3 | PAT, ExternalSecret, volume, credential helper | Bot authenticates; no other app degrades |
| 4 | `update-policy.yaml`, `update-review` skill, cron — `dry_run: true` | Two weeks of "would have merged X" |
| 5 | `dry_run: false` for the gitops auto tier only | — |
| 6 | Backlog triage of the 9 open PRs; cluster rebuild runbook | Backlog at zero or consciously deferred |

Phase 4's dry run is not ceremony: it shows the bot's judgement before it holds the keys.

## Testing

- plder CI, hermetic like the existing 150 tests: policy parsing, label matching, glob
  path matching, fail-closed behaviour on missing/red checks, and `dry_run` never issuing
  a write.
- Fixture-driven tests for `review.py` against recorded GitHub API payloads — no network.
- Layer 1 workflows proven by a deliberately broken scratch PR (invalid manifest; removed
  module option) confirming the checks go red.
- Workstation tests must stay hermetic: `HERMES_HOME` on the Windows workstation points at
  the live Hermes Desktop install.

## Operator steps (cannot be automated)

1. Create the fine-grained PAT (2 repos, Contents RW + Pull requests RW) and store it at
   Infisical `/hermes/HOMELAB_OPS_GITHUB_PAT`.
2. Grant the Renovate GitHub App access to `Forgenn/nixos-config` — it is currently only
   installed on `gitops-cluster`.
3. Confirm the `homelab-ops` Telegram topic id for the digest route.

## Open items

- `home-manager` stays shared rather than splitting with the tracks (see 2c). Revisit only
  if a track-specific HM breakage appears.
- Binary cache deliberately not built; the trigger to reconsider is re-enabled overlays
  (see 1b).
- Whether security-labelled PRs should eventually auto-merge (see 3d).

## Risks

- **Bot merges something harmful.** Mitigated by fail-closed checks, the path denylist,
  the narrow initial auto tier, and the two-week dry run. Blast radius is bounded to
  non-core-infra patch bumps.
- **PAT readable by every profile** (shared uid). Accepted, consistent with existing
  credentials; scope is two repos and the PAT cannot push, only merge.
- **CI noise from 56 kustomizations.** If build time is unreasonable, narrow to changed
  paths rather than weakening the check.
- **Renovate reopening 9 PRs at once** after the automerge rewrite. Phase 6 triages
  deliberately rather than letting the bot act on a cold backlog.
