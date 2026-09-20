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
- `reusable-docker-build-push.yml`
- `reusable-static-analysis.yml`
- `reusable-agent-readiness.yml`
- `reusable-pr-gatekeeper.yml`
- `reusable-dependabot-automerge.yml`
- `reusable-issue-triage.yml`
- `reusable-link-check.yml`
- `released.yml`

For example, a deployment workflow can publish the standard release marker:

```yaml
jobs:
  mark-released:
    permissions:
      deployments: write
    uses: seankoji-com/.github/.github/workflows/released.yml@main
```

## Required persona review

When the Actions variable `PERSONA_REVIEW_REQUIRED` is `true`, the gate also
requires an approval from Grumpy Engineer on the current PR head. Missing,
stale, dismissed, and unreadable reviews block merging. Requested changes
remain blocking. Review execution is managed by the private control plane;
this public workflow reads only reviews in its calling repository.

## Security boundary

This repository is intentionally public. It must not contain credentials,
secret-manager item identifiers, private service addresses, private hostnames,
personal data, or incident logs. Store private operator records in the private
control-plane archive instead.

Report vulnerabilities using the instructions in [SECURITY.md](SECURITY.md).
