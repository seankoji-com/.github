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
            checks, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
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
        # The API replaces a rerun's entry with its current attempt.
        self.assertEqual(self.verdict([workflow(run_attempt=2)]), ("completed", "success"))
        self.assertEqual(self.verdict([workflow(ident=2), workflow(ident=1, conclusion="cancelled")]),
                         ("completed", "success"))
        self.assertEqual(self.verdict([workflow(run_attempt=2, status="queued", conclusion=None)]),
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
        with patch.dict(pr_gatekeeper.os.environ, {"GITHUB_RUN_ID": "1"}):
            self.assertEqual(self.verdict([workflow(path=".github/workflows/renamed-gate.yml", status="in_progress")]),
                             ("completed", "success"))
            self.assertEqual(self.verdict([workflow(ident=2, status="queued")]), ("in_progress", None))

    def test_workflow_labels_cannot_inject_markdown_and_include_recovery_link(self):
        run = workflow(head_branch="`[unsafe](https://invalid.example)`" + "x" * 500,
                       path=".github/workflows/`[unsafe](https://invalid.example).yml")
        with patch.object(pr_gatekeeper, "_get", return_value={"workflow_runs": [run], "total_count": 1}):
            checks, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
        name = checks[0]["name"]
        self.assertLess(len(name), 500)
        self.assertEqual(name.count("`"), 6)
        self.assertIn("https://github.com/org/repo/actions/runs/1", name)
        self.assertIn("rerun unsuccessful runs", name)

    def test_summary_is_bounded_without_dropping_destination_markers(self):
        checks = [{"name": str(n) + "x" * 500, "id": n, "status": "queued"} for n in range(200)]
        with patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "false"}), \
             patch.object(pr_gatekeeper, "collect", return_value=(checks, EMPTY_STATUSES, [])), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token"), 0)
        summary = post.call_args.args[2]["output"]["summary"]
        self.assertLessEqual(len(summary.encode("utf-8")), 60000)
        self.assertIn("Additional details omitted", summary)
        self.assertIn('<!-- gatekeeper-refs:["head"] -->', summary)
        self.assertIn("<!-- gatekeeper-refs-complete -->", summary)

    def test_pagination_reads_late_pending_workflow(self):
        first = [workflow(ident=n + 1, workflow_id=n + 1) for n in range(100)]
        with patch.object(pr_gatekeeper, "_get", side_effect=[
                {"workflow_runs": first, "total_count": 101},
                {"workflow_runs": [workflow(ident=101, workflow_id=101, status="queued")], "total_count": 101},
        ]) as get:
            checks, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
        self.assertEqual(len(checks), 101)
        self.assertIn("head_sha=head&per_page=100&page=2", get.call_args.args[0])
        self.assertEqual(pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[:2], ("in_progress", None))

    def test_malformed_or_truncated_inventory_fails_closed(self):
        for payload in ({}, {"workflow_runs": [], "total_count": 1001},
                        {"workflow_runs": [], "total_count": 1},
                        {"workflow_runs": [workflow(), workflow()], "total_count": 2},
                        {"workflow_runs": [workflow(head_sha=None)], "total_count": 1},
                        {"workflow_runs": [workflow(path=None)], "total_count": 1}):
            with self.subTest(payload=payload), patch.object(pr_gatekeeper, "_get", return_value=payload):
                with self.assertRaises(ValueError):
                    pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")

    def test_missing_second_page_and_count_races_fail_closed(self):
        first = [workflow(ident=n + 1, workflow_id=n + 1) for n in range(100)]
        for second in ({"workflow_runs": [], "total_count": 101},
                       {"workflow_runs": [], "total_count": 100}):
            with self.subTest(second=second), patch.object(pr_gatekeeper, "_get", side_effect=[
                    {"workflow_runs": first, "total_count": 101}, second]):
                with self.assertRaises(ValueError):
                    pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")

    def test_superseded_suites_are_removed_without_hiding_other_workflow_failure(self):
        for other_failure in (False, True):
            with self.subTest(other_failure=other_failure):
                workflows = [workflow(ident=1, check_suite_id=101, conclusion="cancelled"),
                             workflow(ident=2, check_suite_id=102)]
                checks = [{"id": 1, "name": "test", "check_suite": {"id": 101},
                           "status": "completed", "conclusion": "cancelled"},
                          {"id": 2, "name": "test", "check_suite": {"id": 102},
                           "status": "completed", "conclusion": "success"}]
                suites = [{"id": 101, "status": "in_progress", "latest_check_runs_count": 1}]
                if other_failure:
                    workflows.append(workflow(ident=3, workflow_id=20, check_suite_id=103, conclusion="failure"))
                    checks.append({"id": 3, "name": "test", "check_suite": {"id": 103},
                                   "status": "completed", "conclusion": "failure"})
                with patch.object(pr_gatekeeper, "_get", side_effect=[
                        {"check_runs": checks}, EMPTY_STATUSES, {"check_suites": suites},
                        {"workflow_runs": workflows, "total_count": len(workflows)}]):
                    collected = pr_gatekeeper.collect("org/repo", "head", "token")
                self.assertFalse(any(c.get("check_suite", {}).get("id") == 101 for c in collected[0]))
                self.assertEqual(collected[2], [])
                self.assertEqual(pr_gatekeeper.evaluate(*collected)[1], "failure" if other_failure else "success")

    def test_suite_still_selected_by_another_run_is_not_superseded(self):
        runs = [workflow(ident=1, check_suite_id=101), workflow(ident=2, check_suite_id=102),
                workflow(ident=3, workflow_id=20, check_suite_id=101, status="queued")]
        with patch.object(pr_gatekeeper, "_get", return_value={"workflow_runs": runs, "total_count": 3}):
            _, superseded = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
        self.assertEqual(superseded, set())

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
