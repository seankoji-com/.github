#!/usr/bin/env python3
"""Unit tests for scripts/agent_readiness.py.

Stdlib only, no network. Every test builds a throwaway git repo in a tmpdir so
the checks are exercised against real `git ls-files` / `git check-attr`
output — the same surface CI sees — rather than a mocked file list.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_readiness as ar  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, env=GIT_ENV,
                          capture_output=True, text=True).stdout


def make_repo(root: Path, files: dict[str, str], config: dict | None = None) -> Path:
    """A committed git repo. Config defaults to always-scan so the small-repo
    skip does not swallow every fixture."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    cfg = {"always": True}
    cfg.update(config or {})
    files = dict(files)
    files.setdefault(ar.CONFIG_FILE, json.dumps(cfg))
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    return root


def lines(n: int, prefix: str = "x = 1") -> str:
    return "".join(f"{prefix}  # {i}\n" for i in range(n))


def checks(report: ar.RepoReport, name: str) -> list[ar.Finding]:
    return [f for f in report.findings if f.check == name]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ar-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, files, config=None, name="fixture") -> Path:
        return make_repo(self.root / name, files, config)

    def audit(self, files, config=None) -> ar.RepoReport:
        return ar.audit_repo(self.repo(files, config))


class TestContextFiles(Base):
    def test_split_when_neither_imports_the_other(self):
        rep = self.audit({"CLAUDE.md": "# claude\nrules a\n", "AGENTS.md": "# agents\nrules b\n"})
        found = checks(rep, "context-split")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "high")
        self.assertEqual(found[0].subject, "root")

    def test_import_resolves_split_and_sums_budget(self):
        rep = self.audit({"CLAUDE.md": "@AGENTS.md\nclaude-only note\n", "AGENTS.md": "# agents\n"})
        self.assertEqual(checks(rep, "context-split"), [])
        self.assertEqual(checks(rep, "context-duplicate"), [])

    def test_identical_copies_are_a_duplicate(self):
        rep = self.audit({"CLAUDE.md": "same\n", "AGENTS.md": "same\n"})
        found = checks(rep, "context-duplicate")
        self.assertEqual([f.severity for f in found], ["medium"])

    def test_budget_counts_transitive_imports_and_ignores_home_imports(self):
        big = "words " * 2500  # 15000 chars ≈ 3750 tokens; two copies land between 1x and 2x budget
        rep = self.audit(
            {"CLAUDE.md": "@AGENTS.md\n@~/.claude/private.md\n", "AGENTS.md": "@docs/more.md\n" + big,
             "docs/more.md": big},
            {"context_budget_tokens": 4000},
        )
        found = checks(rep, "context-budget")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "medium")
        self.assertGreater(found[0].metric, 7000)
        self.assertIn("docs/more.md", found[0].message)
        self.assertNotIn("private.md", found[0].message)

    def test_budget_high_at_double(self):
        rep = self.audit({"AGENTS.md": "w " * 9000}, {"context_budget_tokens": 2000})
        self.assertEqual([f.severity for f in checks(rep, "context-budget")], ["high"])

    def test_nested_package_guides_are_checked_too(self):
        rep = self.audit({"AGENTS.md": "root\n", "apps/bot/CLAUDE.md": "a\n", "apps/bot/AGENTS.md": "b\n"})
        self.assertEqual([f.subject for f in checks(rep, "context-split")], ["apps/bot"])

    def test_context_missing_only_with_real_code(self):
        self.assertEqual(checks(self.audit({"a.py": lines(100)}), "context-missing"), [])
        rep = self.audit({"a.py": lines(400)})
        self.assertEqual([f.severity for f in checks(rep, "context-missing")], ["low"])


