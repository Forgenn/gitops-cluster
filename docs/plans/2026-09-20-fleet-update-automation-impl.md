# Fleet Update Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make dependency and system updates flow without a human reading GitHub — deterministic CI produces facts, Renovate produces proposals, and the `homelab-ops` bot merges the low-risk classes while escalating the rest to Telegram with a `claude -p` verdict.

**Architecture:** Three layers with opinions confined to the third. GitHub Actions publish status checks (`validate` in gitops-cluster; `flake-check` / `eval` / `dry-run` in nixos-config). Renovate loses all automerge and becomes a pure classifier whose **labels** carry the risk signal. A cron job on the `homelab-ops` Hermes profile reads PRs over the GitHub API, pipes diffs into `claude -p`, and applies a declarative policy file.

**Tech Stack:** GitHub Actions (ubuntu-24.04, SHA-pinned actions), kustomize + helm + kubeconform, Nix flakes, Renovate, Python 3.12 (stdlib only in-pod), pytest, Hermes Agent v2026.8.19.

**Spec:** `docs/plans/2026-09-20-fleet-update-automation.md`

## Global Constraints

- **Model policy (CI-enforced, `hermes/ci/roster.py check_model_policy`):** the only OpenRouter model permitted anywhere is `deepseek/deepseek-v4.1-flash`. No config may set `provider: anthropic` or any Claude model id. Claude is reached **only** by running `claude -p` from a terminal-capable bot.
- **In-pod binaries:** `curl`, `git`, `python3`, `claude` exist. **`gh`, `jq` and `nix` do not.** No `pip install` is available, so pod code uses the standard library plus PyYAML.
- **In-pod interpreter (verified 2026-09-20):** run pod Python as **`/opt/hermes/.venv/bin/python3`** — it carries PyYAML 6.0.3. The system `python3` has no `yaml`. Both are 3.13.5. If an image bump moves that venv the job must fail loudly rather than fall back to a weaker parser.
- **Secondary profiles require** `cron.preflight: false` and `platforms.api_server.enabled: false`. Never share `TELEGRAM_BOT_TOKEN` into a secondary profile — it spawns a competing adapter.
- **`_config_version: 38`** on every profile `config.yaml`; no `mcp_servers` key.
- **Workstation tests must be hermetic**: `HERMES_HOME` on this Windows workstation points at a live Hermes Desktop install.
- **Action pinning:** every GitHub Action is pinned to a full commit SHA with a `# vN` trailing comment, matching `plder/.github/workflows/hermes-config.yml`. Every job sets `persist-credentials: false` on checkout and an explicit `timeout-minutes`.
- **Both repos are public.** Actions minutes are free; the GitHub API read path needs no credential (60 req/hr unauthenticated, ~30 per run).
- **Fail closed everywhere:** a missing check, a red check, or an unavailable `claude -p` escalates. It never merges.
- Commit trailers on every commit:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis`

## File Structure

**`Forgenn/gitops-cluster`**
- Create `scripts/validate-manifests.sh` — builds and schema-validates every kustomization. Runnable locally; the workflow is a thin wrapper.
- Create `.github/workflows/validate.yml` — publishes the `validate` check.
- Modify `.github/renovate.json5` — strip automerge, add classification labels.
- Create `infra/hermes-agent/secrets/externalsecret-homelab-ops.yaml` — the merge PAT.
- Modify `infra/hermes-agent/deployment.yaml` — optional volume + mount.

**`Forgenn/nixos-config`**
- Create `.github/workflows/validate.yml` — `flake-check`, `eval` (7 hosts), `dry-run` (3 hosts + PR comment).
- Create `.github/renovate.json5` — nix manager on, three track groups.
- Modify `flake.nix` — three nixpkgs inputs, `pkgsInput` parameter.
- Create `docs/runbooks/cluster-nixos-rebuild.md`.

**`Forgenn/plder`**
- Create `hermes/profiles/homelab-ops/update-policy.yaml` — the declarative policy.
- Create `hermes/profiles/homelab-ops/skills/custom/update-review/policy.py` — pure matching, no I/O. The only unit-tested logic.
- Create `hermes/profiles/homelab-ops/skills/custom/update-review/review.py` — GitHub API + `claude -p` + actions.
- Create `hermes/profiles/homelab-ops/skills/custom/update-review/SKILL.md`.
- Modify `hermes/profiles/homelab-ops/cron/jobs.json` — the daily job and weekly digest.
- Modify `hermes/profiles/homelab-ops/config.yaml` — named secret override for the PAT mount.
- Create `hermes/ci/test_update_policy.py` — hermetic tests.
- Modify `hermes/ci/validate.py` — schema-check the policy file.

`policy.py` is deliberately separate from `review.py`: it is pure, hermetic and the only part that decides anything, so it is the only part that needs exhaustive tests. `review.py` is I/O and orchestration.

---

### Task 1: Manifest validation script and workflow (gitops-cluster)

**Files:**
- Create: `scripts/validate-manifests.sh`
- Create: `.github/workflows/validate.yml`

**Interfaces:**
- Produces: a required status check named **`validate`**, consumed by Task 8's policy `require_checks`.

- [ ] **Step 1: Write the validation script**

```bash
#!/usr/bin/env bash
# Build and schema-validate every kustomization in infra/.
# Runnable locally: ./scripts/validate-manifests.sh [path ...]
# Requires: kustomize, helm, kubeconform on PATH.
set -uo pipefail

CRD_SCHEMA='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceApiVersion}}.json'
failed=0
count=0

# Bundled upstream charts carry their own templates and non-ASCII READMEs; they
# are excluded from Renovate too (see .github/renovate.json5 ignorePaths).
mapfile -t targets < <(
  if [ "$#" -gt 0 ]; then printf '%s\n' "$@"
  else find infra -name kustomization.yaml -not -path '*/charts/*' -printf '%h\n' | sort
  fi
)

