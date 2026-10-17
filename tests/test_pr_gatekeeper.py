#!/usr/bin/env python3
"""Unit tests for scripts/pr-gatekeeper.py.

No network: `evaluate()` is pure, so every case here is a hand-built payload.
The cases are the ones that would each, on their own, lock merges org-wide or
let a red PR through.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
from pathlib import Path
import os
from datetime import datetime, timezone, timedelta

_SPEC = importlib.util.spec_from_file_location(
    "pr_gatekeeper",
    Path(__file__).resolve().parent.parent / "scripts" / "pr-gatekeeper.py",
)
pr_gatekeeper = importlib.util.module_from_spec(_SPEC)
sys.modules["pr_gatekeeper"] = pr_gatekeeper
_SPEC.loader.exec_module(pr_gatekeeper)

GATE_CHECK_NAME = pr_gatekeeper.GATE_CHECK_NAME
evaluate = pr_gatekeeper.evaluate

EMPTY_STATUSES = {"state": "pending", "total_count": 0, "statuses": []}


def run(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    app_id: int = 15368,
    run_id: int = 1,
    started_at: str = "2026-09-01T00:00:00Z",
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "app": {"id": app_id, "slug": "github-actions"},
    }


def suite(runs_count: int, status: str = "completed", suite_id: int = 900) -> dict:
    return {
        "id": suite_id,
        "status": status,
        "latest_check_runs_count": runs_count,
        "app": {"slug": "github-actions"},
    }


class TestBlocking(unittest.TestCase):
    """The gate must catch red checks absent from a required-context list."""

    def test_red_check_absent_from_any_required_list_fails(self):
        status, conclusion, title, summary = evaluate(
            [
                run("build, lint, typecheck, test"),
                run("Playwright and pgTAP", conclusion="failure", run_id=2),
            ],
            EMPTY_STATUSES,
            [suite(2)],
        )
        self.assertEqual((status, conclusion), ("completed", "failure"))
        self.assertIn("Playwright and pgTAP", summary)
        self.assertIn("1 check", title)

    def test_unknown_conclusion_blocks(self):
        """Allowlist, not denylist: a conclusion GitHub adds later must block."""
        status, conclusion, _, _ = evaluate(
            [run("future", conclusion="quantum_undecided")], EMPTY_STATUSES, []
        )
        self.assertEqual((status, conclusion), ("completed", "failure"))

    def test_cancelled_timed_out_and_action_required_all_block(self):
        for bad in ("cancelled", "timed_out", "action_required", "failure", "stale", None):
            with self.subTest(conclusion=bad):
                status, conclusion, _, _ = evaluate(
                    [run("x", conclusion=bad)], EMPTY_STATUSES, []
                )
                self.assertEqual((status, conclusion), ("completed", "failure"))

    def test_skipped_neutral_do_not_block(self):
        status, conclusion, _, _ = evaluate(
            [
                run("a", conclusion="skipped"),
                run("b", conclusion="neutral", run_id=2),
                run("d", conclusion="success", run_id=4),
            ],
            EMPTY_STATUSES,
            [suite(4)],
        )
        self.assertEqual((status, conclusion), ("completed", "success"))


class TestPending(unittest.TestCase):
    def test_pending_check_is_in_progress_with_no_conclusion(self):
        """`conclusion` must be None here. Any conclusion at all forces
        status=completed server-side, so 'in_progress' as a conclusion is a
        422 on every still-running PR."""
        status, conclusion, _, _ = evaluate(
            [run("slow", status="in_progress", conclusion=None)],
            EMPTY_STATUSES,
            [suite(1, status="in_progress")],
        )
        self.assertEqual(status, "in_progress")
        self.assertIsNone(conclusion)

    def test_pending_never_reports_success(self):
        status, conclusion, _, _ = evaluate(
            [run("done"), run("queued one", status="queued", conclusion=None, run_id=2)],
            EMPTY_STATUSES,
            [],
        )
        self.assertNotEqual(conclusion, "success")
        self.assertEqual((status, conclusion), ("in_progress", None))

    def test_failure_wins_over_pending(self):
        status, conclusion, _, _ = evaluate(
            [
                run("red", conclusion="failure"),
                run("slow", status="in_progress", conclusion=None, run_id=2),
            ],
            EMPTY_STATUSES,
            [],
        )
        self.assertEqual((status, conclusion), ("completed", "failure"))


class TestSelfExclusion(unittest.TestCase):
    def test_gate_excludes_its_own_check_run(self):
        """Left in, the gate's own in_progress run would hold itself open
        forever, and its own previous failure would pin itself red."""
        status, conclusion, _, _ = evaluate(
            [
                run("real check"),
                run(GATE_CHECK_NAME, status="in_progress", conclusion=None, run_id=2),
                run(GATE_CHECK_NAME, conclusion="failure", run_id=3),
            ],
            EMPTY_STATUSES,
            [],
        )
        self.assertEqual((status, conclusion), ("completed", "success"))

    def test_gate_excludes_its_own_running_suite(self):
        gate = run(GATE_CHECK_NAME, status="in_progress", conclusion=None, run_id=2)
        gate["check_suite"] = {"id": 901}
        status, conclusion, _, _ = evaluate(
            [run("real check"), gate],
            EMPTY_STATUSES,
            [suite(1, status="in_progress", suite_id=901)],
        )
        self.assertEqual((status, conclusion), ("completed", "success"))

    def test_advisory_recovery_job_never_gates(self):
        for recovery in (
            run("recover-persona / request", conclusion="failure", run_id=4),
            run("recover-persona / request", status="in_progress", conclusion=None, run_id=5),
        ):
            with self.subTest(status=recovery["status"]):
                status, conclusion, _, _ = evaluate([run("real check"), recovery], EMPTY_STATUSES, [])
                self.assertEqual((status, conclusion), ("completed", "success"))

    def test_advisory_name_from_another_app_still_gates(self):
        imposter = run("recover-persona / request", conclusion="failure", app_id=999, run_id=6)
        self.assertEqual(evaluate([run("real check"), imposter], EMPTY_STATUSES, [])[1], "failure")


class TestDedupe(unittest.TestCase):
    def test_latest_stale_check_cannot_inherit_older_success(self):
        old = run("scan", run_id=1)
        latest = run("scan", run_id=2, conclusion="stale", started_at="2026-09-02T00:00:00Z")
        status, conclusion, _, summary = evaluate([old, latest, run("other CI")], EMPTY_STATUSES, [])
        self.assertEqual((status, conclusion), ("completed", "failure"))
        self.assertIn("scan (stale)", summary)

    def test_same_named_workflows_do_not_hide_a_failure(self):
        failed = run("test", conclusion="failure", run_id=1)
        passed = run("test", run_id=2)
        failed["check_suite"] = {"id": 10}
        passed["check_suite"] = {"id": 20}
        self.assertEqual(evaluate([failed, passed], EMPTY_STATUSES, [])[1], "failure")

    def test_same_app_and_name_counts_only_the_latest(self):
        """'Re-run failed jobs' leaves the stale failure beside the new
        success; counting both pins the gate red permanently."""
        status, conclusion, _, _ = evaluate(
            [
                run(
                    "build",
                    conclusion="failure",
                    run_id=1,
                    started_at="2026-09-01T00:00:00Z",
                ),
                run(
                    "build",
                    conclusion="success",
                    run_id=2,
                    started_at="2026-09-01T01:00:00Z",
                ),
            ],
            EMPTY_STATUSES,
            [],
        )
        self.assertEqual((status, conclusion), ("completed", "success"))

    def test_same_name_from_different_apps_is_not_deduped(self):
        status, conclusion, _, _ = evaluate(
            [
                run("build", conclusion="success", app_id=1, run_id=1),
                run("build", conclusion="failure", app_id=2, run_id=2),
            ],
            EMPTY_STATUSES,
            [],
        )
        self.assertEqual((status, conclusion), ("completed", "failure"))


class TestRecovery(unittest.TestCase):
    def setUp(self):
        prior = patch.object(pr_gatekeeper, "previous_publication_refs", return_value=set())
        prior.start()
        self.addCleanup(prior.stop)

    def test_http_error_records_failed_get_path(self):
        error = urllib.error.HTTPError("https://api.github.com/test", 403, "forbidden", {}, None)
        with patch.object(pr_gatekeeper.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                pr_gatekeeper._get("/test", "fake")
        self.assertEqual(caught.exception.request_path, "/test")

    def test_transient_http_retried_but_auth_not_retried(self):
        for code, calls in ((503, 2), (403, 1)):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{}'
            failure = urllib.error.HTTPError("https://api.github.com/test", code, "failed", {}, None)
            with patch.object(pr_gatekeeper.urllib.request, "urlopen", side_effect=[failure, response]) as request, patch.object(pr_gatekeeper.time, "sleep"):
                if code == 403:
                    with self.assertRaises(urllib.error.HTTPError):
                        pr_gatekeeper._get("/test", "fake")
                else:
                    self.assertEqual(pr_gatekeeper._get("/test", "fake"), {})
                self.assertEqual(request.call_count, calls)

    def test_read_failure_posts_visible_failure_on_pr(self):
        for error in (ValueError("malformed response"), urllib.error.URLError("timeout")):
            with patch.object(pr_gatekeeper, "collect", side_effect=error), patch.object(pr_gatekeeper, "_post") as post:
                self.assertEqual(pr_gatekeeper.report("demo/repo", "abc", "fake"), 2)
                body = post.call_args.args[2]
                self.assertEqual(body["head_sha"], "abc")
                self.assertEqual(body["conclusion"], "failure")

    def test_malformed_check_payload_is_not_green(self):
        for payload in ([], {}, {"check_runs": ["invalid"]}):
            with patch.object(pr_gatekeeper, "_get", return_value=payload):
                with self.assertRaises(ValueError):
                    pr_gatekeeper.collect("demo/repo", "abc", "fake")

    def test_status_pagination_preserves_failure_on_second_page(self):
        first = {"total_count": 101, "statuses": [{"context": str(i), "state": "success"} for i in range(100)]}
        second = {"total_count": 101, "statuses": [{"context": "late", "state": "failure"}]}
        with patch.object(pr_gatekeeper, "_get", side_effect=[{"check_runs": []}, first, second, {"check_suites": []}, {"workflow_runs": [], "total_count": 0}]):
            runs, statuses, suites = pr_gatekeeper.collect("demo/repo", "abc", "fake")
            self.assertEqual(evaluate(runs, statuses, suites)[1], "failure")

    def test_sha_read_falls_back_to_matching_open_pr_branch(self):
        missing = urllib.error.HTTPError("https://api.github.com/test", 422, "missing", {}, None)
        with patch.object(
            pr_gatekeeper,
            "_get",
            side_effect=[
                missing,
                [{"head": {"sha": "abc", "ref": "codex/fix gate"}}],
                {"check_runs": []},
                EMPTY_STATUSES,
                {"check_suites": []},
                {"workflow_runs": [], "total_count": 0},
            ],
        ) as get:
            self.assertEqual(pr_gatekeeper.collect("demo/repo", "abc", "fake"), ([], EMPTY_STATUSES, []))
        self.assertIn("commits/codex%2Ffix%20gate/check-runs", get.call_args_list[2].args[0])
        self.assertIn("actions/runs?head_sha=abc", get.call_args_list[-1].args[0])

    def test_sha_read_raises_when_no_open_pr_matches(self):
        missing = urllib.error.HTTPError("https://api.github.com/test", 422, "missing", {}, None)
        with patch.object(pr_gatekeeper, "_get", side_effect=[missing, []]):
            with self.assertRaises(urllib.error.HTTPError):
                pr_gatekeeper.collect("demo/repo", "abc", "fake")

    def test_scheduled_reconcile_recovers_late_external_check(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), patch.object(pr_gatekeeper, "_get", return_value=[{"head": {"sha": "abc"}}]), patch.object(pr_gatekeeper, "collect", side_effect=[([run("external", status="in_progress", conclusion=None)], EMPTY_STATUSES, []), ([run("external")], EMPTY_STATUSES, [])]), patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
            self.assertEqual(post.call_args.args[2]["status"], "in_progress")
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
            self.assertEqual(post.call_args.args[2]["conclusion"], "success")

    def test_reconcile_continues_after_one_post_failure(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), patch.object(pr_gatekeeper, "_get", return_value=[{"head": {"sha": "a"}}, {"head": {"sha": "b"}}]), patch.object(pr_gatekeeper, "report", side_effect=[2, 0]) as report:
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 2)
            self.assertEqual(report.call_count, 2)

    def test_coalesced_refresh_updates_other_prs_and_current_heads(self):
        # The pending event may have been for an entirely different PR or an
        # obsolete head. Every surviving refresh must discover current heads.
        prs = [{"head": {"sha": sha}} for sha in ("new-a", "b", "c")]
        inventories = {
            "new-a": ([run("build", conclusion="failure")], EMPTY_STATUSES, []),
            "b": ([run("build", status="queued", conclusion=None)], EMPTY_STATUSES, []),
            "c": ([run("build")], EMPTY_STATUSES, []),
        }
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake", "PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", return_value=prs), \
             patch.object(pr_gatekeeper, "collect", side_effect=lambda repo, sha, token: inventories[sha]), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
        bodies = {call.args[2]["head_sha"]: call.args[2] for call in post.call_args_list}
        self.assertEqual(set(bodies), {"new-a", "b", "c"})
        self.assertEqual(bodies["new-a"]["conclusion"], "failure")
        self.assertEqual(bodies["b"]["status"], "in_progress")
        self.assertEqual(bodies["c"]["conclusion"], "success")

    def test_empty_reconciliation_stays_pending_until_ci_registers(self):
        prs = [{"head": {"sha": "new"}, "merge_commit_sha": "merge"}]
        seed = run(pr_gatekeeper.GATE_CHECK_NAME, status="in_progress", conclusion=None)
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake", "PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", return_value=prs), \
             patch.object(pr_gatekeeper, "collect", return_value=([seed], EMPTY_STATUSES, [])) as collect, \
             patch.object(pr_gatekeeper, "_post") as post:
            for _ in range(2):
                self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
                self.assertTrue(all(c.args[2]["status"] == "in_progress" for c in post.call_args_list))
                self.assertTrue(all(c.args[2]["output"]["title"] == "Gate seeded, awaiting CI" for c in post.call_args_list))
                self.assertTrue(all(pr_gatekeeper.EVALUATED_MARKER not in c.args[2]["output"]["summary"] for c in post.call_args_list))
            collect.side_effect = [([run("build")], EMPTY_STATUSES, []), ([], EMPTY_STATUSES, [])]
            post.reset_mock()
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
            self.assertTrue(all(c.args[2]["conclusion"] == "success" for c in post.call_args_list))

    def test_merge_failure_blocks_shared_heads_without_persona_reviews(self):
        prs = [{"head": {"sha": "head"}, "merge_commit_sha": merge} for merge in ("merge-a", "merge-b")]
        inventories = {
            "head": ([run("head build")], EMPTY_STATUSES, []),
            "merge-a": ([run("merge build", conclusion="failure")], EMPTY_STATUSES, []),
            "merge-b": ([], EMPTY_STATUSES, []),
        }
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake", "PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", return_value=prs), \
             patch.object(pr_gatekeeper, "collect", side_effect=lambda repo, sha, token: inventories[sha]) as collect, \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
        self.assertEqual(collect.call_count, 3)
        self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, set(inventories))
        self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

    def test_merge_only_ci_can_complete_reconciliation_with_empty_head(self):
        prs = [{"head": {"sha": "head"}, "merge_commit_sha": "merge"}]
        inventories = {"head": ([], EMPTY_STATUSES, []),
                       "merge": ([run("merge build")], EMPTY_STATUSES, [])}
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake", "PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", return_value=prs), \
             patch.object(pr_gatekeeper, "collect", side_effect=lambda repo, sha, token: inventories[sha]), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open"]), 0)
        self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, set(inventories))
        self.assertTrue(all(c.args[2]["conclusion"] == "success" for c in post.call_args_list))

    def test_legacy_single_sha_evaluates_without_claiming_complete_pr_metadata(self):
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "collect", return_value=([run("build")], EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "_get") as get, \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("demo/repo", "head", "fake"), 0)
        get.assert_not_called()
        post.assert_called_once()
        body = post.call_args.args[2]
        self.assertEqual((body["head_sha"], body["conclusion"]), ("head", "success"))
        self.assertIn(pr_gatekeeper.EVALUATED_MARKER, body["output"]["summary"])
        self.assertIn('<!-- gatekeeper-refs:["head"] -->', body["output"]["summary"])
        self.assertNotIn("<!-- gatekeeper-refs-complete -->", body["output"]["summary"])

    def test_event_evaluation_cannot_publish_green_before_refresh(self):
        prs = [{"head": {"sha": "new"}, "merge_commit_sha": "merge"}]
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake", "PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", return_value=prs), \
             patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "demo/repo", "--sha", "new", "--require-ci"]), 0)
        self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {"new", "merge"})
        self.assertTrue(all(c.args[2]["status"] == "in_progress" for c in post.call_args_list))


class TestInvocationRefresh(unittest.TestCase):
    """Count real API collector paths; no cached verdict or mocked report."""

    def pr(self, number=1, head="head-a", merge="merge-a"):
        return {"number": number, "head": {"sha": head, "ref": f"fix-{number}", "repo": {"id": 42}},
                "merge_commit_sha": merge}

    def invoke(self, prs, trigger="head-a", *, snapshots=None, inventories=None,
               read_error=None, post_error=None, extra=(), reviews=None, review_run=None):
        self.reads, self.bodies = [], []
        snapshots = iter(snapshots if snapshots is not None else [prs, prs])
        inventories = inventories or {}

        def get(path, token):
            self.reads.append(path)
            error = read_error and read_error(path, self.reads)
            if isinstance(error, Exception):
                raise error
            if error:
                raise urllib.error.URLError("fixture API failure")
            if "/pulls?state=open" in path:
                return next(snapshots)
            if path.endswith("/actions/runs/123"):
                return review_run
            if "/reviews?" in path:
                number = int(path.split("/pulls/")[1].split("/")[0])
                return (reviews or {}).get(number, [])
            if "/commits/" in path:
                ref = path.split("/commits/")[1].split("/")[0]
                runs, statuses, suites = inventories.get(ref, ([run("build")], EMPTY_STATUSES, []))
                if "/check-runs?filter=all" in path:
                    return {"check_runs": []}
                if "/check-runs?" in path:
                    return {"check_runs": runs}
                if "/status?" in path:
                    return statuses
                if "/check-suites?" in path:
                    return {"check_suites": suites}
            if "/actions/runs?head_sha=" in path:
                return {"workflow_runs": [], "total_count": 0}
            self.fail(f"Unexpected API read: {path}")

        def post(path, token, body):
            self.assertEqual(path, "/repos/demo/repo/check-runs")
            self.assertEqual(body["name"], GATE_CHECK_NAME)
            self.bodies.append(body)
            if body["head_sha"] == post_error:
                raise urllib.error.URLError("fixture publication failure")
            return {}

        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake", "PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=get), \
             patch.object(pr_gatekeeper, "_post", side_effect=post):
            return pr_gatekeeper.main(["--repo", "demo/repo", "--sha", trigger, "--reconcile-open", *extra])

    def assert_counts(self, refs, review_numbers=(), extra_reads=(), read_refs=None):
        from collections import Counter
        expected = ["/repos/demo/repo/pulls?state=open&per_page=100&page=1"] * 2
        for ref in (refs if read_refs is None else read_refs):
            expected += [f"/repos/demo/repo/commits/{ref}/check-runs?filter=latest&per_page=100&page=1",
                         f"/repos/demo/repo/commits/{ref}/status?per_page=100&page=1",
                         f"/repos/demo/repo/commits/{ref}/check-suites?per_page=100&page=1",
                         f"/repos/demo/repo/actions/runs?head_sha={ref}&per_page=100&page=1"]
        expected += [f"/repos/demo/repo/pulls/{number}/reviews?per_page=100&page=1" for number in review_numbers]
        self.assertEqual(Counter(self.reads), Counter(expected + list(extra_reads)))
        self.assertEqual(Counter(body["head_sha"] for body in self.bodies), Counter(refs))

    def test_two_prs_publish_each_head_and_merge_once(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs), 0)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2])
        self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies))

    def test_merge_trigger_and_shared_heads_read_every_review_and_inventory_once(self):
        prs = [self.pr(), self.pr(2, "head-a", "merge-b")]
        reviews = {1: [TestPersonaReview().review(sha="head-a")], 2: []}
        self.assertEqual(self.invoke(prs, "merge-a", reviews=reviews), 0)
        self.assert_counts(["head-a", "merge-a", "merge-b"], [1, 2])
        self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies))

    def test_stale_or_closed_trigger_cannot_skip_current_pr_destinations(self):
        for trigger in ("old-head", "old-merge"):
            with self.subTest(trigger=trigger):
                prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
                self.assertEqual(self.invoke(prs, trigger), 0)
                self.assert_counts([trigger, "head-a", "merge-a", "head-b", "merge-b"], [1, 2])
        self.assertEqual(self.invoke([], "closed-merge"), 0)
        self.assert_counts(["closed-merge"])

    def test_head_changes_between_event_and_refresh(self):
        initial = [self.pr(), self.pr(2, "head-b", "merge-b")]
        current = [self.pr(1, "new-head", "new-merge"), initial[1]]
        self.assertEqual(self.invoke(initial, snapshots=[initial, current]), 0)
        self.assert_counts(["head-a", "merge-a", "new-head", "new-merge", "head-b", "merge-b"], [1, 1, 2])

    def test_metadata_change_with_same_head_requires_current_review_and_merge_inventory(self):
        initial = [self.pr()]
        current = [self.pr(1, "head-a", "new-merge"), self.pr(2, "head-a", "merge-b")]
        self.assertEqual(self.invoke(initial, snapshots=[initial, current]), 0)
        self.assert_counts(["head-a", "merge-a", "head-a", "new-merge", "merge-b"], [1, 1, 2])

    def test_previously_unassociated_sha_cannot_suppress_a_new_current_pr(self):
        current = [self.pr(2, "head-a", "merge-b")]
        self.assertEqual(self.invoke([], snapshots=[[], current]), 0)
        self.assert_counts(["head-a", "head-a", "merge-b"], [2])
        self.assertEqual(self.bodies[-1]["status"], "in_progress")

    def test_read_failure_blocks_both_refs_and_continues_other_pr(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs, read_error=lambda path, _: "/commits/head-a/check-runs?filter=latest" in path), 2)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2],
                           ["/repos/demo/repo/commits/head-a/check-runs?filter=latest&per_page=100&page=1",
                            "/repos/demo/repo/commits/head-a/check-runs?filter=all&per_page=100&page=1"],
                           read_refs=["head-b", "merge-b"])
        self.assertEqual([body["head_sha"] for body in self.bodies], ["head-a", "merge-a", "head-b", "merge-b"])
        self.assertTrue(all(body["conclusion"] == "failure" for body in self.bodies[:2]))
        self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies[2:]))

    def test_partial_publication_does_not_repeat_trigger_or_skip_other_pr(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs, post_error="head-a"), 2)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2])

    def test_initial_metadata_failure_keeps_blocking_fallback_and_current_refresh(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs, snapshots=[prs], read_error=lambda path, reads: len(reads) == 1), 2)
        self.assert_counts(["head-a", "head-a", "merge-a", "head-b", "merge-b"], [1, 2],
                           ["/repos/demo/repo/commits/head-a/check-runs?filter=all&per_page=100&page=1"],
                           read_refs=["head-a", "merge-a", "head-b", "merge-b"])
        self.assertEqual([body["head_sha"] for body in self.bodies], ["head-a", "head-a", "merge-a", "head-b", "merge-b"])
        self.assertEqual(self.bodies[0]["conclusion"], "failure")

    def test_refresh_metadata_failure_is_not_success(self):
        prs = [self.pr()]
        self.assertEqual(self.invoke(prs, read_error=lambda path, reads: "/pulls?state=open" in path and len(reads) > 1), 2)
        self.assert_counts(["head-a", "merge-a"], [1])

    def test_fork_review_uses_trusted_resolution_and_same_invocation_metadata(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        event = {"event": "pull_request_review", "head_sha": "old-merge", "pull_requests": [],
                 "head_repository": {"id": 42}, "head_branch": "fix-1"}
        self.assertEqual(self.invoke(prs, "old-merge", extra=["--review-run", "123"], review_run=event), 0)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2], ["/repos/demo/repo/actions/runs/123"])

    def test_unassociated_fork_signal_retains_failure_and_refresh(self):
        prs = [self.pr()]
        event = {"event": "pull_request_review", "head_sha": "unrelated", "pull_requests": []}
        self.assertEqual(self.invoke(prs, "old-merge", extra=["--review-run", "123"], review_run=event), 2)
        self.assert_counts(["old-merge", "head-a", "merge-a"], [1],
                           ["/repos/demo/repo/actions/runs/123",
                            "/repos/demo/repo/commits/old-merge/check-runs?filter=all&per_page=100&page=1"],
                           read_refs=["head-a", "merge-a"])
        self.assertEqual([body["head_sha"] for body in self.bodies], ["old-merge", "head-a", "merge-a"])
        self.assertEqual(self.bodies[0]["conclusion"], "failure")

    def test_exact_head_persona_signal_survives_without_second_publication(self):
        for state in ("running", "failed"):
            with self.subTest(state=state):
                prs = [self.pr()]
                self.assertEqual(self.invoke(prs, extra=["--persona-state", state, "--persona-pr", "1"]), 0)
                self.assert_counts(["head-a", "merge-a"], [1])
                self.assertTrue(all(f"{state}:" in body["output"]["summary"] for body in self.bodies))

    def test_stale_persona_signal_cannot_mark_current_head_running(self):
        self.assertEqual(self.invoke([self.pr()], "old-head", extra=["--persona-state", "running", "--persona-pr", "1"]), 0)
        self.assert_counts(["old-head", "head-a", "merge-a"], [1])
        self.assertTrue(all("review running" not in body["output"]["title"] for body in self.bodies))

    def test_current_exact_head_approvals_allow_success_on_every_destination(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        reviews = {1: [TestPersonaReview().review(sha="head-a")],
                   2: [TestPersonaReview().review(sha="head-b")]}
        self.assertEqual(self.invoke(prs, reviews=reviews), 0)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2])
        self.assertTrue(all(body.get("conclusion") == "success" for body in self.bodies))

    def test_empty_failed_cancelled_and_stale_review_inputs_never_pass(self):
        reviews = {1: [TestPersonaReview().review(sha="old-head")]}
        cases = [("empty", ([], EMPTY_STATUSES, []), "in_progress"),
                 ("failed", ([run("build", conclusion="failure")], EMPTY_STATUSES, []), "completed"),
                 ("cancelled", ([run("build", conclusion="cancelled")], EMPTY_STATUSES, []), "completed"),
                 ("status-failed", ([], {"total_count": 1, "statuses": [{"context": "deploy", "state": "failure"}]}, []), "completed")]
        for label, inventory, status in cases:
            with self.subTest(label=label):
                self.assertEqual(self.invoke([self.pr()], inventories={"head-a": inventory, "merge-a": inventory}, reviews=reviews), 0)
                self.assert_counts(["head-a", "merge-a"], [1])
                self.assertTrue(all(body["status"] == status and body.get("conclusion") != "success" for body in self.bodies))

    def test_red_merge_inventory_blocks_approved_green_head(self):
        reviews = {1: [TestPersonaReview().review(sha="head-a")]}
        cases = [("failed", ([run("merge build", conclusion="failure")], EMPTY_STATUSES, []), "completed"),
                 ("cancelled", ([run("merge build", conclusion="cancelled")], EMPTY_STATUSES, []), "completed"),
                 ("stale", ([run("merge build", conclusion="stale")], EMPTY_STATUSES, []), "completed"),
                 ("pending-status", ([], {"total_count": 1, "statuses": [{"context": "deploy", "state": "pending"}]}, []), "in_progress")]
        for label, merge_inventory, status in cases:
            with self.subTest(label=label):
                self.assertEqual(self.invoke([self.pr()], inventories={"merge-a": merge_inventory}, reviews=reviews), 0)
                self.assert_counts(["head-a", "merge-a"], [1])
                self.assertTrue(all(body["status"] == status and body.get("conclusion") != "success" for body in self.bodies))

    def test_merge_read_failure_after_green_head_blocks_both_refs_and_continues_other_pr(self):
        # The head inventory was already read; the merge failure must still
        # replace both destinations. The rejected merge SHA has no open PR
        # branch to fall back to, so collect() re-reads the PR list once.
        rejected = urllib.error.HTTPError("https://api.github.com/test", 422, "No commit found", {}, None)
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs, snapshots=[prs, prs, prs],
                                     read_error=lambda path, _: "/commits/merge-a/check-runs?filter=latest" in path and rejected), 2)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2],
                           ["/repos/demo/repo/pulls?state=open&per_page=100&page=1",
                            "/repos/demo/repo/commits/merge-a/check-runs?filter=latest&per_page=100&page=1",
                            "/repos/demo/repo/commits/head-a/check-runs?filter=all&per_page=100&page=1"],
                           read_refs=["head-a", "head-b", "merge-b"])
        self.assertTrue(all(body["conclusion"] == "failure" for body in self.bodies[:2]))
        self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies[2:]))

    def test_rejected_stale_merge_trigger_fails_closed_without_skipping_current_prs(self):
        rejected = urllib.error.HTTPError("https://api.github.com/test", 422, "No commit found", {}, None)
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs, "old-merge", snapshots=[prs, prs, prs],
                                     read_error=lambda path, _: "/commits/old-merge/check-runs?filter=latest" in path and rejected), 2)
        self.assert_counts(["old-merge", "head-a", "merge-a", "head-b", "merge-b"], [1, 2],
                           ["/repos/demo/repo/pulls?state=open&per_page=100&page=1",
                            "/repos/demo/repo/commits/old-merge/check-runs?filter=latest&per_page=100&page=1",
                            "/repos/demo/repo/commits/old-merge/check-runs?filter=all&per_page=100&page=1"],
                           read_refs=["head-a", "merge-a", "head-b", "merge-b"])
        self.assertEqual(self.bodies[0]["head_sha"], "old-merge")
        self.assertEqual(self.bodies[0]["conclusion"], "failure")
        self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies[1:]))

    def test_merge_sha_appearing_or_disappearing_during_refresh_reevaluates_current_destinations(self):
        for label, initial_merge, current_merge, refs in (
                ("computed", None, "merge-a", ["head-a", "head-a", "merge-a"]),
                ("recomputing", "merge-a", None, ["head-a", "merge-a", "head-a"])):
            with self.subTest(label=label):
                initial, current = [self.pr(merge=initial_merge)], [self.pr(merge=current_merge)]
                self.assertEqual(self.invoke(initial, snapshots=[initial, current]), 0)
                self.assert_counts(refs, [1, 1])
                self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies))

    def test_review_read_failure_blocks_both_refs_and_continues_other_pr(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(prs, read_error=lambda path, _: "/pulls/1/reviews?" in path), 2)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2],
                           ["/repos/demo/repo/commits/head-a/check-runs?filter=all&per_page=100&page=1"],
                           read_refs=["head-b", "merge-b"])
        self.assertEqual([body["head_sha"] for body in self.bodies], ["head-a", "merge-a", "head-b", "merge-b"])
        self.assertTrue(all(body["conclusion"] == "failure" for body in self.bodies[:2]))
        self.assertTrue(all(body["status"] == "in_progress" for body in self.bodies[2:]))

    def test_trigger_pr_closed_during_refresh_is_not_reevaluated(self):
        initial = [self.pr(), self.pr(2, "head-b", "merge-b")]
        self.assertEqual(self.invoke(initial, snapshots=[initial, initial[1:]]), 0)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2])

    def test_approval_of_replaced_head_cannot_pass_current_head(self):
        initial = [self.pr(), self.pr(2, "head-b", "merge-b")]
        current = [self.pr(1, "new-head", "new-merge"), initial[1]]
        reviews = {1: [TestPersonaReview().review(sha="head-a")]}
        self.assertEqual(self.invoke(initial, snapshots=[initial, current], reviews=reviews), 0)
        self.assert_counts(["head-a", "merge-a", "new-head", "new-merge", "head-b", "merge-b"], [1, 1, 2])
        verdicts = {body["head_sha"]: (body["status"], body.get("conclusion")) for body in self.bodies}
        self.assertEqual(verdicts["head-a"], ("completed", "success"))
        for ref in ("new-head", "new-merge", "head-b", "merge-b"):
            self.assertEqual(verdicts[ref], ("in_progress", None))

    def test_review_signal_resolving_several_prs_evaluates_each_once(self):
        prs = [self.pr(), self.pr(2, "head-b", "merge-b")]
        event = {"event": "pull_request_review", "head_sha": "merge-a", "pull_requests": [{"number": 2}]}
        self.assertEqual(self.invoke(prs, "merge-a", extra=["--review-run", "123"], review_run=event), 0)
        self.assert_counts(["head-a", "merge-a", "head-b", "merge-b"], [1, 2], ["/repos/demo/repo/actions/runs/123"])


class TestCommitStatuses(unittest.TestCase):
    def test_total_count_zero_is_not_pending(self):
        """GitHub reports state 'pending' on an empty statuses response. No
        repo in this org uses commit statuses, so reading the state rather
        than the count would hold every PR open forever."""
        status, conclusion, _, _ = evaluate(
            [run("build")],
            {"state": "pending", "total_count": 0, "statuses": []},
            [],
        )
        self.assertEqual((status, conclusion), ("completed", "success"))

    def test_failing_status_blocks(self):
        status, conclusion, _, summary = evaluate(
            [run("build")],
            {
                "state": "failure",
                "total_count": 1,
                "statuses": [{"context": "vercel", "state": "failure"}],
            },
            [],
        )
        self.assertEqual((status, conclusion), ("completed", "failure"))
        self.assertIn("vercel", summary)

    def test_real_pending_status_holds_the_gate(self):
        status, conclusion, _, _ = evaluate(
            [run("build")],
            {
                "state": "pending",
                "total_count": 1,
                "statuses": [{"context": "vercel", "state": "pending"}],
            },
            [],
        )
        self.assertEqual((status, conclusion), ("in_progress", None))


class TestSuiteQuiescence(unittest.TestCase):
    def test_suite_with_zero_runs_is_ignored(self):
        """`vercel` and `claude` suites sit at queued / zero runs permanently.
        Requiring every suite to be completed blocks every PR in the org."""
        status, conclusion, _, _ = evaluate(
            [run("build")],
            EMPTY_STATUSES,
            [suite(0, status="queued", suite_id=1), suite(1, suite_id=2)],
        )
        self.assertEqual((status, conclusion), ("completed", "success"))

    def test_suite_with_runs_outstanding_forces_in_progress(self):
        status, conclusion, _, _ = evaluate(
            [run("build")],
            EMPTY_STATUSES,
            [suite(0, status="queued", suite_id=1), suite(2, status="queued", suite_id=2)],
        )
        self.assertEqual((status, conclusion), ("in_progress", None))


class TestEmpty(unittest.TestCase):
    def test_nothing_at_all_is_success(self):
        """A docs-only PR with no applicable checks. Resolved by the
        reconciler dispatching, not by a timer."""
        status, conclusion, _, _ = evaluate([], EMPTY_STATUSES, [])
        self.assertEqual((status, conclusion), ("completed", "success"))

    def test_conclusion_is_none_whenever_status_is_not_completed(self):
        cases = [
            ([run("a", status="queued", conclusion=None)], EMPTY_STATUSES, []),
            ([run("a")], EMPTY_STATUSES, [suite(1, status="pending")]),
            (
                [run("a")],
                {
                    "state": "pending",
                    "total_count": 1,
                    "statuses": [{"context": "c", "state": "pending"}],
                },
                [],
            ),
        ]
        for runs, statuses, suites in cases:
            with self.subTest(runs=runs):
                status, conclusion, _, _ = evaluate(runs, statuses, suites)
                if status != "completed":
                    self.assertIsNone(conclusion)


class TestPendingOverridesTerminal(unittest.TestCase):
    """An in_progress verdict always posts, even over an earlier terminal gate.

    The gate reads fresh check state on every run, so a pending check at
    evaluation time is genuinely outstanding. Suppressing the in_progress to
    protect an earlier `completed` gate would let a PR merge while that check is
    still in flight, and its later failure would land after the merge. The
    The caller coalesces pending events into a refresh of every open PR;
    no monotonic guard belongs here.
    """

    def test_in_progress_posts_over_an_earlier_completed_gate(self):
        completed_gate = run(GATE_CHECK_NAME, run_id=1)
        pending = run("slow", status="in_progress", conclusion=None, run_id=2)
        with patch.object(pr_gatekeeper, "collect", return_value=([completed_gate, pending], EMPTY_STATUSES, [])), patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("demo/repo", "abc", "fake"), 0)
            self.assertEqual(post.call_args.args[2]["status"], "in_progress")

    def test_in_progress_republishes_over_an_earlier_in_progress_gate(self):
        prior_gate = run(GATE_CHECK_NAME, status="in_progress", conclusion=None, run_id=1)
        pending = run("slow", status="in_progress", conclusion=None, run_id=2)
        with patch.object(pr_gatekeeper, "collect", return_value=([prior_gate, pending], EMPTY_STATUSES, [])), patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("demo/repo", "abc", "fake")
            self.assertEqual(post.call_args.args[2]["status"], "in_progress")

    def test_in_progress_posts_when_no_gate_exists(self):
        pending = run("slow", status="in_progress", conclusion=None)
        with patch.object(pr_gatekeeper, "collect", return_value=([pending], EMPTY_STATUSES, [])), patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("demo/repo", "abc", "fake")
            body = post.call_args.args[2]
            self.assertEqual(body["status"], "in_progress")
            self.assertNotIn("conclusion", body)

    def test_terminal_verdict_posts(self):
        failed_gate = run(GATE_CHECK_NAME, conclusion="failure", run_id=1)
        green = run("build", run_id=2)
        with patch.object(pr_gatekeeper, "collect", return_value=([failed_gate, green], EMPTY_STATUSES, [])), patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("demo/repo", "abc", "fake")
            self.assertEqual(post.call_args.args[2]["conclusion"], "success")


class TestReviewEventContract(unittest.TestCase):
    def test_review_events_use_a_read_only_signal_and_trusted_followup(self):
        import json
        import re
        import yaml
        root = Path(__file__).resolve().parents[1]
        caller = yaml.safe_load((root / ".github/workflows/call-reusable-pr-gatekeeper.yml").read_text())
        events = caller.get("on", caller.get(True))
        self.assertEqual(set(events["pull_request_review"]["types"]), {"submitted", "edited", "dismissed"})
        self.assertEqual(caller["permissions"]["actions"], "read")
        signal = caller["jobs"]["review-event"]
        self.assertEqual(signal["uses"], "./.github/workflows/reusable-review-event.yml",
                         "the secrets guard must inspect the helper used by the caller")
        self.assertEqual(signal["permissions"], {"contents": "read"})
        self.assertEqual(signal["if"], "github.event_name == 'pull_request_review'")
        gate = caller["jobs"]["gatekeeper"]
        self.assertIn("github.event_name == 'workflow_run'", gate["if"])
        self.assertIn("github.event.workflow_run.event == 'pull_request_review'", gate["if"])
        self.assertIn("github.event.workflow_run.pull_requests[0].head.sha", gate["with"]["head_sha"])
        self.assertEqual(set(events["pull_request_target"]["types"]), {"opened", "synchronize", "reopened"})
        self.assertIn("github.event_name == 'pull_request_target'", gate["if"])
        self.assertEqual(gate["with"]["seed"], "${{ github.event_name == 'pull_request_target' }}")
        helper = yaml.safe_load((root / ".github/workflows/reusable-review-event.yml").read_text())
        # This review-event caller/helper pair must neither accept nor
        # reference workflow secrets. Other reusables have separate policies.
        for contract in (signal, helper):
            self.assertNotIn("secrets", yaml.safe_dump(contract).casefold())
        self.assertEqual(helper["permissions"], {"contents": "read"})
        self.assertTrue(all("uses" not in step for step in helper["jobs"]["signal"]["steps"]))
        # Private repos run on the self-hosted pool (immune to the Actions
        # budget); public repos are not served by any org runner group.
        for name, job in helper["jobs"].items():
            with self.subTest(job=name):
                self.assertNotIn("uses", job,
                                 "review signal routing must be inspected locally")
                self.assertEqual(job.get("timeout-minutes"), 2,
                                 "every review signal job must release its runner within two minutes")
                runner_expression = job.get("runs-on")
                self.assertIsInstance(runner_expression, str,
                                      "review signals require private/public conditional routing")
                routing = re.fullmatch(
                    r"\s*\$\{\{\s*github\.event\.repository\.private\s*&&\s*"
                    r"fromJSON\(\s*'([^']+)'\s*\)\s*\|\|\s*'([^']+)'\s*\}\}\s*",
                    runner_expression, re.IGNORECASE)
                self.assertIsNotNone(routing,
                                     "review signals require a private pool and a literal hosted fallback")
                try:
                    labels = json.loads(routing.group(1))
                except json.JSONDecodeError:
                    self.fail("private review signal runner labels must be valid JSON")
                self.assertIsInstance(labels, list,
                                      "private review signal runner labels must be a JSON list")
                self.assertEqual(sorted(labels, key=str), ["Linux", "self-hosted"],
                                 "private review signals must use only the Linux pool labels")
                self.assertEqual(routing.group(2), "ubuntu-latest",
                                 "public review signals must use the approved hosted runner")

    def test_reusable_gatekeeper_seed_input_contract(self):
        import shutil
        import subprocess
        import yaml
        root = Path(__file__).resolve().parents[1]
        reusable = yaml.safe_load((root / ".github/workflows/reusable-pr-gatekeeper.yml").read_text())
        events = reusable.get("on", reusable.get(True))
        inputs = events["workflow_call"]["inputs"]
        self.assertIn("seed", inputs)
        self.assertEqual(inputs["seed"]["type"], "boolean")
        self.assertFalse(inputs["seed"]["default"])
        step = next(s for s in reusable["jobs"]["all-checks-passed"]["steps"]
                    if s["name"] == "Evaluate and post the gate")
        self.assertEqual(step["env"]["SEED"], "${{ inputs.seed }}")
        self.assertIn('if [ "$SEED" = "true" ]; then args+=(--seed); fi', step["run"])

        if not shutil.which("bash"):
            return

        script = step["run"]
        guard_block = script.split('python3 "${RUNNER_TEMP}/pr-gatekeeper.py"')[0]
        test_wrapper = f"""