class TestFiles(Base):
    def test_monster_file_thresholds_by_kind(self):
        rep = self.audit({
            "AGENTS.md": "g\n", "src/big.py": lines(801), "src/ok.py": lines(800),
            "tests/test_big.py": lines(801), "tests/test_huge.py": lines(1501),
        })
        found = {f.subject: f for f in checks(rep, "monster-files")}
        self.assertEqual(set(found), {"src/big.py", "tests/test_huge.py"})
        self.assertEqual(found["src/big.py"].severity, "low")
        self.assertEqual(found["src/big.py"].metric, 801)

    def test_generated_vendored_and_ignored_paths_are_invisible(self):
        rep = self.audit(
            {"AGENTS.md": "g\n", "vendor/x.js": lines(2000), "a.min.js": lines(2000),
             "dist/types.d.ts": lines(2000), "node_modules/p/i.js": lines(2000),
             "fixtures-data/gen.py": lines(2000), "src/real.py": lines(10),
             "src/ambient.d.ts": lines(900)},
            {"ignore": ["fixtures-data/**"]},
        )
        # A hand-written declaration file is source, not generated.
        self.assertEqual([f.subject for f in checks(rep, "monster-files")], ["src/ambient.d.ts"])

    def test_tracked_blob_and_scratch(self):
        rep = self.audit({"AGENTS.md": "g\n", "data/dump.json": "[1]\n" * 200_000,
                          "GOAL.md": "spine\n", "notes.bak": "x\n"})
        self.assertEqual([f.subject for f in checks(rep, "tracked-blobs")], ["data/dump.json"])
        self.assertEqual({f.subject for f in checks(rep, "tracked-scratch")}, {"GOAL.md", "notes.bak"})

    def test_changelog_is_not_a_blob(self):
        rep = self.audit({"AGENTS.md": "g\n", "CHANGELOG.md": "- entry\n" * 200_000})
        self.assertEqual(checks(rep, "tracked-blobs"), [])

    def test_tests_missing_counts_capitalised_and_suffixed_tests(self):
        with_tests = self.audit({"AGENTS.md": "g\n", "Sources/A.swift": lines(600),
                                 "Tests/ATests.swift": lines(5)})
        self.assertEqual(checks(with_tests, "tests-missing"), [])
        without = ar.audit_repo(self.repo({"AGENTS.md": "g\n", "src/a.py": lines(600)}, name="two"))
        self.assertEqual([f.severity for f in checks(without, "tests-missing")], ["low"])

    def test_nested_guides_for_big_packages_only(self):
        rep = self.audit({"AGENTS.md": "g\n", "apps/web/index.ts": lines(2500),
                          "apps/tiny/index.ts": lines(50), "apps/bot/index.ts": lines(2500),
                          "apps/bot/AGENTS.md": "bot\n"})
        self.assertEqual([f.subject for f in checks(rep, "nested-guides")], ["apps/web"])

    def test_gitattributes_marks_lockfiles(self):
        unmarked = self.audit({"AGENTS.md": "g\n", "package-lock.json": "{}\n", "web/yarn.lock": "x\n"})
        self.assertEqual({f.subject for f in checks(unmarked, "gitattributes")},
                         {"package-lock.json", "web/yarn.lock"})
        marked = ar.audit_repo(self.repo(
            {"AGENTS.md": "g\n", "package-lock.json": "{}\n", "web/yarn.lock": "x\n",
             ".gitattributes": "package-lock.json linguist-generated -diff\n**/yarn.lock linguist-generated\n"},
            name="marked"))
        self.assertEqual(checks(marked, "gitattributes"), [])


