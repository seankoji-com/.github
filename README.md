# seankoji-com organization defaults

This public repository holds the files that GitHub can apply across the
`seankoji-com` organization.

## Inherited files

Repositories use these defaults when they do not define their own version:

- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`
- `SECURITY.md`
- `SUPPORT.md`
- `.github/ISSUE_TEMPLATE/`
- `.github/PULL_REQUEST_TEMPLATE.md`

A repository should keep a local override only when its project needs different
instructions or fields. GitHub does not copy inherited files into a repository,
its clones, or release archives.

## Reusable workflows

The workflows under `.github/workflows/` provide shared implementations for CI,
static analysis, pull-request gating, dependency automation, link checking, and
release markers. GitHub Actions workflows are not inherited automatically.
Each repository still needs a small local caller for every shared workflow it
wants to run.

Available reusable workflows include:

- `reusable-node-ci.yml`
- `reusable-shellspec.yml`
- `reusable-docker-build-push.yml`
- `reusable-static-analysis.yml`
- `reusable-agent-readiness.yml`
- `reusable-pr-gatekeeper.yml`
- `reusable-ci-result.yml`
- `reusable-dependabot-automerge.yml`
- `reusable-issue-triage.yml`
- `reusable-link-check.yml`
- `reusable-review-event.yml`
- `reusable-persona-recovery-request.yml`
- `released.yml`

- [Node CI](.github/workflows/reusable-node-ci.yml) accepts `pnpm`, `npm`, or
  `yarn`. An existing `.nvmrc` takes precedence over the `node-version` input.
- [Docker builds](.github/workflows/reusable-docker-build-push.yml) require
  `image-name`. Cache scope defaults to that image name; use `cache-scope` for
  multiple variants. Scope must be nonempty and contain only letters, digits,
  dot, underscore, slash, colon, `@` or hyphen.
- [ShellSpec](.github/workflows/reusable-shellspec.yml) uses a pinned version on
  a runner with zsh available; private repositories should select the `mint` pool,
  whose runner image includes zsh. The caller's `.shellspec` selects
  the shell, for example `--shell /bin/zsh`. The workflow needs `contents: read`.
- [Issue triage](.github/workflows/reusable-issue-triage.yml) and
  [release markers](.github/workflows/released.yml) use `actions/github-script`.
  Callers selecting self-hosted runners must use Actions runner v2.327.1 or newer.
  Issue triage needs `issues: write` (and `pull-requests: read` for the PR-sync job).

### Runner selection

Set the organization Actions variable `PRIVATE_REPO_SELF_HOSTED` to `true`
with repository access **Private repositories** before relying on omitted
`runner-json` inputs. Do not expose it to public repositories or create
repository-level overrides. No organization settings are changed by this repo.

Every reusable with `runner-json` selects runners in this order:

1. A nonempty explicit `runner-json` value, unchanged.
2. When `vars.PRIVATE_REPO_SELF_HOSTED == 'true'`, the workflow's labels below.
3. Otherwise `["ubuntu-latest"]`, for public callers and during provisioning.

| Workflow | Private default labels | Capability |
| --- | --- | --- |
| `reusable-docker-build-push.yml` | `["self-hosted","Linux","X64","has-docker"]` | Docker daemon and x64 builds |
| `reusable-link-check.yml` | `["self-hosted","Linux","X64"]` | Linux x64 Lychee binaries |
| `reusable-shellspec.yml` | `["self-hosted","Linux","X64","mint"]` | zsh in the runner image |
| All other `runner-json` reusables | `["self-hosted","Linux"]` | Architecture-independent scripts or setup actions |

GitHub makes [`vars` available in `jobs.<job_id>.runs-on`](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability)
and uses the [caller repository's variables for reusable workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#configuration-variable-precedence).
This routes push, pull_request, pull_request_target, schedule, workflow_dispatch,
workflow_run and merge_group without reading `github.event.repository`.
The [private-only access policy](https://docs.github.com/en/actions/concepts/workflows-and-actions/variables)
keeps public callers, including public fork PRs, on hosted runners. GitHub sends
[fork PR events to the base repository](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request),
so its caller policy applies, not the head fork's visibility. Do not use this
variable as an approval boundary for untrusted code in private repos.

GitHub's [Dependabot restrictions](https://docs.github.com/en/code-security/reference/supply-chain-security/troubleshoot-dependabot/dependabot-on-actions)
describe tokens and secrets, but do not establish configuration-variable
availability. Verify omitted-input routing on a Dependabot-triggered run after
provisioning. If the variable is unavailable, that run falls back to hosted;
explicit self-hosted `runner-json` values avoid that residual. Unset variables
also leave private omissions on hosted until the operator provisions them.

Agent Readiness uses a fresh checkout directory for each job and removes it
during cleanup. When it encounters partial-clone metadata at the workspace root
from an older run, it deletes the workspace root's `.git` directory so the next
root checkout can start fresh. See [Agent Readiness](docs/agent-readiness.md).

Persona recovery handles the CI App private key, so it never uses the shared
pool. Per-head request jobs are disabled by default to avoid hosted charges on
every PR head. Normal webhook review dispatch and the private controller's
externally monitored sweep provide recovery. Set
`PERSONA_RECOVERY_REQUEST_ENABLED=true` only to opt back into per-head requests;
verify the sweep and its freshness alarm before adopting this default.
This Actions configuration variable is resolved in the **calling repository's**
context. An organization variable applies only where that repository can access
it; a repository variable overrides it. Setting it only in this `.github`
repository does not enable other callers. An absent value or anything other
than the exact string `true` silently skips the job, without allocating a runner
or producing step logs. Set a caller override with
`gh variable set PERSONA_RECOVERY_REQUEST_ENABLED --repo OWNER/REPO --body true`,
or an organization value with the intended repository visibility. Inspect both
scopes when diagnosing an override.
Required persona-review and gatekeeper contexts are unchanged.
Gate summaries still warn when an exact-head review has not been dispatched;
disabling this optional request job does not suppress missing-review warnings.
When explicitly enabled, it accepts only `PERSONA_RECOVERY_RUNS_ON` equal to the
contracted JSON string `["self-hosted","persona-recovery"]`: ephemeral,
single-use JIT runners in the `persona-recovery` group, restricted to
`seankoji-com/.github/.github/workflows/reusable-persona-recovery-request.yml@refs/heads/main`.
The group must allow all repositories, including private ones, and run no other
workloads. Its runners need bash, curl, jq, gh and the runner's bundled Node.
Repository variables override organization variables, so any unset or different
value falls back to `ubuntu-latest` rather than selecting the shared pool.

For example, a deployment workflow can publish the standard release marker:

```yaml
jobs:
  mark-released:
    permissions:
      deployments: write
    uses: seankoji-com/.github/.github/workflows/released.yml@main
