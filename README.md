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

Persona recovery handles the CI App private key, so it never uses the shared
pool. Per-head request jobs are disabled by default to avoid hosted charges on
every PR head. Normal webhook review dispatch and the private controller's
externally monitored sweep provide recovery. Set
`PERSONA_RECOVERY_REQUEST_ENABLED=true` only to opt back into per-head requests;
verify the sweep and its freshness alarm before adopting this default.
Required persona-review and gatekeeper contexts are unchanged.
When explicitly enabled, it accepts only `PERSONA_RECOVERY_RUNS_ON` equal to the contracted JSON
string `["self-hosted","persona-recovery"]`: ephemeral, single-use JIT runners
in the `persona-recovery` group, restricted to
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

## Required persona review

Use the [local gatekeeper caller](.github/workflows/call-reusable-pr-gatekeeper.yml)
as the trigger and permissions reference. Its `head_sha` input identifies the
commit to evaluate; retain the PR seed, workflow completion and review-event
triggers when adapting it.

The gate also waits for the latest Actions runs on the evaluated commit,
including queued workflows that have not created job checks yet. It cannot
detect a test workflow that was never triggered; keep required test callers
and their trigger coverage under repository review.

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