class TestDocs(Base):
    def test_broken_link_is_medium_backtick_path_is_low(self):
        rep = self.audit({"AGENTS.md": "g\n", "scripts/run.sh": "#!/bin/sh\n",
                          "README.md": "[a](docs/missing.md) and `scripts/gone.sh` and `other-repo/x.py`\n"})
        self.assertEqual([f.subject for f in checks(rep, "doc-drift")], ["→docs/missing.md"])
        path = checks(rep, "path-drift")
        self.assertEqual([(f.subject, f.severity) for f in path], [("→scripts/gone.sh", "low")])
        self.assertIn("README.md mentions it", path[0].message)

    def test_resolution_rules_avoid_false_positives(self):
        rep = self.audit({
            "AGENTS.md": "g\n", "scripts/a.py": "\n", "docs/guide.md": "\n", "docs/sub/index.md": "\n",
            "docs-site/research/x.md": "\n", "docs-site/index.md": "\n",
            "changelog.d/README.md": "\n", "src/app/(booking)/page.tsx": "\n",
            "docs/notes.md": "\n".join([
                "[line](../scripts/a.py:42)", "[range](../scripts/a.py:10-20)",
                "[root-relative](scripts/a.py)", "[no-ext](guide)", "[dir](sub)",
                "[anchor](#heading)", "[url](https://x.invalid/a.md)", "[mail](mailto:a@b.c)",
                "[tmpl](<path-to-file>)", "[glob](src/*.py)", "[paren](../src/app/(booking)/page.tsx)",
                "[fragment](../changelog.d/2026-01-01-thing.md)", "[outside](../../elsewhere.md)",
            ]),
            "docs-site/research/y.md": "[site](/research/x) [home](/) [idx](/index)\n",
            ".github/workflows/ci.yml": "\n", ".opencodereview/rule.json": "{}\n",
            "panel/README.md": "See `.github/workflows/ci.yml` and [rule](.opencodereview/rule.json)\n",
        })
        self.assertEqual(checks(rep, "doc-drift"), [], [f.message for f in rep.findings])

    def test_gitignored_targets_are_intentionally_absent(self):
        rep = self.audit({"AGENTS.md": "g\n", ".gitignore": ".env\n*.log\n.claude/settings.local.json\n",
                          "README.md": "Copy [.env](.env), watch `logs/debug.log`, edit "
                                       "[local](.claude/settings.local.json), and see [gone](docs/gone.md)\n",
                          "logs/.keep": ""})
        self.assertEqual([f.subject for f in checks(rep, "doc-drift")], ["→docs/gone.md"])
        self.assertEqual(checks(rep, "path-drift"), [])

    def test_archive_and_changelog_docs_are_skipped(self):
        rep = self.audit({"AGENTS.md": "g\n", "docs/archive/old.md": "[x](gone.md)\n",
                          "CHANGELOG.md": "[x](gone.md)\n"})
        self.assertEqual(checks(rep, "doc-drift"), [])

    def test_stale_command_is_high_and_wins_over_plain_docs(self):
        rep = self.audit({"AGENTS.md": "g\n", "scripts/x.py": "\n",
                          ".claude/commands/db.md": "Read [schema](../../scripts/gone.py) first, "
                                                    "then `scripts/also-gone.py`\n",
                          "README.md": "[schema](scripts/gone.py)\n"})
        stale = checks(rep, "stale-commands")
        self.assertEqual([(f.subject, f.severity) for f in stale], [("→../../scripts/gone.py", "high")])
        self.assertEqual([f.subject for f in checks(rep, "doc-drift")], ["→scripts/gone.py"])
        self.assertEqual([f.severity for f in checks(rep, "path-drift")], ["low"])

    def test_one_target_many_docs_is_one_stable_finding(self):
        files = {"AGENTS.md": "g\n", "stacks/keep.yml": "\n",
                 **{f"docs/s{i}.md": "[c](../stacks/gone.yml)\n" for i in range(4)}}
        four = self.audit(files)
        found = checks(four, "doc-drift")
        self.assertEqual([f.subject for f in found], ["→../stacks/gone.yml"])
        self.assertIn("docs/s0.md, docs/s1.md, docs/s2.md, docs/s3.md links to it", found[0].message)
        # Removing three of the four links keeps the same key: not a regression.
        two = ar.audit_repo(self.repo({k: v for k, v in files.items() if k not in ("docs/s2.md", "docs/s3.md")},
                                      name="fewer"))
        self.assertEqual([f.subject for f in checks(two, "doc-drift")], ["→../stacks/gone.yml"])

    def test_ellipsis_and_quoted_syntax_are_not_links(self):
        rep = self.audit({"AGENTS.md": "g\n", "docs/a.md": "use `![](...)` and [x](...) and [y](a/b/...)\n"})
        self.assertEqual(checks(rep, "doc-drift"), [])


