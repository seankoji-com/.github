#!/usr/bin/env python3
"""Bump the tool pins that Dependabot cannot see.

Dependabot covers GitHub Actions and requirements-test.txt. The pins below live
in workflow `env:` blocks, so this script refreshes them from PyPI and GitHub
releases. Stdlib only. It never commits, pushes or merges: the scheduled
workflow turns a diff into a reviewed pull request.

    update_tool_pins.py [--root DIR] [--check] [--report FILE] [--print-pins]
    update_tool_pins.py [--root DIR] --apply-report FILE

--apply-report rewrites pins from a report written by an earlier run, after
re-validating every entry. It needs no network, so the privileged publish job
can apply a report produced by a job that ran freshly installed scanners.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

SCANNERS = ".github/workflows/reusable-static-analysis.yml"
TESTS = ".github/workflows/public-test.yml"

# Oldest interpreter that runs these tools: the self-hosted pools ship 3.10.
MIN_PYTHON = (3, 10)

ALLOWED_HOSTS = {"pypi.org", "api.github.com", "github.com"}
# github.com release downloads redirect to these; every hop is checked.
REDIRECT_HOSTS = ALLOWED_HOSTS | {"release-assets.githubusercontent.com",
                                  "objects.githubusercontent.com"}
ACTIONLINT_REPO = "rhysd/actionlint"
OTEL_PACKAGES = ("opentelemetry-instrumentation-requests",
                 "opentelemetry-instrumentation-threading")

# Every pin this script owns, in the order it prints them.
PIN_NAMES = (
    (SCANNERS, "PIN_PIP"),
    (SCANNERS, "PIN_SEMGREP"),
    (SCANNERS, "PIN_SEMGREP_OTEL"),
    (SCANNERS, "PIN_ZIZMOR"),
    (TESTS, "PIN_ACTIONLINT"),
    (TESTS, "PIN_ACTIONLINT_SHA256"),
)

STABLE = re.compile(r"\d+(\.\d+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
OTEL_SPEC = re.compile(r"~=\s*(\d+\.\d+b\d+)")

Fetch = Callable[[str], bytes]


@dataclass
class Change:
    tool: str
    file: str
    name: str
    old: str
    new: str


def request_headers(url: str, environ: dict[str, str]) -> dict[str, str]:
    headers = {"User-Agent": "update-tool-pins"}
    token = environ.get("GITHUB_TOKEN", "")
    # Unauthenticated api.github.com calls share a low per-IP rate limit on hosted
    # runners. The token goes to that one host only, never to PyPI or a redirect.
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    return headers


def check_url(url: str, hosts: set[str]) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in hosts:
        raise ValueError(f"refusing to fetch {url}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, REDIRECT_HOSTS)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


def fetch(url: str) -> bytes:
    check_url(url, ALLOWED_HOSTS)
    request = urllib.request.Request(url, headers=request_headers(url, os.environ))
    opener = urllib.request.build_opener(_CheckedRedirects)
    with opener.open(request, timeout=30) as response:
        return response.read()


def _line(name: str) -> re.Pattern:
    # Matches `  NAME: "1.2.3"  # comment`; the quotes are kept on rewrite.
    return re.compile(
        r"^(?P<head>[ \t]*" + re.escape(name) + r":[ \t]*)(?P<q>[\"']?)"
        r"(?P<value>[^\"'\s#]+)(?P=q)(?P<tail>[ \t]*(?:#.*)?)$", re.M)


def read_pin(text: str, name: str) -> str:
    matches = list(_line(name).finditer(text))
    if len(matches) != 1:
        raise ValueError(f"{name}: expected exactly one definition, found {len(matches)}")
    return matches[0].group("value")


def set_pin(text: str, name: str, value: str) -> str:
    read_pin(text, name)  # exactly-one check
    return _line(name).sub(
        lambda m: f"{m.group('head')}{m.group('q')}{value}{m.group('q')}{m.group('tail')}",
        text, count=1)


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def python_ok(requires_python: str | None) -> bool:
    """False when a `>=`/`>` bound excludes MIN_PYTHON. Other clauses are ignored."""
    for clause in (requires_python or "").split(","):
        match = re.fullmatch(r"\s*(>=|>)\s*(\d+)\.(\d+)(?:\.\d+)?\s*", clause)
        if not match:
            continue
        bound = (int(match.group(2)), int(match.group(3)))
        if bound > MIN_PYTHON or (match.group(1) == ">" and bound == MIN_PYTHON):
            return False
    return True


def newest_stable(releases: dict) -> str:
    """Highest final release that is not yanked and still runs on MIN_PYTHON."""
    usable = [
        version for version, files in releases.items()
        if STABLE.fullmatch(version) and files
        and not any(f.get("yanked") for f in files)
        and all(python_ok(f.get("requires_python")) for f in files)
    ]
    if not usable:
        raise ValueError("no stable release supports Python %d.%d" % MIN_PYTHON)
    return max(usable, key=version_key)


def pypi_latest(package: str, get: Fetch) -> str:
    return newest_stable(json.loads(get(f"https://pypi.org/pypi/{package}/json"))["releases"])


def semgrep_otel(version: str, get: Fetch) -> str:
    """Instrumentation pin that semgrep itself asks for (`~=0.58b0`)."""
    info = json.loads(get(f"https://pypi.org/pypi/semgrep/{version}/json"))["info"]
    found = set()
    for requirement in info.get("requires_dist") or []:
        name = re.split(r"[\s;<>=~!(\[]", requirement, maxsplit=1)[0].lower()
        if name in OTEL_PACKAGES:
            match = OTEL_SPEC.search(requirement)
            found.add((name, match.group(1) if match else None))
    names = {name for name, _ in found}
    specs = {spec for _, spec in found}
    if names != set(OTEL_PACKAGES) or len(specs) != 1 or None in specs:
        raise ValueError(f"semgrep {version} does not pin one ~= OpenTelemetry release: {sorted(found, key=str)}")
    return specs.pop()


def actionlint_latest(get: Fetch) -> str:
    release = json.loads(get(f"https://api.github.com/repos/{ACTIONLINT_REPO}/releases/latest"))
    tag = release["tag_name"]
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v\d+(\.\d+)+", tag):
        raise ValueError(f"unexpected actionlint release {tag!r}")
    return tag[1:]


def actionlint_digest(version: str, get: Fetch) -> str:
    """sha256 of the linux_amd64 tarball, from the release's published checksums."""
    sums = get(f"https://github.com/{ACTIONLINT_REPO}/releases/download/v{version}/"
               f"actionlint_{version}_checksums.txt").decode()
    wanted = f"actionlint_{version}_linux_amd64.tar.gz"
    digests = [line.split()[0] for line in sums.splitlines()
               if len(line.split()) == 2 and line.split()[1] == wanted]
    if len(digests) != 1 or not SHA256.fullmatch(digests[0]):
        raise ValueError(f"no usable checksum for {wanted}")
    return digests[0]