set -euo pipefail
{guard_block}
echo "ARGS: ${{args[*]}}"
"""
        cases = [
            ("running", "42", "--persona-state running --persona-pr 42", False),
            ("failed", "42", "--persona-state failed --persona-pr 42", False),
            ("", "", "", False),
            ("waiting", "42", "", True),
            ("", "42", "", True),
            ("running", "", "", True),
            ("running", "0", "", True),
        ]
        for state, pr_num, expected_args, expect_warning in cases:
            with self.subTest(state=state, pr=pr_num):
                env = dict(os.environ, REVIEW_RUN_ID="", SEED="",
                           PERSONA_STATE=state, PERSONA_PR=pr_num)
                res = subprocess.run(["bash", "-c", test_wrapper],
                                     env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(res.returncode, 0, res.stderr)
                if expected_args:
                    self.assertIn(expected_args, res.stdout)
                else:
                    self.assertNotIn("--persona-state", res.stdout)
                    self.assertNotIn("--persona-pr", res.stdout)
                if expect_warning:
                    self.assertIn("::warning::Invalid persona signal inputs", res.stdout + res.stderr)
                else:
                    self.assertNotIn("::warning::Invalid persona signal inputs", res.stdout + res.stderr)


class TestSeed(unittest.TestCase):
    def test_seed_posts_in_progress_without_evaluation(self):
        for persona_required in ("true", "false"):
            with self.subTest(persona_required=persona_required):
                with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": persona_required}), \
                     patch.object(pr_gatekeeper, "_get", return_value={"check_runs": []}) as get_mock, \
                     patch.object(pr_gatekeeper, "collect") as collect_mock, \
                     patch.object(pr_gatekeeper, "_post") as post_mock:
                    self.assertEqual(pr_gatekeeper.report("demo/repo", "abc", "fake", seed=True), 0)
                    get_mock.assert_called_once()
                    collect_mock.assert_not_called()
                    self.assertEqual(post_mock.call_count, 1)
                    body = post_mock.call_args.args[2]
                    self.assertEqual(body["name"], GATE_CHECK_NAME)
                    self.assertEqual(body["head_sha"], "abc")
                    self.assertEqual(body["status"], "in_progress")
                    self.assertNotIn("conclusion", body)
                    self.assertEqual(body["output"]["title"], "Gate seeded, awaiting CI")
                    self.assertIn("Gate seeded on PR event", body["output"]["summary"])
                    self.assertIn('<!-- gatekeeper-refs:["abc"] -->', body["output"]["summary"])

    def test_seed_dry_run_does_not_post(self):
        with patch.object(pr_gatekeeper, "has_evaluated_gate", return_value=False), \
             patch.object(pr_gatekeeper, "_post") as post_mock:
            self.assertEqual(pr_gatekeeper.report("demo/repo", "abc", "fake", dry_run=True, seed=True), 0)
            post_mock.assert_not_called()

    def test_seed_post_failure_returns_error_code(self):
        with patch.object(pr_gatekeeper, "has_evaluated_gate", return_value=False), \
             patch.object(pr_gatekeeper, "_post", side_effect=urllib.error.URLError("network error")), \
             patch("sys.stderr"):
            self.assertEqual(pr_gatekeeper.report("demo/repo", "abc", "fake", seed=True), 2)

    def test_main_seed_invokes_report_with_seed(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), \
             patch.object(pr_gatekeeper, "report", return_value=0) as report_mock:
            rc = pr_gatekeeper.main(["--repo", "demo/repo", "--sha", "abc", "--seed"])
            self.assertEqual(rc, 0)
            report_mock.assert_called_once_with("demo/repo", "abc", "fake", False, seed=True, require_ci=False)

    def test_main_seed_mutually_exclusive_with_reconcile(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr, \
             patch.object(pr_gatekeeper, "report") as mock_report:
            with self.assertRaises(SystemExit) as cm:
                pr_gatekeeper.main(["--repo", "demo/repo", "--reconcile-open", "--seed"])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("--seed cannot be used with --reconcile-open", mock_stderr.getvalue())
            mock_report.assert_not_called()

    def test_main_seed_mutually_exclusive_with_review_run(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_stderr, \
             patch.object(pr_gatekeeper, "resolve_review_heads") as mock_resolve, \
             patch.object(pr_gatekeeper, "report") as mock_report:
            with self.assertRaises(SystemExit) as cm:
                pr_gatekeeper.main(["--repo", "demo/repo", "--sha", "abc", "--review-run", "123", "--seed"])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("--seed cannot be used with --review-run", mock_stderr.getvalue())
            mock_resolve.assert_not_called()
            mock_report.assert_not_called()


class TestPersonaReview(unittest.TestCase):
    def review(self, state="APPROVED", sha="head", ident=1, **fields):
        return dict({"id": ident, "state": state, "commit_id": sha,
                     "submitted_at": "2026-09-19T00:00:00Z",
                     "user": {"id": pr_gatekeeper.PERSONA_USER_ID, "type": "Bot"}}, **fields)

    def test_only_current_bot_approval_passes(self):
        verdict = pr_gatekeeper.persona_verdict
        self.assertEqual(verdict([self.review()], "head"), ("completed", "success"))
        for reviews in ([], [self.review(sha="old")],
                        [self.review(user={"id": 1, "type": "Bot"})],
                        [self.review(user={"id": pr_gatekeeper.PERSONA_USER_ID, "type": "User"})],
                        [self.review(submitted_at=None)], [self.review(state="PENDING")]):
            with self.subTest(reviews=reviews):
                self.assertEqual(verdict(reviews, "head"), ("in_progress", None))

    def test_latest_verdict_and_dismissal(self):
        reviews = [self.review(), self.review("CHANGES_REQUESTED", ident=2)]
        self.assertEqual(pr_gatekeeper.persona_verdict(reviews, "head"), ("completed", "failure"))
        reviews[-1]["state"] = "DISMISSED"
        self.assertEqual(pr_gatekeeper.persona_verdict(reviews, "head"), ("in_progress", None))
        reviews[-1]["state"] = "APPROVED"
        self.assertEqual(pr_gatekeeper.persona_verdict(reviews, "head"), ("completed", "success"))

    def test_every_pr_sharing_sha_requires_its_own_review(self):
        prs = [{"number": n, "head": {"sha": "head"}} for n in (1, 2)]
        with patch.object(pr_gatekeeper, "_get", side_effect=[prs, [self.review()], []]):
            checks = pr_gatekeeper.collect_persona_checks("org/repo", "head", "token")
        self.assertEqual([c["status"] for c in checks], ["completed", "in_progress"])

    def test_review_pagination(self):
        reviews = [self.review(sha="old", ident=n) for n in range(100)]
        prs = [{"number": 1, "head": {"sha": "head"}}]
        with patch.object(pr_gatekeeper, "_get", side_effect=[prs, reviews, [self.review()]]):
            self.assertEqual(pr_gatekeeper.collect_persona_checks("org/repo", "head", "token")[0]["conclusion"], "success")

    def test_enforcement_holds_gate_and_read_failure_blocks(self):
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[{"number": 1, "head": {"sha": "head"}}], []]), \
             patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("org/repo", "head", "token")
            self.assertEqual(post.call_args.args[2]["status"], "in_progress")
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "_get", side_effect=ValueError("invalid reviews")), \
             patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("org/repo", "head", "token")
            self.assertEqual(post.call_args.args[2]["conclusion"], "failure")



    def test_dispatch_wait_running_and_recovered_verdict(self):
        sha = "a" * 40
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = {"number": 42, "head": {"sha": sha}, "merge_commit_sha": None,
              "updated_at": now}
        old = self.review("CHANGES_REQUESTED", sha="old")
        current = self.review("APPROVED", sha=sha, ident=2)
        prior = []
        for state, reviews, expected_status, expected_title in (
                ("", [old], "in_progress", "waiting for review dispatch"),
                ("running", [old], "in_progress", "review running"),
                ("", [old, current], "completed", "All checks passed")):
            with self.subTest(state=state, reviews=len(reviews)), \
                 patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
                 patch.object(pr_gatekeeper, "collect", return_value=(prior, EMPTY_STATUSES, [])), \
                 patch.object(pr_gatekeeper, "_get", side_effect=[[pr], reviews]), \
                 patch.object(pr_gatekeeper, "_post") as post:
                self.assertEqual(pr_gatekeeper.report("org/repo", sha, "token",
                               persona_state=state, persona_pr=42 if state else 0), 0)
                body = post.call_args.args[2]
                self.assertEqual(body["status"], expected_status)
                self.assertIn(expected_title, body["output"]["title"])
                prior = [dict(body, id=100, app={"id": pr_gatekeeper.GATE_APP_ID})]

    def test_overdue_and_failed_dispatch_are_visible(self):
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        overdue = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")

        fresh_check = {"number": 42, "head_sha": "a" * 40, "updated_at": fresh,
                       "status": "in_progress", "conclusion": None}
        progress = pr_gatekeeper.persona_progress([fresh_check], [])
        self.assertIn("waiting for review dispatch", progress[0])
        self.assertNotIn("alert:", progress[0])
        self.assertEqual(fresh_check["status"], "in_progress")

        overdue_check = {"number": 42, "head_sha": "a" * 40, "updated_at": overdue,
                         "status": "in_progress", "conclusion": None}
        progress = pr_gatekeeper.persona_progress([overdue_check], [])
        self.assertIn("waiting for review dispatch; alert: undispatched for", progress[0])

        bad_stamp_check = {"number": 42, "head_sha": "a" * 40, "updated_at": "not-a-timestamp",
                           "status": "in_progress", "conclusion": None}
        progress = pr_gatekeeper.persona_progress([bad_stamp_check], [])
        self.assertIn("waiting for review dispatch", progress[0])
        self.assertNotIn("alert:", progress[0])

        failed_check = {"number": 42, "head_sha": "a" * 40, "updated_at": fresh,
                        "status": "in_progress", "conclusion": None,
                        "marker": pr_gatekeeper.persona_marker(42, ("in_progress", None))}
        progress = pr_gatekeeper.persona_progress([failed_check], [], "failed", 42, "a" * 40)
        self.assertIn("review dispatch or job failed", progress[0])
        self.assertEqual((failed_check["status"], failed_check["conclusion"]), ("completed", "failure"))
        self.assertEqual(failed_check["marker"], pr_gatekeeper.persona_marker(42, ("in_progress", None)))

    def test_running_signal_survives_gate_refresh_only_for_its_head(self):
        now = datetime.now(timezone.utc)
        overdue_stamp = (now - timedelta(minutes=125)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_stamp = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha = "a" * 40
        prior = {"name": GATE_CHECK_NAME, "app": {"id": pr_gatekeeper.GATE_APP_ID}, "output": {"summary":
                 f"<!-- grumpy-dispatch:42:{sha}:running:{overdue_stamp} -->"}}
        check = {"number": 42, "head_sha": sha, "updated_at": fresh_stamp,
                 "status": "in_progress", "conclusion": None}
        self.assertIn("review running; alert: running for", pr_gatekeeper.persona_progress([check], [prior])[0])

        fresh_prior = {"name": GATE_CHECK_NAME, "app": {"id": pr_gatekeeper.GATE_APP_ID}, "output": {"summary":
                       f"<!-- grumpy-dispatch:42:{sha}:running:{fresh_stamp} -->"}}
        fresh_progress = pr_gatekeeper.persona_progress([check], [fresh_prior])[0]
        self.assertIn("review running", fresh_progress)
        self.assertNotIn("alert:", fresh_progress)

        newer = dict(check, head_sha="b" * 40)
        self.assertIn("waiting for review dispatch", pr_gatekeeper.persona_progress([newer], [prior])[0])

    def test_signal_and_prior_state_require_exact_head_and_trusted_run(self):
        sha = "a" * 40
        check = {"number": 42, "head_sha": sha, "updated_at": "2026-09-23T00:00:00Z",
                 "status": "in_progress", "conclusion": None}
        prior = {"name": GATE_CHECK_NAME, "app": {"id": 1}, "output": {"summary":
                 f"<!-- grumpy-dispatch:42:{sha}:failed:2099-01-01T00:00:00Z -->"}}
        self.assertIn("waiting for review dispatch",
                      pr_gatekeeper.persona_progress([check], [prior])[0])
        self.assertIn("waiting for review dispatch",
                      pr_gatekeeper.persona_progress([check], [], "failed", 42, "b" * 40)[0])
        embedded = dict(prior, app={"id": pr_gatekeeper.GATE_APP_ID}, output={"summary":
                        f"Other check <!-- grumpy-dispatch:42:{sha}:failed:2099-01-01T00:00:00Z -->"})
        self.assertIn("waiting for review dispatch",
                      pr_gatekeeper.persona_progress([check], [embedded])[0])

    def test_main_rejects_invalid_persona_signal_pairs(self):
        base = ["--repo", "org/repo", "--sha", "a" * 40]
        invalid = (["--seed", "--persona-state", "running", "--persona-pr", "42"],
                   ["--persona-state", "running"],
                   ["--persona-pr", "42"],
                   ["--persona-state", "running", "--persona-pr", "-1"],
                   ["--review-run", "123", "--persona-state", "running", "--persona-pr", "42"])
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), \
             patch.object(pr_gatekeeper, "report") as report_mock, \
             patch("sys.stderr", new_callable=io.StringIO):
            for extra in invalid:
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    pr_gatekeeper.main(base + extra)
            with self.assertRaises(SystemExit):
                pr_gatekeeper.main(["--repo", "org/repo", "--reconcile-open",
                                    "--persona-state", "running", "--persona-pr", "42"])
            report_mock.assert_not_called()
            self.assertEqual(pr_gatekeeper.main(base + ["--persona-state", "running",
                                                      "--persona-pr", "42"]), report_mock.return_value)
            report_mock.assert_called_once_with("org/repo", "a" * 40, "fake", False,
                                                persona_state="running", persona_pr=42, require_ci=False)


class TestReviewEventResolution(unittest.TestCase):

    def pr(self):
        return {"number": 7, "head": {"sha": "head", "ref": "fix", "repo": {"id": 42}},
                "merge_commit_sha": "merge"}

    def test_empty_fork_links_resolve_merge_sha_to_head(self):
        event = {"event": "pull_request_review", "head_sha": "merge", "pull_requests": []}
        with patch.object(pr_gatekeeper, "_get", side_effect=[event, [self.pr()]]):
            self.assertEqual(pr_gatekeeper.resolve_review_heads("org/repo", 123, "token"), ["head"])

    def test_delayed_review_resolves_current_head_by_fork_branch(self):
        event = {"event": "pull_request_review", "head_sha": "old-merge", "pull_requests": [],
                 "head_repository": {"id": 42}, "head_branch": "fix"}
        with patch.object(pr_gatekeeper, "_get", side_effect=[event, [self.pr()]]):
            self.assertEqual(pr_gatekeeper.resolve_review_heads("org/repo", 123, "token"), ["head"])

    def test_missing_metadata_does_not_match_null_merge_sha(self):
        event = {"event": "pull_request_review", "head_repository": None}
        pr = self.pr(); pr["merge_commit_sha"] = None
        with patch.object(pr_gatekeeper, "_get", side_effect=[event, [pr]]):
            with self.assertRaisesRegex(ValueError, "associate"):
                pr_gatekeeper.resolve_review_heads("org/repo", 123, "token")

    def test_unassociated_signal_posts_blocking_fallback(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), \
             patch.object(pr_gatekeeper, "resolve_review_heads", side_effect=ValueError("unassociated")), \
             patch.object(pr_gatekeeper, "previous_publication_refs", return_value=set()), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "org/repo", "--sha", "merge", "--review-run", "123"]), 2)
            self.assertEqual(post.call_args.args[2]["head_sha"], "merge")
            self.assertEqual(post.call_args.args[2]["conclusion"], "failure")

    def test_approval_and_dismissal_update_both_refs_using_actual_head(self):
        for state, expected in (("APPROVED", "success"), ("DISMISSED", None)):
            review = TestPersonaReview().review(state=state)
            with self.subTest(state=state), \
                 patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
                 patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
                 patch.object(pr_gatekeeper, "_get", side_effect=[[self.pr()], [review]]), \
                 patch.object(pr_gatekeeper, "_post") as post:
                pr_gatekeeper.report("org/repo", "merge", "token")
                bodies = [call.args[2] for call in post.call_args_list]
                self.assertEqual({b["head_sha"] for b in bodies}, {"head", "merge"})
                self.assertTrue(all(b.get("conclusion") == expected for b in bodies))
                self.assertTrue(all(b["status"] == ("completed" if expected else "in_progress") for b in bodies))

    def test_merge_event_requires_every_review_sharing_the_head(self):
        sibling = dict(self.pr(), number=8, merge_commit_sha="other-merge")
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[self.pr(), sibling], [TestPersonaReview().review()], []]) as get, \
             patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("org/repo", "merge", "token")
            self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {"head", "merge", "other-merge"})
            self.assertTrue(all(c.args[2]["status"] == "in_progress" for c in post.call_args_list))
            self.assertTrue(any("/pulls/8/reviews" in c.args[0] for c in get.call_args_list))

    def test_pr_metadata_failure_recovers_previous_head_and_merge_destinations(self):
        head, merge = "a" * 40, "b" * 40
        previous = {"id": 9, "name": GATE_CHECK_NAME, "app": {"id": pr_gatekeeper.GATE_APP_ID},
                    "output": {"summary": '<!-- gatekeeper-refs:["' + head + '", "' + merge + '"] --> <!-- gatekeeper-refs-complete -->'}}
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[ValueError("PR list unavailable"), {"check_runs": [previous]}]), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", head, "token"), 2)
            self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {head, merge})
            self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

    def test_failed_recovery_retains_older_complete_metadata_across_pages(self):
        head, merge = "a" * 40, "b" * 40
        old = {"id": 1, "name": GATE_CHECK_NAME, "app": {"id": pr_gatekeeper.GATE_APP_ID},
               "output": {"summary": '<!-- gatekeeper-refs:["' + head + '", "' + merge + '"] --> <!-- gatekeeper-refs-complete -->'}}
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=ValueError("API outage")), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", head, "token"), 2)
            incomplete = post.call_args.args[2]
        self.assertNotIn("<!-- gatekeeper-refs-complete -->", incomplete["output"]["summary"])
        incomplete.update(id=2, app={"id": pr_gatekeeper.GATE_APP_ID})
        page_one = [incomplete] + [{"id": n, "name": "other"} for n in range(99)]
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[ValueError("PR metadata unavailable"),
                          {"check_runs": page_one}, {"check_runs": [old]}]), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", head, "token"), 2)
            self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {head, merge})
            self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

    def test_overlapping_publications_share_one_repository_queue(self):
        import yaml
        root = Path(__file__).resolve().parent.parent
        caller = yaml.safe_load((root / ".github/workflows/call-reusable-pr-gatekeeper.yml").read_text())
        self.assertIn("concurrency", caller)
        self.assertEqual(caller["jobs"]["gatekeeper"]["concurrency"],
                         {"group": "pr-gatekeeper-${{ github.repository }}",
                          "cancel-in-progress": False, "queue": "max"})
        self.assertTrue(caller["jobs"]["gatekeeper"]["with"]["reconcile_open"])
        for signal in ("recover-persona", "review-event"):
            self.assertIn(signal, caller["jobs"])
        holders = {name for name, job in caller["jobs"].items() if "concurrency" in job}
        self.assertEqual(holders, {"gatekeeper"})

    def test_partial_publication_attempts_all_refs_and_reports_failure(self):
        for failed_ref in ("head", "merge"):
            def fail_one(path, token, body):
                if body["head_sha"] == failed_ref:
                    raise urllib.error.URLError("temporary failure")
            with self.subTest(failed_ref=failed_ref), \
                 patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
                 patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
                 patch.object(pr_gatekeeper, "_get", side_effect=[[self.pr()], [TestPersonaReview().review("CHANGES_REQUESTED")]]), \
                 patch.object(pr_gatekeeper, "_post", side_effect=fail_one) as post:
                self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token"), 2)
                self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {"head", "merge"})
                self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

    def test_review_read_failure_replaces_both_previous_green_gates(self):
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[self.pr()], ValueError("unreadable"), {"check_runs": []}]), \
             patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("org/repo", "head", "token")
            self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {"head", "merge"})
            self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

    def test_mirrored_gate_preserves_red_merge_commit_checks(self):
        def collect(repo, sha, token):
            return ([run("merge test", conclusion="failure")] if sha == "merge" else [], EMPTY_STATUSES, [])
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "collect", side_effect=collect), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[self.pr()], [TestPersonaReview().review()]]), \
             patch.object(pr_gatekeeper, "_post") as post:
            pr_gatekeeper.report("org/repo", "head", "token")
            self.assertEqual(len(post.call_args_list), 2)
            self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

class TestPublication(unittest.TestCase):
    def test_failed_destination_does_not_skip_remaining_destinations(self):
        for status, conclusion in (("in_progress", None), ("completed", "failure")):
            with self.subTest(status=status), \
                 patch.object(pr_gatekeeper, "_post", side_effect=[urllib.error.URLError("offline"), {}]) as post, \
                 patch("sys.stderr"):
                self.assertEqual(pr_gatekeeper.publish("org/repo", {"a", "b"}, "token",
                                 status, conclusion, "title", "summary", False), 2)
                self.assertEqual([c.args[2]["head_sha"] for c in post.call_args_list], ["a", "b"])
                for call in post.call_args_list:
                    self.assertEqual(call.args[2]["status"], status)
                    self.assertEqual("conclusion" in call.args[2], status == "completed")


class TestRequireCiAndMalformedManifest(unittest.TestCase):
    """`--require-ci` reaches every report() call, and a malformed manifest
    still resolves destinations before its (now skipped) persona reads."""

    def test_reconcile_open_forwards_require_ci_to_every_report(self):
        # The reusable caller adds --require-ci and --reconcile-open together, so
        # the flag must survive the reconciliation path, including its default
        # when a caller omits it.
        for require_ci, expected in ((True, True), (False, False)):
            with self.subTest(require_ci=require_ci), \
                 patch.object(pr_gatekeeper, "_list_all",
                              return_value=[{"number": 1, "head": {"sha": "head"}}]), \
                 patch.object(pr_gatekeeper, "report", return_value=0) as report_mock:
                self.assertEqual(pr_gatekeeper.reconcile_open(
                    "org/repo", "token", False, sha="head", require_ci=require_ci), 0)
            self.assertEqual(report_mock.call_count, 1)
            self.assertIs(report_mock.call_args.kwargs.get("require_ci"), expected)
            self.assertEqual(report_mock.call_args.args[1], "head")

    def test_reconcile_open_error_fallback_forwards_require_ci(self):
        with patch.object(pr_gatekeeper, "_list_all", side_effect=ValueError("PR list unavailable")), \
             patch.object(pr_gatekeeper, "report", return_value=2) as report_mock:
            self.assertEqual(pr_gatekeeper.reconcile_open(
                "org/repo", "token", False, sha="head", require_ci=True), 2)
        error_calls = [c for c in report_mock.call_args_list if "error" in c.kwargs]
        self.assertTrue(error_calls, "the read failure must post a blocking fallback")
        self.assertTrue(all(c.kwargs.get("require_ci") is True for c in report_mock.call_args_list))

    def test_main_reconcile_open_forwards_require_ci(self):
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_TOKEN": "fake"}), \
             patch.object(pr_gatekeeper, "reconcile_open", return_value=0) as reconcile_mock:
            self.assertEqual(pr_gatekeeper.main(
                ["--repo", "org/repo", "--reconcile-open", "--require-ci"]), 0)
        self.assertIs(reconcile_mock.call_args.kwargs.get("require_ci"), True)

    def test_malformed_manifest_resolves_destinations_and_skips_discarded_review_reads(self):
        import json
        pr = {"number": 7, "head": {"sha": "head"}, "merge_commit_sha": "merge"}
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[pr], {"check_runs": []}]), \
             patch.object(pr_gatekeeper, "collect_persona_checks") as persona, \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token",
                                                  required_workflows="not JSON"), 2)
        # The re-raised parse error discards review reads, so none may be made.
        persona.assert_not_called()
        self.assertEqual({c.args[2]["head_sha"] for c in post.call_args_list}, {"head", "merge"})
        self.assertTrue(all(c.args[2]["conclusion"] == "failure" for c in post.call_args_list))

    def test_valid_manifest_still_reads_persona_reviews(self):
        import json
        pr = {"number": 7, "head": {"sha": "head"}, "merge_commit_sha": "merge"}
        valid = json.dumps([{"path": ".github/workflows/test.yml",
                             "event": "pull_request", "ref": "head"}])
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[pr]]), \
             patch.object(pr_gatekeeper, "collect_persona_checks", return_value=[]) as persona, \
             patch.object(pr_gatekeeper, "collect", return_value=([], EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "required_workflow_checks", return_value=[]), \
             patch.object(pr_gatekeeper, "_post"):
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token",
                                                  required_workflows=valid, workflow_metadata={}), 0)
        persona.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