for dir in "${targets[@]}"; do
  count=$((count + 1))
  if ! out=$(kustomize build --enable-helm "$dir" 2>&1); then
    echo "::error file=${dir}/kustomization.yaml::kustomize build failed"
    printf '%s\n' "$out" | tail -30
    failed=1
    continue
  fi
  if ! printf '%s\n' "$out" | kubeconform \
      -strict \
      -ignore-missing-schemas \
      -schema-location default \
      -schema-location "$CRD_SCHEMA" \
      -summary; then
    echo "::error file=${dir}/kustomization.yaml::kubeconform rejected the rendered manifests"
    failed=1
  fi
done

echo "checked ${count} kustomization(s)"
exit "$failed"
```

Note `-ignore-missing-schemas`: an exotic CRD absent from the catalog passes rather than failing the build. This is a deliberate v1 trade — it keeps false failures out of a gate the bot depends on. Tightening it to `-strict` without the ignore is a later, separate decision.

- [ ] **Step 2: Make it executable and prove it fails on a broken manifest**

```bash
chmod +x scripts/validate-manifests.sh
mkdir -p /tmp/kbad && cat > /tmp/kbad/kustomization.yaml <<'EOF'
resources: [bad.yaml]
EOF
cat > /tmp/kbad/bad.yaml <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata: {name: x}
data: "this must be a mapping, not a string"
EOF
./scripts/validate-manifests.sh /tmp/kbad; echo "exit=$?"
```

Expected: non-zero exit with a `::error` line. Then `rm -rf /tmp/kbad`.

- [ ] **Step 3: Run it against the real tree**

```bash
./scripts/validate-manifests.sh
```

Expected: `checked 56 kustomization(s)` and exit 0. If a pre-existing kustomization already fails, **fix it or record it in the PR body** — do not weaken the script to make it pass.

- [ ] **Step 4: Write the workflow**

Resolve each action SHA first (do not invent one):

```bash
gh api repos/actions/checkout/git/ref/tags/v4 --jq .object.sha
```

```yaml
# Renders and schema-validates every kustomization. This is the check that
# Renovate and the homelab-ops update-review job gate on: before it existed,
# automergeType 'branch' waited forever for a status that never appeared, and
# no dependency commit ever landed. See docs/plans/2026-09-20-fleet-update-automation.md
name: validate

on:
  pull_request:
    paths-ignore:
      - 'docs/**'
      - '**.md'
  workflow_dispatch: {}

permissions:
  contents: read

concurrency:
  group: validate-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: true

jobs:
  validate:
    runs-on: ubuntu-24.04
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@<sha-from-step-4> # v4
        with:
          persist-credentials: false

      - name: Install kustomize, helm and kubeconform
        run: |
          set -euo pipefail
          curl -sSfL https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh | bash -s -- 5.7.1 /usr/local/bin
          curl -sSfL https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz | tar xz -C /tmp linux-amd64/helm
          sudo mv /tmp/linux-amd64/helm /usr/local/bin/helm
          curl -sSfL https://github.com/yannh/kubeconform/releases/download/v0.7.0/kubeconform-linux-amd64.tar.gz | tar xz -C /tmp kubeconform
          sudo mv /tmp/kubeconform /usr/local/bin/kubeconform

      - name: Build and validate manifests
        run: ./scripts/validate-manifests.sh
```

- [ ] **Step 5: Commit on a branch and open a PR to prove the check reports**

```bash
git checkout -b ci/manifest-validation
git add scripts/validate-manifests.sh .github/workflows/validate.yml
git commit -m "ci: render and schema-validate every kustomization

Renovate's automergeType 'branch' waits on a status check, and this repo had
no workflows at all, so no dependency commit has ever landed. This is the
check that makes merging mean something -- ArgoCD auto-syncs main to prod.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
git push -u origin ci/manifest-validation
gh pr create --fill
gh pr checks --watch
```

Expected: the `validate` check appears and passes. **Do not merge yet** — Task 3 rides the same branch.

---

### Task 2: Nix evaluation workflow (nixos-config)

**Files:**
- Create: `.github/workflows/validate.yml` (in `Forgenn/nixos-config`)

**Interfaces:**
- Produces: status checks **`flake-check`**, **`eval`**, **`dry-run`**, consumed by Task 8's policy.
- Produces: a PR comment beginning `<!-- nix-dry-run -->` holding the fetch list, read by Task 7's `review.py`.

Evaluate, never build. Every overlay in this repo is commented out (`hosts/as-pm/overlays.nix` — "Not using overlays currently"), so the fleet is stock nixpkgs and a build would only re-download what the hosts fetch from cache.nixos.org anyway. What a bad lock bump breaks is evaluation.

- [ ] **Step 1: Resolve the nix installer action SHA**

```bash
gh api repos/DeterminateSystems/nix-installer-action/git/ref/tags/v20 --jq .object.sha
```

If tag `v20` does not exist, list tags and take the newest stable major:
`gh api repos/DeterminateSystems/nix-installer-action/tags --jq '.[].name' | head`

- [ ] **Step 2: Write the workflow**

```yaml
# Evaluates the flake rather than building it. Every overlay in this repo is
# commented out, so the fleet is stock nixpkgs: `nixos-rebuild build` would
# only re-download paths the hosts fetch from cache.nixos.org themselves. What
# a bad flake.lock bump actually breaks is evaluation -- a removed module
# option, a renamed package, a changed default -- and that needs no closure.
# Design: gitops-cluster docs/plans/2026-09-20-fleet-update-automation.md
name: validate

on:
  pull_request:
  workflow_dispatch: {}

permissions:
  contents: read

concurrency:
  group: validate-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: true

