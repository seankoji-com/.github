# Pinned tool versions

Every tool version this repository installs is pinned, and every pin has an
automated update path. Nothing here merges itself.

| Pin | Defined in | Updated by |
| --- | --- | --- |
| GitHub Actions (by SHA) | `uses:` lines | Dependabot `github-actions` |
| `PyYAML` | `requirements-test.txt` | Dependabot `pip` |
| `PIN_PIP`, `PIN_SEMGREP`, `PIN_SEMGREP_OTEL`, `PIN_ZIZMOR` | `env:` in `reusable-static-analysis.yml` | `update-tool-pins.yml` |
| `PIN_ACTIONLINT`, `PIN_ACTIONLINT_SHA256` | `public-test.yml` | `update-tool-pins.yml` |

Each value is defined once. The scanner cache keys and install commands read
the `env:` values, so a bump invalidates the pip download cache automatically.

## Why the scanner pins are not in a requirements file

`reusable-static-analysis.yml` runs in consuming repositories, which do not have
this repository's files. Reading a requirements file would need a second
checkout of this repository on every run, at a ref that can drift from the
workflow's own commit. Keeping the pins in the workflow means a caller pinned to
a commit gets exactly the scanner versions reviewed with that commit.

## Weekly update PR

`update-tool-pins.yml` runs on Mondays (and on demand from the default branch)
as two jobs, so freshly released scanner code never runs beside a write token:

**`resolve`** (read-only)

1. `scripts/update_tool_pins.py` reads the latest stable release from PyPI or
   GitHub and rewrites the pins in its checkout. It skips pre-releases, yanked
   files and releases that need a Python newer than 3.10 (the oldest
   self-hosted interpreter). It never lowers a pin.
2. `PIN_SEMGREP_OTEL` is taken from the `~=` requirement of the new Semgrep
   release. If Semgrep stops pinning one OpenTelemetry release, the Semgrep bump
   is skipped and the run fails with the reason.
3. `PIN_ACTIONLINT_SHA256` is the `linux_amd64` digest from the release's
   `checksums.txt`, fetched over HTTPS. It pins what was published; it does not
   authenticate the release.
4. The new scanners are installed under Python 3.10, pass `pip check`, and must
   emit the JSON shapes `reusable-static-analysis.yml` parses (a local one-rule
   Semgrep scan and a zizmor scan). The reusable workflow treats scanner
   failures as advisory, so this is the only gate.
5. A JSON report of the bumps is uploaded.

**`publish`** (write access, runs no scanner)

6. `--apply-report` re-validates every entry (a known pin, a plain version or
   digest, the old value still current) and rewrites only those lines.
7. It force-pushes `automation/tool-pins` when the content differs from the
   branch, opens or updates one PR, and dispatches `public-test.yml` on the
   branch.

If a tool could not be resolved, the other bumps are still published for a new
PR, an already open PR is left as it is, nothing is closed, and the job fails so
the error is visible.

Do not push to `automation/tool-pins`. If its PR shows no checks, close and
reopen it: events created by `GITHUB_TOKEN` do not trigger `pull_request`
workflows.

**One-time setting:** the repository setting *Actions → General → Allow GitHub
Actions to create and approve pull requests* must be on. Without it the branch
is still pushed and the job fails with a compare link.

## Running locally

```sh
python3 scripts/update_tool_pins.py --check   # list available bumps, write nothing, exit 1 if any
python3 scripts/update_tool_pins.py           # apply them
python3 scripts/update_tool_pins.py --print-pins
```

To add a pin, define it once as a quoted `NAME: "value"` line, add it to
`PIN_NAMES` and `plan()` in the script, and add tests.