def plan(root: Path, get: Fetch) -> tuple[list[Change], list[str]]:
    """Return (changes, errors). A tool that errors is left untouched."""
    texts = {path: (root / path).read_text() for path in {SCANNERS, TESTS}}

    def current(file: str, name: str) -> str:
        return read_pin(texts[file], name)

    changes: list[Change] = []
    errors: list[str] = []

    def attempt(tool: str, build: Callable[[], list[Change]]) -> None:
        try:
            changes.extend(build())
        except Exception as error:  # noqa: BLE001 - one tool failing must not hide the rest
            errors.append(f"{tool}: {error}")

    def simple(tool: str, package: str, name: str) -> list[Change]:
        old, new = current(SCANNERS, name), pypi_latest(package, get)
        return [Change(tool, SCANNERS, name, old, new)] if version_key(new) > version_key(old) else []

    def semgrep() -> list[Change]:
        old, new = current(SCANNERS, "PIN_SEMGREP"), pypi_latest("semgrep", get)
        if version_key(new) <= version_key(old):
            return []
        otel, old_otel = semgrep_otel(new, get), current(SCANNERS, "PIN_SEMGREP_OTEL")
        result = [Change("semgrep", SCANNERS, "PIN_SEMGREP", old, new)]
        if otel != old_otel:
            result.append(Change("semgrep", SCANNERS, "PIN_SEMGREP_OTEL", old_otel, otel))
        return result

    def actionlint() -> list[Change]:
        old = current(TESTS, "PIN_ACTIONLINT")
        version = actionlint_latest(get)
        if version_key(version) <= version_key(old):
            return []
        digest = actionlint_digest(version, get)
        return [Change("actionlint", TESTS, "PIN_ACTIONLINT", old, version),
                Change("actionlint", TESTS, "PIN_ACTIONLINT_SHA256",
                       current(TESTS, "PIN_ACTIONLINT_SHA256"), digest)]

    attempt("pip", lambda: simple("pip", "pip", "PIN_PIP"))
    attempt("semgrep", semgrep)
    attempt("zizmor", lambda: simple("zizmor", "zizmor", "PIN_ZIZMOR"))
    attempt("actionlint", actionlint)
    return changes, errors