jobs:
  flake-check:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@<sha> # v4
        with:
          persist-credentials: false
      - uses: DeterminateSystems/nix-installer-action@<sha> # v20
      - run: nix flake check --no-build --all-systems

  eval:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        host: [as-pm, t440, hatsum, dubois, cuno, katsuragi, dolores]
    steps:
      - uses: actions/checkout@<sha> # v4
        with:
          persist-credentials: false
      - uses: DeterminateSystems/nix-installer-action@<sha> # v20
      - name: Evaluate ${{ matrix.host }}
        run: |
          nix eval --raw \
            ".#nixosConfigurations.${{ matrix.host }}.config.system.build.toplevel.drvPath"

  dry-run:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    needs: eval
    permissions:
      contents: read
      pull-requests: write
    strategy:
      fail-fast: false
      matrix:
        # One representative per update track. dubois covers cuno and katsuragi:
        # all three import hosts/revachol-cluster/revachol-common.nix.
        host: [dubois, dolores, hatsum]
    steps:
      - uses: actions/checkout@<sha> # v4
        with:
          persist-credentials: false
      - uses: DeterminateSystems/nix-installer-action@<sha> # v20
      - name: What would change on ${{ matrix.host }}
        run: |
          set -euo pipefail
          nix build --dry-run \
            ".#nixosConfigurations.${{ matrix.host }}.config.system.build.toplevel" \
            2> "dry-run-${{ matrix.host }}.txt" || true
          cat "dry-run-${{ matrix.host }}.txt"
      - name: Publish the fetch list to the PR
        if: github.event_name == 'pull_request'
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          {
            echo "<!-- nix-dry-run:${{ matrix.host }} -->"
            echo "### \`nix build --dry-run\` — ${{ matrix.host }}"
            echo '```'
            head -c 60000 "dry-run-${{ matrix.host }}.txt"
            echo '```'
          } > comment.md
          gh pr comment "${{ github.event.pull_request.number }}" --body-file comment.md
```

The `<!-- nix-dry-run:HOST -->` marker is the contract Task 7 parses. Do not change it without changing `review.py`.

- [ ] **Step 3: Open a PR that proves all three checks report**

```bash
cd /c/Users/Pol/projects/nixos-config
git checkout -b ci/flake-validation
git add .github/workflows/validate.yml
git commit -m "ci: evaluate the flake for every host on each PR

Evaluate, don't build: every overlay here is commented out, so the fleet is
stock nixpkgs and a build would only re-download what the hosts fetch from
cache.nixos.org. Eval catches what a lock bump actually breaks -- removed
options, renamed packages -- at zero download cost, so all 7 hosts are
covered rather than a sample. The dry-run fetch list is posted to the PR as
the review artifact.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
git push -u origin ci/flake-validation
gh pr create --fill
gh pr checks --watch
```

Expected: `flake-check` passes, seven `eval (host)` jobs pass, three `dry-run (host)` jobs pass and post comments. If `nix flake check --all-systems` fails on a pre-existing issue unrelated to this change, drop `--all-systems` and note why in the PR body.

- [ ] **Step 4: Merge**

```bash
gh pr merge --squash --delete-branch
```

This must land before Task 5, which needs these checks to validate the flake split.

---

### Task 3: Renovate rewrite (gitops-cluster)

**Files:**
- Modify: `.github/renovate.json5`

**Interfaces:**
- Produces: PRs labelled `renovate` plus exactly one of `patch` / `minor` / `major` / `digest`, and `group:media-apps` / `group:arr-suite` on the two grouped rules. Task 8's policy matches on these labels and **never** parses a branch name or PR title.

- [ ] **Step 1: Remove every automerge directive**

Delete `automergeType: 'branch'` at top level. In `vulnerabilityAlerts`, delete `automerge: true` and `automergeType: 'branch'`, keeping `enabled: true` and `labels: ['security']`. In every `packageRules` entry, delete all `automerge` and `automergeType` keys.

Two independent mergers means two audit trails and two places to reason about trust. One merge authority is the whole point.

- [ ] **Step 2: Add classification labels**

Append to `packageRules`:

```json5
{
  description: 'Classification labels -- the update-review policy reads these and nothing else',
  matchUpdateTypes: ['patch'],
  addLabels: ['patch'],
},
{
  matchUpdateTypes: ['minor'],
  addLabels: ['minor'],
},
{
  matchUpdateTypes: ['major'],
  addLabels: ['major'],
},
{
  matchUpdateTypes: ['digest'],
  addLabels: ['digest'],
},
```

- [ ] **Step 3: Label the two existing groups**

On the `arr-suite` rule add `addLabels: ['group:arr-suite']`; on the `media-apps` rule add `addLabels: ['group:media-apps']`.

- [ ] **Step 4: Validate the JSON5 parses**

```bash
npx --yes renovate-config-validator .github/renovate.json5
```

Expected: `Config validated successfully`.

- [ ] **Step 5: Commit onto the Task 1 branch and merge**

```bash
git add .github/renovate.json5
git commit -m "chore(renovate): drop automerge, classify with labels instead

Renovate's automerge and the update-review bot would be two independent
mergers with two audit trails. Renovate now only classifies; the labels it
adds are the risk signal the policy reads.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
git push
gh pr merge --squash --delete-branch
```

---

### Task 4: Flake track split (nixos-config)

**Files:**
- Modify: `flake.nix:6-30` (inputs), `flake.nix:~60` (`mkNixosSystem`), `flake.nix:~116-185` (`nixosConfigurations`)

**Interfaces:**
- Produces: flake inputs `nixpkgs`, `nixpkgs-nas`, `nixpkgs-cluster`; `mkNixosSystem` accepts `pkgsInput ? nixpkgs`. Task 6's Renovate groups match these input names exactly.

One lock bump currently moves all seven machines including the k3s nodes. Three independent tracks fix that. All three start on `nixos-26.05` — this is about letting them **drift independently**, not about different branches.

- [ ] **Step 1: Add the two track inputs**

After the existing `nixpkgs.url` line:

```nix
    # Update tracks. All three follow nixos-26.05; they are separate inputs so
    # a workstation bump never moves the k3s control plane. Renovate groups
    # them on these names with different cadences and soak times -- see
    # .github/renovate.json5. The full github: form is required: Renovate's
    # lockFileMaintenance does not refresh a flake.lock without it
    # (renovatebot/renovate#29721).
    nixpkgs-cluster.url = "github:NixOS/nixpkgs/nixos-26.05";
    nixpkgs-nas.url = "github:NixOS/nixpkgs/nixos-26.05";
