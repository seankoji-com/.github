"""Exercise selected history against real shallow repositories; no network."""

import base64
import io
import json
import os
from pathlib import Path
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_agent_readiness import Base, GIT_ENV, ar, git, lines
from agent_readiness_git import prepare_ci_git


class SelectedHistoryTests(Base):
    def source(self):
        root = self.repo({"AGENTS.md": "rules\n", "src/old.py": lines(1000)},
                         config={"enforce": True})
        git(root, "config", "uploadpack.allowFilter", "true")
        git(root, "config", "uploadpack.allowAnySHA1InWant", "true")
        return root

    def commit(self, root, path, contents, message="change"):
        (root / path).write_text(contents)
        git(root, "add", "-A")
        git(root, "commit", "-qm", message)
        return git(root, "rev-parse", "HEAD").strip()

    def shallow(self, source, ref="feature"):
        target = self.root / "checkout"
        git(self.root, "clone", "-q", "--depth=1", "--filter=blob:none", "--no-tags",
            "--single-branch", "--branch", ref, source.as_uri(), str(target))
        self.assertEqual(git(target, "rev-parse", "--is-shallow-repository").strip(), "true")
        return target

    def run_ci(self, root):
        output = io.StringIO()
        git_env = {key: value for key, value in GIT_ENV.items() if key.startswith("GIT_")}
        with patch.dict(os.environ, {**git_env, "GITHUB_BASE_REF": "main",
                                     "GITHUB_REPOSITORY": "example/fixture",
                                     "AGENT_READINESS_GIT_TOKEN": ""}), redirect_stderr(output), redirect_stdout(output):
            rc = ar.run_ci(root, "medium")
        return rc, output.getvalue()

    def test_deep_divergence_rename_and_lagging_branch_preserve_exact_base(self):
        source = self.source()
        expected = git(source, "rev-parse", "HEAD").strip()
        git(source, "checkout", "-qb", "feature")
        git(source, "mv", "src/old.py", "src/new.py")
        git(source, "commit", "-qm", "rename old debt")
        for i in range(7):
            self.commit(source, "feature.txt", str(i))
        feature = git(source, "rev-parse", "HEAD").strip()
        git(source, "checkout", "-q", "main")
        for i in range(9):
            self.commit(source, "main.txt", str(i))
        git(source, "branch", "unrelated")
        git(source, "tag", "unrelated-tag")
        root = self.shallow(source)
        rc, output = self.run_ci(root)
        self.assertEqual(rc, 0, output)
        self.assertIn("0 regression(s)", output)
        self.assertEqual(git(root, "merge-base", "origin/main", "HEAD").strip(), expected)
        self.assertEqual(git(root, "rev-parse", "HEAD").strip(), feature)
        self.assertEqual(git(root, "tag"), "")
        self.assertNotIn("unrelated", git(root, "for-each-ref", "--format=%(refname)"))
        self.assertEqual(len(git(root, "worktree", "list").splitlines()), 1)

    def test_fork_synthetic_merge_keeps_checked_out_commit_and_correct_base_tree(self):
        source = self.source()
        git(source, "checkout", "-qb", "fork-head")
        self.commit(source, "feature.txt", "fork change")
        git(source, "checkout", "-q", "main")
        expected = self.commit(source, "base-only.txt", "advanced base")
        git(source, "checkout", "-qb", "pr-merge")
        git(source, "merge", "-q", "--no-ff", "fork-head", "-m", "synthetic merge")
        merge = git(source, "rev-parse", "HEAD").strip()
        git(source, "update-ref", "refs/pull/1/merge", merge)
        git(source, "checkout", "-q", "main")
        git(source, "branch", "-D", "fork-head", "pr-merge")
        root = self.shallow(source, "main")
        git(root, "fetch", "-q", "--depth=1", "origin", "refs/pull/1/merge")
        git(root, "checkout", "-q", "--detach", "FETCH_HEAD")
        # Advance base after the synthetic merge was created: do not substitute
        # the new base tip for the exact common ancestor of the tested checkout.
        self.commit(source, "late-base.txt", "new base commit")
        seen = []
        real_audit = ar.audit_repo
        def audit(path, *args):
            seen.append((git(path, "rev-parse", "HEAD").strip(), (path / "base-only.txt").exists(),
                         (path / "late-base.txt").exists()))
            return real_audit(path, *args)
        with patch.object(ar, "audit_repo", side_effect=audit):
            rc, output = self.run_ci(root)
        self.assertEqual(rc, 0, output)
        self.assertEqual(git(root, "rev-parse", "HEAD").strip(), merge)
        self.assertEqual(git(root, "merge-base", "origin/main", "HEAD").strip(), expected)
        self.assertEqual(seen, [(merge, True, False), (expected, True, False)])

    def test_fetch_failure_and_missing_helper_fail_only_enforced_repositories(self):
        source = self.source()
        git(source, "checkout", "-qb", "feature")
        root = self.shallow(source)
        git(root, "remote", "set-url", "origin", str(self.root / "missing"))
        for enforced in (True, False):
            (root / ar.CONFIG_FILE).write_text(json.dumps({"always": True, "enforce": enforced}))
            for missing_helper in (False, True):
                with self.subTest(enforced=enforced, missing_helper=missing_helper):
                    summary = self.root / "summary.md"
                    summary.write_text("")
                    with patch.dict("sys.modules", {"agent_readiness_git": None} if missing_helper else {}), \
                         patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                        rc, output = self.run_ci(root)
                    self.assertEqual(rc, 2 if enforced else 0, output)
                    self.assertIn("merge-base", output)
                    self.assertNotIn("regressions vs base", output)
                    self.assertIn("Ratchet unavailable: <pre>", summary.read_text())
                    self.assertIn("agent_readiness_git" if missing_helper else "fetch", summary.read_text())

    def test_lfs_smudge_is_skipped_for_base_worktree(self):
        source = self.source()
        self.commit(source, ".gitattributes", "*.sql filter=lfs\n")
        self.commit(source, "old.sql", "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a"*64 + "\nsize 9000000\n")
        git(source, "checkout", "-qb", "feature")
        git(source, "rm", "old.sql")
        git(source, "commit", "-qm", "remove dump")
        root = self.shallow(source)
        # A local filter fixture obeys the real git-lfs skip variable, and
        # refuses any download. This also proves the detached baseline gets it.
        git(root, "config", "filter.lfs.smudge", 'test "$GIT_LFS_SKIP_SMUDGE" = 1 && cat')
        git(root, "config", "filter.lfs.required", "true")
        rc, output = self.run_ci(root)
        self.assertEqual(rc, 0, output)
        self.assertIn("0 regression(s)", output)

    def test_historical_blobs_remain_remote(self):
        source = self.source()
        self.commit(source, "obsolete.sql", "old fixture row\n" * 10000)
        obsolete = git(source, "rev-parse", "HEAD:obsolete.sql").strip()
        git(source, "rm", "obsolete.sql")
        git(source, "commit", "-qm", "remove obsolete fixture")
        git(source, "checkout", "-qb", "feature")
        root = self.shallow(source)
        rc, output = self.run_ci(root)
        self.assertEqual(rc, 0, output)
        missing = git(root, "rev-list", "--objects", "--all", "--missing=print")
        self.assertIn("?" + obsolete, missing)

    def test_fetch_timeout_cannot_pass_an_enforced_ratchet(self):
        source = self.source()
        git(source, "checkout", "-qb", "feature")
        root = self.shallow(source)
        real_run = subprocess.run
        def run(args, **kwargs):
            if "fetch" in args:
                self.assertLessEqual(kwargs["timeout"], 180)
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return real_run(args, **kwargs)
        with patch("agent_readiness_git.subprocess.run", side_effect=run):
            rc, output = self.run_ci(root)
        self.assertEqual(rc, 2, output)
        self.assertIn("exact merge-base unavailable", output)

    def test_auth_is_scoped_ephemeral_and_fetch_is_bounded(self):
        source = self.source()
        git(source, "remote", "add", "origin", "https://github.com/example/fixture.git")
        original = git(source, "config", "--local", "--list")
        real_run = subprocess.run
        commands = []
        token = "fixture-token-never-persist"
        def run(args, **kwargs):
            if "fetch" in args:
                commands.append((args, kwargs))
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, **kwargs)
        with patch.dict(os.environ, {**GIT_ENV, "GITHUB_REPOSITORY": "example/fixture",
                                     "GITHUB_SERVER_URL": "https://github.com",
                                     "AGENT_READINESS_GIT_TOKEN": token}), patch("agent_readiness_git.subprocess.run", side_effect=run):
            prepare_ci_git(source, "main")
        args, kwargs = commands[0]
        self.assertEqual(kwargs["timeout"], 180)
        self.assertIn("--filter=blob:none", args)
        self.assertIn("--no-tags", args)
        self.assertFalse(any("*" in arg for arg in args))
        self.assertNotIn(token, repr(args))
        env = kwargs["env"]
        config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self.assertEqual(config["http.https://github.com/.extraheader"], f"AUTHORIZATION: basic {encoded}")
        self.assertEqual(config["http.followRedirects"], "false")
        self.assertNotIn("AGENT_READINESS_GIT_TOKEN", env)
        self.assertEqual(git(source, "config", "--local", "--list"), original)

    def test_token_never_sent_to_mismatched_origin_or_ssh(self):
        source = self.source()
        git(source, "remote", "add", "origin", "https://example.invalid/other/repo")
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/fixture", "GITHUB_SERVER_URL": "https://github.com",
                                     "AGENT_READINESS_GIT_TOKEN": "fixture-token"}):
            for origin in ("https://example.invalid/other/repo", "git@github.com:example/fixture.git",
                           "https://github.com/other/repo.git"):
                git(source, "remote", "set-url", "origin", origin)
                with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "origin does not match"):
                    prepare_ci_git(source, "main")
