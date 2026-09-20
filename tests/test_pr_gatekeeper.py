#!/usr/bin/env python3
"""Unit tests for scripts/pr-gatekeeper.py.

No network: `evaluate()` is pure, so every case here is a hand-built payload.
The cases are the ones that would each, on their own, lock merges org-wide or
let a red PR through.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
from pathlib import Path

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
        for bad in ("cancelled", "timed_out", "action_required", "failure", None):
            with self.subTest(conclusion=bad):
                status, conclusion, _, _ = evaluate(
                    [run("x", conclusion=bad)], EMPTY_STATUSES, []
                )
                self.assertEqual((status, conclusion), ("completed", "failure"))

    def test_skipped_neutral_stale_do_not_block(self):
        status, conclusion, _, _ = evaluate(
            [
                run("a", conclusion="skipped"),
                run("b", conclusion="neutral", run_id=2),
                run("c", conclusion="stale", run_id=3),
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


class TestDedupe(unittest.TestCase):
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
                self.assertEqual(pr_gatekeeper.report("demo/repo", "abc", "fake"), 0)
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
        with patch.object(pr_gatekeeper, "_get", side_effect=[{"check_runs": []}, first, second, {"check_suites": []}]):
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
            ],
        ) as get:
            self.assertEqual(pr_gatekeeper.collect("demo/repo", "abc", "fake"), ([], EMPTY_STATUSES, []))
        self.assertIn("commits/codex%2Ffix%20gate/check-runs", get.call_args_list[2].args[0])

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
    caller's `queue: max` is what fixes the original frozen-gate bug; no
    monotonic guard belongs here.
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
        import yaml
        root = Path(__file__).resolve().parents[1]
        caller = yaml.safe_load((root / ".github/workflows/call-reusable-pr-gatekeeper.yml").read_text())
        events = caller.get("on", caller.get(True))
        self.assertEqual(set(events["pull_request_review"]["types"]), {"submitted", "edited", "dismissed"})
        signal = caller["jobs"]["review-event"]
        self.assertEqual(signal["permissions"], {"contents": "read"})
        self.assertEqual(signal["if"], "github.event_name == 'pull_request_review'")
        gate = caller["jobs"]["gatekeeper"]
        self.assertIn("github.event_name == 'workflow_run'", gate["if"])
        self.assertIn("github.event.workflow_run.event == 'pull_request_review'", gate["if"])
        self.assertIn("github.event.workflow_run.pull_requests[0].head.sha", gate["with"]["head_sha"])
        helper = yaml.safe_load((root / ".github/workflows/reusable-review-event.yml").read_text())
        self.assertEqual(helper["permissions"], {"contents": "read"})
        self.assertTrue(all("uses" not in step for step in helper["jobs"]["signal"]["steps"]))


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
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.main(["--repo", "org/repo", "--sha", "merge", "--review-run", "123"]), 0)
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

    def test_review_read_failure_replaces_both_previous_green_gates(self):
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "true"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[[self.pr()], ValueError("unreadable")]), \
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

if __name__ == "__main__":
    unittest.main(verbosity=2)
