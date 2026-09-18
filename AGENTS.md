# Agent guide

This repository is public and supplies organization-wide GitHub defaults.

## Safety

- Never add credentials, secret-manager identifiers, private hostnames, private
  network addresses, personal data, or incident logs.
- Keep issue and pull-request templates under `.github/` so GitHub can inherit
  them.
- Do not remove a local workflow caller from another repository unless the
  workflow is retired. GitHub does not inherit Actions workflows.
- Do not remove local `CODEOWNERS`, `README.md`, or `LICENSE` files. GitHub does
  not inherit them.

## Changes

- Treat reusable workflow changes as organization-wide changes.
- Pin third-party Actions by commit SHA and keep the release tag in a comment.
- Add tests for script changes.
- Preserve project-specific local overrides when their guidance differs from
  the organization default.