```

## Native CI result

[reusable-ci-result.yml](.github/workflows/reusable-ci-result.yml) checks native
job results within one workflow. It uses installed Python 3.10 or newer, makes
no API requests, checks out no code and needs no token permissions. A caller
named `gatekeeper` produces the required context `gatekeeper / all-checks-passed`.

List every retained validation job directly in both `needs` and `expected_jobs`,
including upstream jobs, rather than only the final jobs in a chain. An
intermediate job with `if: always()` can succeed after an upstream failure.
Use lowercase job IDs and pin the reusable to an approved immutable commit:

```yaml
jobs:
  # Define lint and test in THIS workflow.
  gatekeeper:
    needs: [lint, test]
    if: always()
    permissions: {}
    uses: seankoji-com/.github/.github/workflows/reusable-ci-result.yml@<approved-40-hex-commit>
    with:
      needs_json: ${{ toJSON(needs) }}
      expected_jobs: '["lint", "test"]'
      runner-json: '["ubuntu-latest"]' # Public callers only; private callers omit this override.
```

Keep the `gatekeeper` job ID and omit its `name` override. Its `if: always()`
is required. Every declared job must report `success`; failed, cancelled,
unknown or missing results fail the final job, and so does `skipped` unless
`skippable_jobs` lists the job and `skip_authority` confirms the skip. The
`expected_jobs` IDs must match the `needs` IDs exactly, with no empty list or
duplicates. A superseded
run whose dependencies are cancelled also fails, and can consume a final-job
runner on the old head. This is expected; cancellation must not produce a
passing gate.

The gate is only as trustworthy as the caller workflow. On `pull_request`
events GitHub runs the workflow from the pull request's head commit, so a pull
request that edits the caller can change `expected_jobs`, `skippable_jobs` or
the `changes` outputs and still turn the context green. Before relying on this
helper as a merge gate, require review of `.github/workflows/` changes, for
example with `CODEOWNERS` and a ruleset that requires code-owner review.

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `needs_json` | yes | | `${{ toJSON(needs) }}` from the caller. |
| `expected_jobs` | yes | | Literal JSON array of every direct dependency ID. |
| `skippable_jobs` | no | `'[]'` | Literal JSON array of `expected_jobs` IDs whose `skipped` result may pass when `skip_authority` confirms it. |
| `skip_authority` | when `skippable_jobs` is nonempty | `''` | ID of the job that decides which checks apply. It must be in `expected_jobs` and not in `skippable_jobs`. |
| `runner-json` | no | `''` | Runner labels; see [runner selection](#runner-selection). |

For path-scoped checks, prefer a job-level filter: a `changes` job computes
which checks apply, and each filtered job runs only when its flag is set. The
`changes` job must expose one output per filtered job, named exactly as that
job's ID, with the value `'true'` or `'false'`. List the filtered jobs in
`skippable_jobs`, name `changes` as `skip_authority`, and list `changes` in
`expected_jobs` but not in `skippable_jobs`. Pin a revision that declares
`skippable_jobs` and `skip_authority`; passing them to an older pin makes the
caller workflow fail to start with an undefined-input error:

```yaml
jobs:
  changes:
    outputs:
      docs: ${{ steps.filter.outputs.docs }} # 'true' or 'false'
    # Steps omitted: always runs; step `filter` sets docs.
  docs:
    needs: changes
    if: needs.changes.outputs.docs == 'true'
    # Steps omitted.
  test:
    # Steps omitted: always runs.
  gatekeeper:
    needs: [changes, docs, test]
    if: always()
    permissions: {}
    uses: seankoji-com/.github/.github/workflows/reusable-ci-result.yml@<approved-40-hex-commit>
    with:
      needs_json: ${{ toJSON(needs) }}
      expected_jobs: '["changes", "docs", "test"]'
      skippable_jobs: '["docs"]'
      skip_authority: changes
