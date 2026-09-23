"""Queued workflows must block before their jobs have check runs."""

import unittest
from unittest.mock import patch
import urllib.error

from test_pr_gatekeeper import EMPTY_STATUSES, pr_gatekeeper


def workflow(ident=1, **fields):
    return {"id": ident, "workflow_id": 10, "head_sha": "head",
            "head_branch": "feature", "path": ".github/workflows/test.yml",
            "event": "pull_request", "run_attempt": 1,
            "status": "completed", "conclusion": "success", **fields}


class WorkflowInventoryTests(unittest.TestCase):
    def verdict(self, runs):
        with patch.object(pr_gatekeeper, "_get", return_value={"workflow_runs": runs, "total_count": len(runs)}):
            checks = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
        return pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[:2]

    def test_zero_job_workflows_wait_for_every_nonterminal_state(self):
        for status in ("queued", "in_progress", "waiting", "pending", "requested", None):
            with self.subTest(status=status):
                self.assertEqual(self.verdict([workflow(status=status, conclusion=None)]), ("in_progress", None))

    def test_failed_and_cancelled_current_runs_block(self):
        for conclusion in ("failure", "cancelled", "timed_out", "action_required", None):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.verdict([workflow(conclusion=conclusion)]), ("completed", "failure"))

    def test_latest_attempt_and_replacement_run_win(self):
        self.assertEqual(self.verdict([workflow(run_attempt=1, conclusion="failure"),
                                       workflow(run_attempt=2)]), ("completed", "success"))
        self.assertEqual(self.verdict([workflow(ident=2), workflow(ident=1, conclusion="cancelled")]),
                         ("completed", "success"))
        self.assertEqual(self.verdict([workflow(run_attempt=2, status="queued", conclusion=None), workflow()]),
                         ("in_progress", None))

    def test_old_heads_are_ignored_but_distinct_events_and_workflows_are_not(self):
        self.assertEqual(self.verdict([workflow(head_sha="old", conclusion="cancelled"), workflow(ident=2)]),
                         ("completed", "success"))
        for other in (workflow(ident=2, event="push", status="queued"),
                      workflow(ident=2, workflow_id=11, status="queued"),
                      workflow(ident=2, head_branch="another", status="queued")):
            self.assertEqual(self.verdict([workflow(), other]), ("in_progress", None))

    def test_only_explicit_gatekeeper_path_is_excluded(self):
        self.assertEqual(self.verdict([workflow(path=pr_gatekeeper.GATE_WORKFLOW_PATH, status="in_progress")]),
                         ("completed", "success"))
        self.assertEqual(self.verdict([workflow(name="PR Gatekeeper", status="queued")]), ("in_progress", None))

    def test_pagination_reads_late_pending_workflow(self):
        first = [workflow(ident=n + 1, workflow_id=n + 1) for n in range(100)]
        with patch.object(pr_gatekeeper, "_get", side_effect=[
                {"workflow_runs": first, "total_count": 101},
                {"workflow_runs": [workflow(ident=101, workflow_id=101, status="queued")], "total_count": 101},
        ]) as get:
            checks = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
        self.assertEqual(len(checks), 101)
        self.assertIn("head_sha=head&per_page=100&page=2", get.call_args.args[0])
        self.assertEqual(pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[:2], ("in_progress", None))

    def test_malformed_or_truncated_inventory_fails_closed(self):
        for payload in ({}, {"workflow_runs": [], "total_count": 1001},
                        {"workflow_runs": [workflow(head_sha=None)], "total_count": 1},
                        {"workflow_runs": [workflow(path=None)], "total_count": 1}):
            with self.subTest(payload=payload), patch.object(pr_gatekeeper, "_get", return_value=payload):
                with self.assertRaises(ValueError):
                    pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")

    def test_workflow_api_failure_posts_blocking_gate(self):
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[
                 {"check_runs": []}, EMPTY_STATUSES, {"check_suites": []}, urllib.error.URLError("unavailable")]), \
             patch.object(pr_gatekeeper, "previous_publication_refs", return_value=set()), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token"), 2)
        self.assertEqual(post.call_args.args[2]["conclusion"], "failure")

    def test_report_cannot_publish_green_with_a_queued_zero_job_workflow(self):
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "_get", side_effect=[
                 {"check_runs": []}, EMPTY_STATUSES, {"check_suites": []},
                 {"workflow_runs": [workflow(status="queued", conclusion=None)], "total_count": 1}]), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token"), 0)
        self.assertEqual(post.call_args.args[2]["status"], "in_progress")
        self.assertNotIn("conclusion", post.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