```

- [ ] **Step 2: Parameterise `mkNixosSystem`**

Add `pkgsInput ? nixpkgs,` to the argument set (after `clusterNode ? null,`) and change `nixpkgs.lib.nixosSystem {` to `pkgsInput.lib.nixosSystem {`. Change nothing else in the body — `home-manager` stays shared because `useGlobalPkgs = true` already makes it consume each host's own `pkgs`, so splitting it would triple the lock for no behavioural gain.

- [ ] **Step 3: Assign each host to its track**

Add `pkgsInput = inputs.nixpkgs-cluster;` to `dubois`, `cuno` and `katsuragi`. Add `pkgsInput = inputs.nixpkgs-nas;` to `dolores`. Leave `as-pm`, `t440` and `hatsum` on the default.

- [ ] **Step 4: Regenerate the lock and verify locally if nix is available**

```bash
nix flake lock
git diff --stat flake.lock
```

Expected: `flake.lock` gains `nixpkgs-cluster` and `nixpkgs-nas` nodes. If nix is unavailable on this workstation, skip to Step 5 — **Task 2's CI is the verification gate**, which is exactly why it landed first.

- [ ] **Step 5: Open a PR and let CI prove all seven hosts still evaluate**

```bash
git checkout -b feat/flake-update-tracks
git add flake.nix flake.lock
git commit -m "flake: split nixpkgs into cluster, nas and workstation tracks

One lock bump moved all seven machines including the k3s nodes. Three
separate inputs let the tracks drift independently, so a workstation update
never touches etcd. All three follow nixos-26.05; Renovate gives each its own
cadence and soak. home-manager stays shared: useGlobalPkgs is already set, so
it consumes each host's own pkgs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
git push -u origin feat/flake-update-tracks
gh pr create --fill
gh pr checks --watch
```

Expected: all seven `eval` jobs pass. A failure here means the `pkgsInput` threading is wrong — fix it before merging. Merge with `gh pr merge --squash --delete-branch`.

---

### Task 5: Renovate config for nixos-config

**Files:**
- Create: `.github/renovate.json5` (in `Forgenn/nixos-config`)

**Interfaces:**
- Consumes: input names from Task 4.
- Produces: PRs labelled `renovate` + `track:workstation` / `track:nas` / `track:cluster`.

- [ ] **Step 1: Write the config**

The `nix` manager ships `enabled: false` and must be turned on explicitly — this is the usual reason people find it silently does nothing. `lockFileMaintenance` is what actually moves branch-tracking inputs.

```json5
{
  $schema: 'https://docs.renovatebot.com/renovate-schema.json',
  extends: ['config:recommended', ':dependencyDashboard', ':semanticCommits'],
  timezone: 'Europe/Madrid',
  labels: ['renovate'],
  reviewers: ['Forgenn'],
  commitMessagePrefix: 'chore(deps):',
  // The nix manager is opt-in: its default config carries enabled:false.
  nix: { enabled: true },
  // Inputs are tracked by ref, so a branch-tracking input never has a "newer
  // version" -- lockFileMaintenance is what moves each input to its ref head.
  lockFileMaintenance: {
    enabled: true,
    schedule: ['after 3am and before 6am on monday'],
  },
  packageRules: [
    {
      description: 'Workstations (as-pm, t440, hatsum) -- weekly, short soak',
      matchDepNames: ['nixpkgs'],
      groupName: 'workstation track',
      addLabels: ['track:workstation'],
      minimumReleaseAge: '2 days',
      schedule: ['after 3am and before 6am on monday'],
    },
    {
      description: 'dolores NAS -- biweekly, longer soak (ZFS lags kernel support)',
      matchDepNames: ['nixpkgs-nas'],
      groupName: 'nas track',
      addLabels: ['track:nas'],
      minimumReleaseAge: '7 days',
      schedule: ['after 3am and before 6am on the first day of the month'],
    },
    {
      description: 'k3s nodes -- monthly, longest soak. nixpkgs moves k3s and the kernel.',
      matchDepNames: ['nixpkgs-cluster'],
      groupName: 'cluster track',
      addLabels: ['track:cluster'],
      minimumReleaseAge: '14 days',
      schedule: ['after 3am and before 6am on the first day of the month'],
      prPriority: -1,
    },
  ],
}
```

- [ ] **Step 2: Validate and commit**

```bash
npx --yes renovate-config-validator .github/renovate.json5
git checkout -b ci/renovate-config
git add .github/renovate.json5
git commit -m "chore(renovate): enable the nix manager with three update tracks

The nix manager ships enabled:false, which is why flake.lock never moved.
lockFileMaintenance is what actually advances branch-tracking inputs. Each
track gets its own cadence and soak: workstations weekly, NAS biweekly,
k3s nodes monthly with a 14-day soak.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
git push -u origin ci/renovate-config && gh pr create --fill && gh pr merge --squash --delete-branch
```

- [ ] **Step 3: Record the operator step**

Renovate is installed on `gitops-cluster` only. Note in the handoff that the operator must grant the Renovate GitHub App access to `Forgenn/nixos-config`, or no PR will ever appear.

---

### Task 6: Policy engine (plder)

**Files:**
- Create: `hermes/profiles/homelab-ops/update-policy.yaml`
- Create: `hermes/profiles/homelab-ops/skills/custom/update-review/policy.py`
- Create: `hermes/ci/test_update_policy.py`

**Interfaces:**
- Produces: `load_policy(text: str) -> dict`, `decide(pr: dict, repo_policy: dict) -> Decision` where `Decision` is a `NamedTuple(action: str, reason: str, rule: str | None)` and `action` is `"merge"` or `"escalate"`.
- Consumes: `pr` dicts shaped `{"number": int, "labels": list[str], "checks": dict[str, str], "files": list[str]}`. `checks` maps check name to conclusion (`"success"`, `"failure"`, `"neutral"`, …).

Pure, no I/O, stdlib only. This is the only part that decides anything, so it is the only part with exhaustive tests.

- [ ] **Step 1: Write the policy file**

```yaml
# Declarative merge policy for the update-review job. Widening trust is a
# reviewable one-line commit. Matcher semantics: a rule fires only when EVERY
# key it declares holds (labels_all all present, labels_any at least one,
# labels_none none present, no changed file matching paths_deny). A PR merges
# when ANY rule fires AND every check in require_checks reports success.
version: 1
dry_run: true

repos:
  Forgenn/gitops-cluster:
    enabled: true
    require_checks: [validate]
    auto_merge:
      - name: low-risk image patches
        labels_all: [renovate, patch]
        labels_none: [core-infra, helm-major, helm-minor, security, major]
        paths_deny:
          - "infra/longhorn/**"
          - "infra/cert-manager/**"
          - "infra/argocd/**"
          - "infra/external-secrets-operator/**"
          - "infra/cnpg/**"
          - "infra/hermes-agent/**"
      - name: curated media groups
        labels_any: ["group:media-apps", "group:arr-suite"]
        labels_none: [major, security]
    escalate_all_else: true

  Forgenn/nixos-config:
    enabled: true
    require_checks: [flake-check, eval, dry-run]
    auto_merge: []
    escalate_all_else: true

notify:
  digest_day: mon
  stale_after_days: 14
```

Security-labelled PRs escalate rather than auto-merge, flagged priority. A security bump is urgent but unreviewed, and a security-motivated Longhorn bump can still take down storage.

- [ ] **Step 2: Write the failing tests**

```python
"""Hermetic tests for the update-review merge policy. No network, no pod."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "profiles/homelab-ops/skills/custom/update-review"))

import policy  # noqa: E402

POLICY_PATH = ROOT / "profiles/homelab-ops/update-policy.yaml"


@pytest.fixture(scope="module")
def loaded():
    return policy.load_policy(POLICY_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def gitops(loaded):
    return loaded["repos"]["Forgenn/gitops-cluster"]


def pr(labels, checks=None, files=None):
    return {
        "number": 1,
        "labels": labels,
        "checks": {"validate": "success"} if checks is None else checks,
        "files": files or ["infra/navidrome/values/navidrome.yaml"],
    }


def test_shipped_policy_defaults_to_dry_run(loaded):
    assert loaded["dry_run"] is True


def test_patch_with_green_check_merges(gitops):
    d = policy.decide(pr(["renovate", "patch"]), gitops)
    assert d.action == "merge"
    assert d.rule == "low-risk image patches"


def test_core_infra_label_escalates(gitops):
    d = policy.decide(pr(["renovate", "patch", "core-infra"]), gitops)
    assert d.action == "escalate"


def test_major_escalates(gitops):
    assert policy.decide(pr(["renovate", "major"]), gitops).action == "escalate"


def test_denied_path_escalates_despite_patch_label(gitops):
    d = policy.decide(
        pr(["renovate", "patch"], files=["infra/longhorn/kustomization.yaml"]), gitops
    )
    assert d.action == "escalate"
    assert "longhorn" in d.reason


def test_red_check_escalates(gitops):
    d = policy.decide(pr(["renovate", "patch"], checks={"validate": "failure"}), gitops)
    assert d.action == "escalate"


def test_missing_check_escalates(gitops):
    """Fail closed: absent is not success."""
    d = policy.decide(pr(["renovate", "patch"], checks={}), gitops)
    assert d.action == "escalate"


def test_media_group_merges(gitops):
    d = policy.decide(pr(["renovate", "group:media-apps"]), gitops)
    assert d.action == "merge"
    assert d.rule == "curated media groups"


def test_nixos_never_auto_merges(loaded):
    repo = loaded["repos"]["Forgenn/nixos-config"]
    d = policy.decide(
        pr(["renovate", "track:workstation"],
           checks={"flake-check": "success", "eval": "success", "dry-run": "success"},
           files=["flake.lock"]),
        repo,
    )
    assert d.action == "escalate"


def test_unknown_labels_escalate(gitops):
    assert policy.decide(pr(["renovate"]), gitops).action == "escalate"
```

- [ ] **Step 3: Run the tests and watch them fail**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest hermes/ci/test_update_policy.py -q
```

Expected: `ModuleNotFoundError: No module named 'policy'`.

- [ ] **Step 4: Implement `policy.py`**

Uses real PyYAML — verified present at `/opt/hermes/.venv/bin/python3` in the pod (6.0.3). **Do not hand-roll a YAML parser**: a second parser that disagrees with CI's is a silent-corruption bug waiting to happen, and this file gates merges.

```python
"""Merge policy for the update-review job.

Pure and hermetic: no network, no subprocess. Everything that decides whether
a PR may merge lives here so it can be tested exhaustively.

Requires PyYAML, which exists in Hermes' own venv but NOT in the pod's system
python3 -- run this under /opt/hermes/.venv/bin/python3. If that import fails
the job must die rather than degrade to a weaker parser: this file gates
merges, and two parsers that disagree is a silent-corruption bug.
"""
from __future__ import annotations

import fnmatch
from typing import NamedTuple

import yaml


class Decision(NamedTuple):
    action: str           # "merge" | "escalate"
    reason: str
    rule: str | None


def load_policy(text: str) -> dict:
    """Parse update-policy.yaml. Raises on malformed input -- fail closed."""
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("update-policy.yaml must be a mapping")
    if loaded.get("version") != 1:
        raise ValueError(f"unsupported policy version: {loaded.get('version')!r}")
    return loaded


def decide(pr: dict, repo_policy: dict) -> Decision:
    """Return the action for one PR. Fail closed: anything unclear escalates."""
    if not repo_policy.get("enabled", False):
        return Decision("escalate", "repo not enabled in policy", None)

    required = repo_policy.get("require_checks") or []
    checks = pr.get("checks") or {}
    for name in required:
        conclusion = checks.get(name)
        if conclusion is None:
            return Decision("escalate", f"required check {name!r} has not reported", None)
        if conclusion != "success":
            return Decision("escalate", f"check {name!r} is {conclusion}", None)

    labels = set(pr.get("labels") or [])
    files = pr.get("files") or []

    for rule in repo_policy.get("auto_merge") or []:
        name = rule.get("name", "<unnamed>")
        if not set(rule.get("labels_all") or []) <= labels:
            continue
        any_of = rule.get("labels_any") or []
        if any_of and not (set(any_of) & labels):
            continue
        if set(rule.get("labels_none") or []) & labels:
            continue
        denied = _first_denied(files, rule.get("paths_deny") or [])
        if denied:
            return Decision("escalate", f"changed file {denied} is denied by policy", None)
        return Decision("merge", f"matched rule {name!r}, all checks green", name)

    return Decision("escalate", "no auto-merge rule matched", None)


def _first_denied(files: list[str], patterns: list[str]) -> str | None:
    for path in files:
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("/**", "/*")):
                return path
    return None
```

- [ ] **Step 5: Run the tests until green**

```bash
python -m pytest hermes/ci/test_update_policy.py -q
```

Expected: 10 passed.

- [ ] **Step 6: Run the whole plder suite to prove nothing regressed**

```bash
python -m pytest hermes/ci -q
```

Expected: the existing 150 tests plus the new ones, all passing.

- [ ] **Step 7: Commit**

```bash
git add hermes/profiles/homelab-ops/update-policy.yaml \
        hermes/profiles/homelab-ops/skills/custom/update-review/policy.py \
        hermes/ci/test_update_policy.py
git commit -m "homelab-ops: declarative merge policy for dependency PRs

Pure matching, stdlib only -- the pod has no PyYAML and no jq. Fail closed:
a missing check is not a success, and a denied path escalates regardless of
labels. Ships dry_run: true.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
```

---

### Task 7: Review driver and skill (plder)

**Files:**
- Create: `hermes/profiles/homelab-ops/skills/custom/update-review/review.py`
- Create: `hermes/profiles/homelab-ops/skills/custom/update-review/SKILL.md`
- Modify: `hermes/profiles/homelab-ops/cron/jobs.json`

**Interfaces:**
- Consumes: `policy.load_policy`, `policy.decide` from Task 6.
- Consumes: the `<!-- nix-dry-run:HOST -->` PR comment marker from Task 2.
- Consumes: `HOMELAB_OPS_GITHUB_PAT` from the profile environment (Task 8). Absent → read-only, report-only.

- [ ] **Step 1: Write the driver**

Stdlib (`urllib`, `json`, `subprocess`) plus PyYAML via `policy.py`. Reads work unauthenticated because both repos are public; the PAT is needed only for the merge.

```python
#!/usr/bin/env python3
"""Review open dependency PRs and apply the declared merge policy.

Reads need no credential -- both repos are public. HOMELAB_OPS_GITHUB_PAT is
required only to merge; without it the job still reports, which is the same
behaviour as dry_run: true.

Judgement calls go to `claude -p`: per the model policy Claude is reachable
only through the CLI, never as a configured provider. If claude is missing or
fails, the PR escalates -- it never merges on a missing verdict.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

import policy

API = "https://api.github.com"
HERE = pathlib.Path(__file__).resolve().parent
POLICY_FILE = HERE.parent.parent.parent / "update-policy.yaml"
TOKEN = os.environ.get("HOMELAB_OPS_GITHUB_PAT", "")


def api(path: str, method: str = "GET", body: dict | None = None) -> object:
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "hermes-update-review")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else {}


def open_pulls(repo: str) -> list[dict]:
    return api(f"/repos/{repo}/pulls?state=open&per_page=50")


def check_conclusions(repo: str, sha: str) -> dict[str, str]:
    """Merge check-runs and legacy commit statuses into one name -> conclusion map."""
    out: dict[str, str] = {}
    runs = api(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
    for run in runs.get("check_runs", []):
        name = run["name"].split(" (")[0]          # collapse matrix legs
        result = run.get("conclusion") or "pending"
        if out.get(name) == "failure":
            continue
        out[name] = result if out.get(name) in (None, "success") else out[name]
    for st in api(f"/repos/{repo}/commits/{sha}/status").get("statuses", []):
        out.setdefault(st["context"], "success" if st["state"] == "success" else st["state"])
    return out


def changed_files(repo: str, number: int) -> list[str]:
    return [f["filename"] for f in api(f"/repos/{repo}/pulls/{number}/files?per_page=100")]


def review_material(repo: str, number: int) -> str:
    """The diff, or for nixos-config the dry-run fetch list CI posted."""
    comments = api(f"/repos/{repo}/issues/{number}/comments?per_page=100")
    dry = [c["body"] for c in comments if "<!-- nix-dry-run:" in c.get("body", "")]
    if dry:
        return "\n\n".join(dry)[:40000]
    req = urllib.request.Request(f"{API}/repos/{repo}/pulls/{number}")
    req.add_header("Accept", "application/vnd.github.v3.diff")
    req.add_header("User-Agent", "hermes-update-review")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()[:40000]


def claude_verdict(repo: str, pr: dict, material: str) -> str | None:
    """Opus-class judgement via the CLI. None means no verdict -- escalate."""
    prompt = (
        f"You are reviewing an automated dependency update to {repo}, a homelab "
        f"Kubernetes/NixOS fleet. PR #{pr['number']}: {pr['title']}\n\n"
        "In at most four sentences: what actually changes, what could break in a "
        "3-node k3s cluster with Longhorn storage, and whether a human should look "
        "before this merges. Be concrete about version numbers.\n\n"
        f"{material}"
    )
    try:
        done = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[update-review] claude unavailable: {exc}", file=sys.stderr)
        return None
    if done.returncode != 0:
        print(f"[update-review] claude exit {done.returncode}: {done.stderr[:400]}", file=sys.stderr)
        return None
    return done.stdout.strip() or None


def merge(repo: str, number: int, title: str) -> bool:
    if not TOKEN:
        print(f"[update-review] no PAT; cannot merge {repo}#{number}")
        return False
    try:
        api(f"/repos/{repo}/pulls/{number}/merge", method="PUT",
            body={"merge_method": "squash", "commit_title": f"{title} (#{number})"})
        return True
    except urllib.error.HTTPError as exc:
        print(f"[update-review] merge refused for {repo}#{number}: {exc.code}", file=sys.stderr)
        return False


def main() -> int:
    pol = policy.load_policy(POLICY_FILE.read_text(encoding="utf-8"))
    dry_run = bool(pol.get("dry_run", True))
    merged, escalated = [], []

    for repo, repo_pol in pol.get("repos", {}).items():
        if not repo_pol.get("enabled"):
            continue
        for raw in open_pulls(repo):
            labels = [lab["name"] for lab in raw.get("labels", [])]
            if "renovate" not in labels:
                continue
            number = raw["number"]
            pr = {
                "number": number,
                "title": raw["title"],
                "labels": labels,
                "checks": check_conclusions(repo, raw["head"]["sha"]),
                "files": changed_files(repo, number),
            }
            decision = policy.decide(pr, repo_pol)
            verdict = claude_verdict(repo, pr, review_material(repo, number))
            if decision.action == "merge" and verdict is None:
                decision = policy.Decision("escalate", "no claude verdict available", None)

            entry = {
                "repo": repo, "number": number, "title": raw["title"],
                "url": raw["html_url"], "reason": decision.reason,
                "rule": decision.rule, "verdict": verdict,
            }
            if decision.action == "merge":
                if dry_run:
                    entry["reason"] = f"DRY RUN would merge: {decision.reason}"
                    escalated.append(entry)
                elif merge(repo, number, raw["title"]):
                    merged.append(entry)
                else:
                    entry["reason"] = "merge call failed"
                    escalated.append(entry)
            else:
                escalated.append(entry)

    print(json.dumps({"dry_run": dry_run, "merged": merged, "escalated": escalated}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write `SKILL.md`**

```markdown
---
name: update-review
description: "Review open Renovate PRs in gitops-cluster and nixos-config, merge the low-risk ones, escalate the rest."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [homelab, renovate, dependencies, github, gitops]
---

# Dependency update review

Applies `update-policy.yaml` to the open Renovate PRs in `Forgenn/gitops-cluster`
and `Forgenn/nixos-config`. Design: gitops-cluster
`docs/plans/2026-09-20-fleet-update-automation.md`.

## Run it

```bash
/opt/hermes/.venv/bin/python3 ~/.hermes/skills/custom/update-review/review.py
```

The venv interpreter is required: the pod's system `python3` has no PyYAML. If
that path stops existing after an image bump, the script exits non-zero — report
that plainly rather than reaching for another interpreter.

It prints one JSON object: `{"dry_run", "merged", "escalated"}`.

## What to do with the output

Post a Telegram message to your own topic:

- **Merged** — one line each: repo, PR number, title. State plainly that it
  merged itself.
- **Escalated** — for each, the title, the link, the policy reason, and
  Claude's verdict verbatim. Do not summarise the verdict away; it is the
  reason a human is being asked.
- If `dry_run` is true, say so at the top. Entries reading `DRY RUN would
  merge` are what the bot *would* have done — that is the point of the phase.

Lead with anything labelled `security`. Those escalate by design rather than
auto-merging, so they need same-day human attention.

## Rules

- **Never merge by hand** to work around a red check or a policy refusal. If
  the policy is wrong, change `update-policy.yaml` in plder and let CI validate
  it — that is the audit trail.
- The script only ever merges. It does not push, force-push, close or edit PRs.
- A missing `claude -p` verdict escalates. Do not substitute your own verdict:
  per the model policy, judgement on infra diffs is Claude's, reached through
  the CLI.
```

- [ ] **Step 3: Add the cron jobs**

Replace `hermes/profiles/homelab-ops/cron/jobs.json`:

```json
{
  "jobs": [
    {
      "id": "homelab-ops-update-review",
      "managed_by": "plder",
      "schedule": "0 8 * * *",
      "enabled": true,
      "prompt": "Use the update-review skill: run its review.py, then report the result to your Telegram topic exactly as the skill's 'What to do with the output' section describes. If the script exits non-zero or prints no JSON, say so plainly rather than inventing a result."
    },
    {
      "id": "homelab-ops-update-digest",
      "managed_by": "plder",
      "schedule": "0 9 * * 1",
      "enabled": true,
      "prompt": "Weekly dependency digest. Run the update-review skill's review.py, then report to your Telegram topic: what merged itself in the last seven days, what is still waiting on a human, and every PR open more than 14 days. Name the oldest one explicitly."
    }
  ]
}
```

- [ ] **Step 4: Validate against plder CI**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest hermes/ci -q && python hermes/ci/validate.py
```

Expected: all tests pass and the validator accepts the new cron ids and skill. If `validate.py` rejects an unknown `update-policy.yaml`, extend it — see Task 8 Step 1.

- [ ] **Step 5: Commit**

```bash
git add hermes/profiles/homelab-ops/skills/custom/update-review \
        hermes/profiles/homelab-ops/cron/jobs.json
git commit -m "homelab-ops: update-review skill and daily cron

Reads PRs unauthenticated (both repos are public), pipes the diff or the
CI-posted nix dry-run into claude -p for the verdict, applies the policy. A
missing verdict escalates rather than merging.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
```

---

### Task 8: Policy validation and the merge credential

**Files:**
- Modify: `hermes/ci/validate.py` (plder)
- Create: `infra/hermes-agent/secrets/externalsecret-homelab-ops.yaml` (gitops-cluster)
- Modify: `infra/hermes-agent/secrets/kustomization.yaml`, `infra/hermes-agent/deployment.yaml`
- Modify: `hermes/profiles/homelab-ops/config.yaml` (plder)

- [ ] **Step 1: Teach `validate.py` about the policy file**

Add a check that parses `hermes/profiles/*/update-policy.yaml` and asserts: `version == 1`; every `repos` entry has `enabled`, `require_checks` and `auto_merge`; every `auto_merge` rule has a `name`; and every label named in a rule is one Renovate actually applies (`renovate`, `patch`, `minor`, `major`, `digest`, `security`, `core-infra`, `helm-minor`, `helm-major`, `group:*`, `track:*`). The label check is the valuable one — a typo like `core_infra` would otherwise silently widen auto-merge.

- [ ] **Step 2: Create the ExternalSecret**

Copy `externalsecret-developer.yaml`, changing the name to `hermes-secrets-homelab-ops`, the `secretKey` to `HOMELAB_OPS_GITHUB_PAT`, and the remote key to `/hermes/HOMELAB_OPS_GITHUB_PAT`. **Keep** `argocd.argoproj.io/ignore-healthcheck: "true"` — the PAT is optional, and a missing Infisical path must not degrade the whole ArgoCD app, which is exactly what masked a real storage incident on 2026-09-19.

Add it to `secrets/kustomization.yaml`.

- [ ] **Step 3: Mount it optionally**

In `deployment.yaml`, add a `profile-secrets-homelab-ops` volume (`secret: {secretName: hermes-secrets-homelab-ops, optional: true, defaultMode: 0440}`) mounted at `/etc/hermes-profile-secrets-homelab-ops` on **both** the main container and the `profile-sync` initContainer. The initContainer mount is not optional-in-practice: the first developer deploy logged `env: 0 managed key(s)` precisely because it was missing.

- [ ] **Step 4: Add the named secret override to the profile**

In `hermes/profiles/homelab-ops/config.yaml`, extend `secrets` with a second named command reading `/etc/hermes-profile-secrets-homelab-ops/*`, mirroring the developer profile's override. Keep `override_existing: true` and `_config_version: 38`.

- [ ] **Step 5: Verify and commit both repos**

```bash
cd /c/Users/Pol/projects/plder && python -m pytest hermes/ci -q
cd /c/Users/Pol/projects/gitops-check && ./scripts/validate-manifests.sh infra/hermes-agent
```

Expected: both green. Commit each repo separately with the standard trailers.

---

### Task 9: Cluster rebuild runbook (nixos-config)

**Files:**
- Create: `docs/runbooks/cluster-nixos-rebuild.md`

- [ ] **Step 1: Write the runbook**

Cover, concretely: `nixos-rebuild boot` rather than `switch` on k3s nodes; one node at a time; reboot and confirm `kubectl get nodes` shows Ready plus etcd health before touching the next; dubois is the control plane and goes last; how to roll back to the previous generation from the bootloader. State the reason at the top — the 2026-09-19 incident began with nodes 24 days adrift, booted 25.05 while running 26.05.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/cluster-nixos-rebuild.md
git commit -m "docs: cluster nixos-rebuild runbook

Deploys stay manual by design. The September incident began with nodes 24
days adrift (booted 25.05, running 26.05); a runbook is the cheap fix.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JkeDBVcEJAuqSyr7oEjFis"
```

---

## Operator steps (cannot be automated)

1. Create a fine-grained PAT — "only select repositories" = exactly `Forgenn/gitops-cluster` and `Forgenn/nixos-config`; permissions **Contents: read/write** and **Pull requests: read/write**. Store at Infisical `/hermes/HOMELAB_OPS_GITHUB_PAT`.
2. Grant the Renovate GitHub App access to `Forgenn/nixos-config` — it is installed on `gitops-cluster` only, so without this Task 5 produces nothing.
3. Confirm the `homelab-ops` Telegram topic id for the digest.

Until 1 is done the bot runs read-only and reports, which is the same behaviour as `dry_run: true`.

## Phase 5 — flipping the switch (not part of this plan)

After two weeks of `dry_run: true` output the operator reviews the "would have merged" entries. Flipping `dry_run: false` is a **separate one-line commit** to `update-policy.yaml`, deliberately not bundled here.

## Self-review notes

- Spec coverage: Layer 1 → Tasks 1–2; Layer 2 → Tasks 3–5; Layer 3 → Tasks 6–8; manual deploys/runbook → Task 9; operator steps carried forward verbatim.
- The spec's `paths_deny` listed `infra/external-secrets/**` and `infra/cloudnative-pg/**`; the real directories are `infra/external-secrets-operator/` and `infra/cnpg/`. Task 6 uses the **real** paths.
- `Decision` is used identically in `policy.py`, the tests and `review.py`.
- Task 2's `<!-- nix-dry-run:HOST -->` marker is consumed by Task 7's `review_material`; the two must change together.