def apply(root: Path, changes: list[Change]) -> None:
    for file in sorted({c.file for c in changes}):
        path = root / file
        text = path.read_text()
        for change in (c for c in changes if c.file == file):
            text = set_pin(text, change.name, change.new)
        path.write_text(text)


def validate_change(change: Change, root: Path) -> None:
    """Reject anything that is not a plain pin bump for a pin this script owns."""
    if (change.file, change.name) not in PIN_NAMES:
        raise ValueError(f"unknown pin {change.file}:{change.name}")
    pattern = SHA256 if change.name.endswith("_SHA256") else re.compile(r"\d+(\.\d+)*(b\d+)?")
    for value in (change.old, change.new):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"{change.name}: unexpected value {value!r}")
    if read_pin((root / change.file).read_text(), change.name) != change.old:
        raise ValueError(f"{change.name}: pin is no longer {change.old}; rerun the update")


def load_report(path: Path, root: Path) -> list[Change]:
    changes = [Change(**entry) for entry in json.loads(path.read_text())["changes"]]
    seen = set()
    for change in changes:
        validate_change(change, root)
        if (change.file, change.name) in seen:
            raise ValueError(f"{change.name}: listed twice")
        seen.add((change.file, change.name))
    return changes


def pins(root: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    result = {}
    for file, name in PIN_NAMES:
        texts.setdefault(file, (root / file).read_text())
        result[name] = read_pin(texts[file], name)
    return result


def main(argv: list[str] | None = None, get: Fetch = fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="report updates, write nothing, exit 1 if any")
    parser.add_argument("--report", type=Path, help="write {changes, errors} JSON here")
    parser.add_argument("--print-pins", action="store_true", help="print NAME=value for every pin and exit")
    parser.add_argument("--apply-report", type=Path, help="apply a validated report from an earlier run; no network")
    args = parser.parse_args(argv)

    if args.apply_report:
        try:
            changes = load_report(args.apply_report, args.root)
        except (ValueError, KeyError, TypeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        apply(args.root, changes)
        for change in changes:
            print(f"{change.tool}: {change.name} {change.old} -> {change.new}")
        return 0

    if args.print_pins:
        for name, value in pins(args.root).items():
            print(f"{name}={value}")
        return 0

    changes, errors = plan(args.root, get)
    if not args.check:
        apply(args.root, changes)
    if args.report:
        args.report.write_text(json.dumps(
            {"changes": [asdict(c) for c in changes], "errors": errors}, indent=2) + "\n")
    for change in changes:
        print(f"{change.tool}: {change.name} {change.old} -> {change.new}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 2
    return 1 if (args.check and changes) else 0


if __name__ == "__main__":
    sys.exit(main())
