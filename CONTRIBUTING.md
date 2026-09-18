# Contributing to seankoji-com projects

Thank you for contributing. These are the organization defaults. A repository's
own contributing guide takes precedence when it has one.

## Before opening an issue

- Search existing issues and discussions.
- Use the repository's issue forms when available.
- Remove credentials, personal data, private hostnames, and private logs.
- Report vulnerabilities privately as described in the repository's security
  policy. Do not open a public issue with exploit details.

## Pull requests

- Keep each pull request focused on one change.
- Link the issue or discussion that explains the problem when one exists.
- Explain the behavior change and how you tested it.
- Add or update tests for changed behavior.
- Update user and operator documentation when the change affects them.
- Keep generated files, dependency changes, and unrelated formatting out of the
  diff unless they are part of the change.

Automated checks and automated review help maintainers, but they do not replace
human judgment. Address findings that apply and explain deliberate exceptions.

## Shared GitHub Actions workflows

Reusable workflows live in `.github/workflows/` in this repository. A consuming
repository must add a small local caller because GitHub does not inherit or run
Actions workflows from an organization `.github` repository automatically.
