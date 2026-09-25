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
- `released.yml`

- [Node CI](.github/workflows/reusable-node-ci.yml) accepts `pnpm`, `npm`, or
  `yarn`. An existing `.nvmrc` takes precedence over the `node-version` input.
- [Docker builds](.github/workflows/reusable-docker-build-push.yml) require
  `image-name`. Cache scope defaults to that image name; use `cache-scope` for
  multiple variants. Scope must be nonempty and contain only letters, digits,
  dot, underscore, slash, colon, `@` or hyphen.
- [ShellSpec](.github/workflows/reusable-shellspec.yml) uses a pinned version on
  a GitHub-hosted runner with zsh available. The caller's `.shellspec` selects
  the shell, for example `--shell /bin/zsh`. The workflow needs `contents: read`.

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

With Python 3, Node.js, Bash and PyYAML (`pip install PyYAML==6.0.2`) installed,
run from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```