```

A declared job's `skipped` result passes only when `skip_authority` reported
`success` and its output named after that job is exactly the string `'false'`.
GitHub also reports `skipped` for a job whose upstream dependency failed or was
cancelled, so the declaration alone does not prove the skip was intended. Any
other case fails the job: the authority failed, was cancelled or skipped, or
its output is missing, `'true'` or any other value. The final job reads only
those outputs, compares them with `'false'` and never logs them. Every
`skippable_jobs` ID must appear in `expected_jobs`, and the list must hold
distinct lowercase IDs. A declared job that fails or is cancelled still fails
the final job. Without a `changes` job, return a successful explicit no-op
inside the validation job when nothing applies.

Make each filtered job depend only on `changes` and other jobs that always run,
and list every one of those upstream jobs in `expected_jobs`. The authority's
`'false'` confirms only that the filter did not require the job; it does not
cover a different upstream job left out of `needs` that also failed.

Avoid workflow-level `paths` or `paths-ignore` filters that suppress the
required producer. Do not conditionally skip the final job. Retained
validations must propagate failures, without masking them
through `continue-on-error`. The helper cannot detect a job omitted from both
lists, masked failures, or checks in another workflow. Move those checks into
the same dependency graph or require their contexts separately in effective rules.

`toJSON(needs)` includes job outputs. The helper reads only
`skip_authority`'s output for each declared skippable job, compares it with
`'false'`, and ignores every other output. The helper never prints outputs,
though the runner may still show step environment values in its own logs. Large
outputs can exceed the runner's environment-variable limit, roughly
128 KiB for one string on Linux, before the helper starts. Keep large data in
artifacts rather than dependency outputs.

This reusable is additive. Existing callers of `reusable-pr-gatekeeper.yml`
keep their behavior. Before migrating a repository, verify its complete native
producer, immutable pin and separately required persona producer/rule. Drain
legacy publishers and use a fresh head before retiring that repository's old
gatekeeper caller. Do not leave a REST evaluator or another Actions job posting
the same required CI context. Keep the legacy template for unmigrated callers,
and ensure distribution/audit knows which producer each repository uses.

The native helper alone does not prove the five-minute CI target. Measure the
complete retained dependency path, runner setup and final-job execution with
queues reported separately. Persona review remains a separate required gate.
The existing advisory scanner runtime needs its own bounded, blocking migration.

## Required persona review

The additive [persona status action](docs/persona-status.md) publishes a separate
review context from authoritative GitHub reviews. It is inactive until a trusted
controller calls an immutable revision and a verified per-repository rollout
establishes its App source binding, event delivery and required-ref behavior.
Adding it does not change the legacy combined gate described below.

Use the [local gatekeeper caller](.github/workflows/call-reusable-pr-gatekeeper.yml)
as the trigger and permissions reference. Its `head_sha` input identifies the
commit to evaluate; retain the PR seed, workflow completion and review-event
triggers when adapting it.

Pass `sweep: event` with that caller's per-head admission group. Each job then
evaluates only its own head, that head's merge refs and every PR sharing it.
`event` always requires CI registration and ignores `reconcile_open`. The
default, `sweep: open`, keeps the older behaviour: `reconcile_open` (default
`false`) decides whether to require CI registration and re-evaluate every open
PR on every event. Use `event` only with
per-head admission. A repo-wide admission group can replace one PR's pending
refresh with another PR's, and nothing would then re-evaluate the first PR. In
`event` mode the job fails before evaluation without a 40-hex `head_sha`.

A repository with the Pull Requests feature disabled answers 404 for `/pulls`.
The evaluator reads that as no open PR, after the repository's
`has_pull_requests` confirms the feature is off, so push heads there still get
a verdict. Any other 404 and every other read failure keeps failing closed.

The gate also waits for the latest Actions runs on the evaluated commit,
including queued workflows that have not created job checks yet. It cannot
detect a test workflow that was never triggered; keep required test callers
and their trigger coverage under repository review.

The retired OpenCode greenkeeper signal is the one exception. Its reusable
workflow and callers were removed in November 2026; the exception remains so
pull-request heads that still carry a signal run from before the retirement do
not turn red. The signal checked no code, and its caller kept only the newest
signal per pull request and cancelled the rest. Runs of
`.github/workflows/call-reusable-opencode-greenkeeper.yml` from the events that
caller declared (`pull_request_target`, `pull_request_review` and
`workflow_run`) are left out of the gate, with their job check runs and any
check suite that no gated run shares. They never hold or fail the gate, in any
state. A run of that path from any other event is gated: a `cancelled` run
passes as superseded, any other unsuccessful conclusion blocks and it holds the
gate while running. A cancelled GitHub Actions check run named exactly
`OpenCode greenkeeper signal / signal` also passes.
`required_workflows` cannot declare those three events, so the exception cannot
hide a declared requirement.

When the Actions variable `PERSONA_REVIEW_REQUIRED` is `true`, the gate also
requires an approval from Grumpy Engineer on the current PR head. Missing,
stale, dismissed, and unreadable reviews block merging. Requested changes
remain blocking. Review execution is managed by the private control plane;
this public workflow reads only reviews in its calling repository. Submitted,
edited, and dismissed reviews trigger a read-only signal, followed by a gate
evaluation from the default branch. The private reconciler recovers missed events.

## Security boundary

This repository is intentionally public. It must not contain credentials,
secret-manager item identifiers, private service addresses, private hostnames,
personal data, or incident logs. Store private operator records in the private
control-plane archive instead.

Report vulnerabilities using the instructions in [SECURITY.md](SECURITY.md).

## Validation

With Python 3, Node.js and Bash installed and the test dependency set up
(`python3 -m pip install -r requirements-test.txt`), run from the repository
root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Pinned tool versions are refreshed automatically; see
[docs/tool-pins.md](docs/tool-pins.md).