class TestWaiversSkipAndBlocked(Base):
    def test_waiver_suppresses_until_expiry(self):
        files = {"CLAUDE.md": "a\n", "AGENTS.md": "b\n"}
        waiver = [{"check": "context-split", "subject": "root", "until": "2030-01-01", "reason": "wip"}]
        live = ar.audit_repo(self.repo(files, {"waivers": waiver}), today=date(2026, 9, 15))
        self.assertEqual(checks(live, "context-split"), [])
        self.assertEqual(checks(live, "waiver-expired"), [])
        expired = ar.audit_repo(self.repo(files, {"waivers": waiver}, name="two"), today=date(2031, 1, 1))
        self.assertEqual(len(checks(expired, "context-split")), 1)
        self.assertEqual(len(checks(expired, "waiver-expired")), 1)

    def test_small_repo_is_skipped_unless_always(self):
        skipped = ar.audit_repo(self.repo({"CLAUDE.md": "a\n", "AGENTS.md": "b\n"}, {"always": False}))
        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(skipped.findings, [])
        self.assertIn("source_files", skipped.stats)
        scanned = ar.audit_repo(self.repo({"CLAUDE.md": "a\n", "AGENTS.md": "b\n"}, name="two"))
        self.assertEqual(scanned.status, "fail")

    def test_bad_config_is_a_finding_and_never_enforces(self):
        bad = self.repo({ar.CONFIG_FILE: '{"enforce": true, "always": true, "oops": ', "AGENTS.md": "g\n"})
        rep = ar.audit_repo(bad)
        self.assertFalse(rep.blocked)
        self.assertFalse(rep.enforce)
        self.assertEqual([f.severity for f in checks(rep, "config-invalid")], ["high"])

    def test_crash_and_empty_listing_read_as_blocked(self):
        good = self.repo({"AGENTS.md": "g\n"})
        with patch.object(ar, "check_docs", side_effect=RuntimeError("boom")):
            rep = ar.audit_repo(good)
        self.assertTrue(rep.blocked)
        self.assertEqual(ar.exit_code([rep], "low", gate=False), 2)
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        rep = ar.audit_repo(empty)
        self.assertTrue(rep.blocked, "an empty ls-files must not skip as green")
        self.assertIn("returned nothing", rep.findings[0].message)

    def test_inventory_counts(self):
        rep = self.audit({"AGENTS.md": "g\n", ".claude/commands/a.md": "\n", ".claude/commands/b.md": "\n",
                          ".claude/skills/s/SKILL.md": "\n", ".claude/hooks/h.sh": "\n",
                          ".mcp.json": json.dumps({"mcpServers": {"a": {}, "b": {}}}),
                          ".claude/settings.json": json.dumps({"hooks": {"PreToolUse": [{}, {}]}})})
        st = rep.stats
        self.assertEqual((st["commands"], st["skills"], st["hook_files"], st["mcp_servers"], st["settings_hooks"]),
                         (2, 1, 1, 2, 2))


class TestRatchetAndGate(Base):
    def report(self, findings, enforce=False):
        rep = ar.RepoReport("r", enforce=enforce)
        rep.findings = findings
        return rep

    def test_existing_findings_suppressed_new_ones_kept(self):
        old = ar.Finding("doc-drift", "medium", "README.md→a.md", "old", repo="r")
        base = ar.to_json([self.report([old])])
        new = ar.Finding("doc-drift", "medium", "README.md→b.md", "new", repo="r")
        out = ar.apply_baseline([self.report([ar.Finding("doc-drift", "medium", "README.md→a.md", "old", repo="r"), new])], base)
        self.assertEqual([f.subject for f in out[0].findings], ["README.md→b.md"])

    def test_metric_growth_beyond_tolerance_is_promoted(self):
        base = ar.to_json([self.report([ar.Finding("monster-files", "low", "big.py", "m", repo="r", metric=1000)])])
        small = ar.Finding("monster-files", "low", "big.py", "m", repo="r", metric=1050)
        self.assertEqual(ar.apply_baseline([self.report([small])], base)[0].findings, [])
        grown = ar.Finding("monster-files", "low", "big.py", "m", repo="r", metric=1200)
        kept = ar.apply_baseline([self.report([grown])], base)[0].findings
        self.assertEqual([(f.severity, f.subject) for f in kept], [("medium", "big.py")])
        self.assertIn("grew from 1000", kept[0].message)

    def test_new_monster_file_is_medium_and_worse_severity_surfaces(self):
        base = ar.to_json([self.report([ar.Finding("context-budget", "medium", "root", "m", repo="r", metric=4100)])])
        new_monster = ar.Finding("monster-files", "low", "new.py", "m", repo="r", metric=900)
        worse = ar.Finding("context-budget", "high", "root", "m", repo="r", metric=9000)
        kept = ar.apply_baseline([self.report([new_monster, worse])], base)[0].findings
        self.assertEqual([(f.check, f.severity) for f in kept],
                         [("monster-files", "medium"), ("context-budget", "high")])

    def test_gate_respects_enforce(self):
        finding = ar.Finding("context-split", "high", "root", "m", repo="r")
        self.assertEqual(ar.exit_code([self.report([finding])], "medium", gate=True), 0)
        self.assertEqual(ar.exit_code([self.report([finding], enforce=True)], "medium", gate=True), 1)
        self.assertEqual(ar.exit_code([self.report([finding])], "medium", gate=False), 1)
        low = ar.Finding("monster-files", "low", "x", "m", repo="r")
        self.assertEqual(ar.exit_code([self.report([low], enforce=True)], "medium", gate=True), 0)

    def test_blocked_only_fails_the_gate_for_enforced_repos(self):
        blocked = ar.Finding("setup", "critical", "repo", "crash", repo="r", blocked=True)
        self.assertEqual(ar.exit_code([self.report([blocked])], "medium", gate=True), 0)
        self.assertEqual(ar.exit_code([self.report([blocked], enforce=True)], "medium", gate=True), 2)
        self.assertEqual(ar.exit_code([self.report([blocked])], "medium", gate=False), 2)

    def test_rename_of_monster_file_is_not_new(self):
        base = ar.to_json([self.report([ar.Finding("monster-files", "low", "src/old.py", "m", repo="r", metric=1000)])])
        moved = ar.Finding("monster-files", "low", "src/new.py", "m", repo="r", metric=1000)
        self.assertEqual(ar.apply_baseline([self.report([moved])], base)[0].findings[0].subject, "src/new.py")
        kept = ar.apply_baseline([self.report([moved])], base, renames={"src/new.py": "src/old.py"})[0].findings
        self.assertEqual(kept, [])

    def test_per_repo_growth_tolerance_is_honoured(self):
        base = ar.to_json([self.report([ar.Finding("monster-files", "low", "big.py", "m", repo="r", metric=1000)])])
        grown = ar.Finding("monster-files", "low", "big.py", "m", repo="r", metric=1400)
        lenient = self.report([grown])
        lenient.tolerance = 0.5
        self.assertEqual(ar.apply_baseline([lenient], base)[0].findings, [])


class TestCli(Base):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ar.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_json_baseline_and_summary_round_trip(self):
        root = self.repo({"CLAUDE.md": "a\n", "AGENTS.md": "b\n", "src/x.py": lines(10)})
        rc, out, _ = self.run_main("--root", str(root), "--json", "--quiet")
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertEqual(doc["repos"]["fixture"]["status"], "fail")
        base = self.root / "base.json"
        base.write_text(out)
        summary = self.root / "summary.md"
        rc, _, err = self.run_main("--root", str(root), "--baseline", str(base), "--summary-md", str(summary))
        self.assertEqual(rc, 0)
        self.assertIn("0 regression(s)", err)
        self.assertIn("No regressions vs base.", summary.read_text())
        self.assertIn("advisory", summary.read_text())

    def test_fleet_mode_and_not_a_repo(self):
        self.repo({"CLAUDE.md": "a\n", "AGENTS.md": "b\n"}, name="one")
        self.repo({"AGENTS.md": "fine\n"}, name="two")
        rc, out, _ = self.run_main("--fleet", str(self.root), "--json", "--quiet")
        self.assertEqual(rc, 1)
        doc = json.loads(out)["repos"]
        self.assertEqual({k: v["status"] for k, v in doc.items()}, {"one": "fail", "two": "pass"})
        rc, _, err = self.run_main("--root", str(self.root / "nope"))
        self.assertEqual(rc, 2)

    def test_github_output_and_gate_flag(self):
        root = self.repo({"CLAUDE.md": "a\n", "AGENTS.md": "b\n"})
        gh_out = self.root / "gh.txt"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}):
            rc, _, _ = self.run_main("--root", str(root), "--gate", "--github-output", "--quiet")
        self.assertEqual(rc, 0)  # advisory: enforce is not set
        self.assertIn("PROBLEMS<<AGENT_READINESS_EOF", gh_out.read_text())
        self.assertIn("fixture: [context-split] root:", gh_out.read_text())

    def test_ci_mode_ratchets_against_merge_base(self):
        # main: an existing doc-drift finding. branch: keeps it and adds a
        # split. Only the split is a regression; the gate honours enforce.
        root = self.repo({"AGENTS.md": "g\n", "README.md": "[x](gone.md)\n", "src/a.py": lines(10)})
        git(root, "checkout", "-q", "-b", "feature")
        (root / "CLAUDE.md").write_text("different\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "add split")
        # `origin` must exist for the fetch; point it at the repo itself.
        git(root, "remote", "add", "origin", str(root))
        summary = self.root / "summary.md"
        env = {"GITHUB_BASE_REF": "main", "GITHUB_REPOSITORY": "seankoji-com/fixture",
               "GITHUB_STEP_SUMMARY": str(summary)}
        with patch.dict(os.environ, env):
            rc, _, err = self.run_main("--root", str(root), "--ci")
        self.assertEqual(rc, 0, err)
        self.assertIn("1 regression(s)", err)
        self.assertIn("context-split", err)
        self.assertNotIn("doc-drift", err)
        self.assertIn("regressions vs base", summary.read_text())
        self.assertEqual(git(root, "worktree", "list").count("\n"), 1, "base worktree not cleaned up")
        (root / ar.CONFIG_FILE).write_text(json.dumps({"always": True, "enforce": True}))
        git(root, "commit", "-qam", "enforce")
        with patch.dict(os.environ, env):
            rc, _, err = self.run_main("--root", str(root), "--ci")
        self.assertEqual(rc, 1)
        self.assertIn("::error::", err)

    def test_ci_mode_without_base_ref_is_absolute_and_advisory(self):
        root = self.repo({"CLAUDE.md": "a\n", "AGENTS.md": "b\n"})
        with patch.dict(os.environ, {"GITHUB_BASE_REF": "", "GITHUB_REPOSITORY": "o/fixture"}):
            rc, _, err = self.run_main("--root", str(root), "--ci")
        self.assertEqual(rc, 0)
        self.assertIn("1 finding(s)", err)


class TestSelf(unittest.TestCase):
    def test_script_stays_under_its_own_limit(self):
        path = Path(ar.__file__)
        self.assertLessEqual(sum(1 for _ in open(path, "rb")), ar.DEFAULT_CONFIG["max_code_lines"],
                             "agent_readiness.py is a monster file by its own rule")


if __name__ == "__main__":
    unittest.main()
