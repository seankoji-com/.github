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
    def setUp(self):
        sleep = patch.object(pr_gatekeeper.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def verdict(self, runs):
        with patch.object(pr_gatekeeper, "_get", return_value={"workflow_runs": runs, "total_count": len(runs)}):
            checks, _, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
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
            checks, _, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
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

    def test_oversized_destination_metadata_retains_evaluated_failure_marker(self):
        refs = {f"{number:040x}" for number in range(1400)}
        with patch.object(pr_gatekeeper, "previous_publication_refs", return_value=refs), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token", error="fixture failure"), 2)
        body = post.call_args.args[2]
        self.assertEqual(body["conclusion"], "failure")
        self.assertEqual(body["output"]["title"], "Too many gate destinations")
        summary = body["output"]["summary"]
        self.assertLessEqual(len(summary.encode("utf-8")), 60000)
        self.assertIn(pr_gatekeeper.EVALUATED_MARKER, summary)
        self.assertNotIn("gatekeeper-refs-complete", summary)
        check = {**body, "app": {"id": 15368}}
        with patch.object(pr_gatekeeper, "_get", return_value={"check_runs": [check]}):
            self.assertTrue(pr_gatekeeper.has_evaluated_gate("org/repo", "head", "token"))

    def test_pagination_reads_late_pending_workflow(self):
        first = [workflow(ident=n + 1, workflow_id=n + 1) for n in range(100)]
        with patch.object(pr_gatekeeper, "_get", side_effect=[
                {"workflow_runs": first, "total_count": 101},
                {"workflow_runs": [workflow(ident=101, workflow_id=101, status="queued")], "total_count": 101},
        ]) as get:
            checks, _, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
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
                    {"workflow_runs": first, "total_count": 101}, second] * 3):
                with self.assertRaises(ValueError):
                    pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")

    def test_inventory_drift_retries_whole_snapshot_and_retains_new_pending_run(self):
        first = [workflow(ident=n + 1, workflow_id=n + 1) for n in range(100)]
        final = {"workflow_runs": [workflow(ident=101, workflow_id=101, status="queued")], "total_count": 101}
        for drift in ({"workflow_runs": [], "total_count": 100},
                      {"workflow_runs": [first[-1]], "total_count": 101},
                      {"workflow_runs": [], "total_count": 101}):
            with self.subTest(drift=drift), patch.object(pr_gatekeeper, "_get", side_effect=[
                    {"workflow_runs": first, "total_count": 101}, drift,
                    {"workflow_runs": first, "total_count": 101}, final]) as get:
                checks, _, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
            self.assertEqual(get.call_count, 4)
            self.assertIn("page=1", get.call_args_list[2].args[0])
            self.assertEqual(pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[:2], ("in_progress", None))

    def test_persistent_inventory_drift_is_bounded_and_stays_blocking(self):
        with patch.object(pr_gatekeeper, "_get", return_value={"workflow_runs": [], "total_count": 1}) as get:
            with self.assertRaises(pr_gatekeeper.WorkflowInventoryDrift):
                pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_gate_check_sharing_superseded_suite_cannot_create_self_wait(self):
        workflows = [workflow(ident=1, check_suite_id=101, conclusion="cancelled"),
                     workflow(ident=2, check_suite_id=102)]
        gate = {"id": 10, "name": pr_gatekeeper.GATE_CHECK_NAME, "check_suite": {"id": 101},
                "status": "in_progress", "conclusion": None}
        with patch.object(pr_gatekeeper, "_get", side_effect=[
                {"check_runs": [gate]}, EMPTY_STATUSES,
                {"check_suites": [{"id": 101, "status": "in_progress", "latest_check_runs_count": 1}]},
                {"workflow_runs": workflows, "total_count": 2}]):
            collected = pr_gatekeeper.collect("org/repo", "head", "token")
        self.assertEqual(collected[2], [])
        self.assertEqual(pr_gatekeeper.evaluate(*collected)[:2], ("completed", "success"))

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

    def test_persona_recovery_job_never_holds_or_fails_the_gate(self):
        gate_run = workflow(ident=5, workflow_id=30, check_suite_id=105,
                            path=pr_gatekeeper.GATE_WORKFLOW_PATH, event="pull_request_target",
                            status="in_progress", conclusion=None)
        for state, conclusion in (("completed", "failure"), ("in_progress", None)):
            with self.subTest(state=state):
                recovery = {"id": 50, "name": pr_gatekeeper.RECOVERY_CHECK_NAME,
                            "check_suite": {"id": 105}, "status": state, "conclusion": conclusion}
                with patch.object(pr_gatekeeper, "_get", side_effect=[
                        {"check_runs": [recovery]}, EMPTY_STATUSES, {"check_suites": []},
                        {"workflow_runs": [workflow(check_suite_id=101), gate_run], "total_count": 2}]):
                    collected = pr_gatekeeper.collect("org/repo", "head", "token")
                self.assertEqual(pr_gatekeeper.evaluate(*collected)[:2], ("completed", "success"))

    def test_same_named_job_outside_the_gate_workflow_still_blocks(self):
        impostor = {"id": 50, "name": pr_gatekeeper.RECOVERY_CHECK_NAME,
                    "check_suite": {"id": 101}, "status": "completed", "conclusion": "failure"}
        with patch.object(pr_gatekeeper, "_get", side_effect=[
                {"check_runs": [impostor]}, EMPTY_STATUSES, {"check_suites": []},
                {"workflow_runs": [workflow(check_suite_id=101)], "total_count": 1}]):
            collected = pr_gatekeeper.collect("org/repo", "head", "token")
        self.assertEqual(pr_gatekeeper.evaluate(*collected)[:2], ("completed", "failure"))

    def test_suite_still_selected_by_another_run_is_not_superseded(self):
        runs = [workflow(ident=1, check_suite_id=101), workflow(ident=2, check_suite_id=102),
                workflow(ident=3, workflow_id=20, check_suite_id=101, status="queued")]
        with patch.object(pr_gatekeeper, "_get", return_value={"workflow_runs": runs, "total_count": 3}):
            _, superseded, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
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


class DelayedSeedTests(unittest.TestCase):
    def gate(self, title="All checks passed", summary=None, **fields):
        return {"name": pr_gatekeeper.GATE_CHECK_NAME, "app": {"id": 15368},
                "output": {"title": title, "summary": summary if summary is not None else
                           '<!-- gatekeeper-refs:["head"] -->\n<!-- gatekeeper-refs-complete -->'}, **fields}

    def test_only_a_real_evaluated_gate_triggers_refresh(self):
        for check, expected in (
                (self.gate(), True),
                (self.gate(title="Waiting on 1 check"), True),
                (self.gate(title="1 check failing"), True),
                (self.gate(title="Gate seeded, awaiting CI"), False),
                (self.gate(title="", summary=pr_gatekeeper.EVALUATED_MARKER), True),
                (self.gate(output={"title": None, "summary": None}), False),
                (self.gate(app={"id": 999}), False),
                (self.gate(name="test"), False)):
            with self.subTest(check=check), patch.object(pr_gatekeeper, "_get", return_value={"check_runs": [check]}):
                self.assertEqual(pr_gatekeeper.has_evaluated_gate("org/repo", "head", "token"), expected)

    def test_null_wrapper_summary_does_not_prevent_destination_recovery(self):
        sha = "a" * 40
        checks = [self.gate(output={"summary": None}),
                  self.gate(summary=f'<!-- gatekeeper-refs:["{sha}"] -->\n<!-- gatekeeper-refs-complete -->')]
        with patch.object(pr_gatekeeper, "_get", return_value={"check_runs": checks}):
            self.assertEqual(pr_gatekeeper.previous_publication_refs("org/repo", sha, "token"), {sha})

    def test_evaluated_gate_is_found_on_later_page(self):
        with patch.object(pr_gatekeeper, "_get", side_effect=[
                {"check_runs": [self.gate(name="test")] * 100}, {"check_runs": [self.gate()]}]) as get:
            self.assertTrue(pr_gatekeeper.has_evaluated_gate("org/repo", "head", "token"))
        self.assertIn("page=2", get.call_args.args[0])

    def test_seed_lookup_preserves_branch_fallback_without_accepting_moved_heads(self):
        missing = urllib.error.HTTPError("https://api.github.com/test", 422, "missing", {}, None)
        for head, expected in (("head", True), ("moved", False)):
            with self.subTest(head=head), patch.object(pr_gatekeeper, "_get", side_effect=[
                    missing, [{"head": {"sha": "head", "ref": "codex/fix"}}],
                    {"check_runs": [self.gate(head_sha=head)]}]) as get:
                self.assertEqual(pr_gatekeeper.has_evaluated_gate("org/repo", "head", "token"), expected)
            self.assertIn("commits/codex%2Ffix/check-runs", get.call_args.args[0])

    def test_delayed_seed_rechecks_current_ci_instead_of_erasing_prior_verdict(self):
        for status, conclusion in (("completed", "success"), ("queued", None), ("completed", "failure")):
            checks = [{"name": "test", "status": status, "conclusion": conclusion}]
            with self.subTest(status=status, conclusion=conclusion), \
                 patch.dict(pr_gatekeeper.os.environ, {"PERSONA_REVIEW_REQUIRED": "false"}), \
                 patch.object(pr_gatekeeper, "_get", return_value={"check_runs": [self.gate()]}), \
                 patch.object(pr_gatekeeper, "collect", return_value=(checks, EMPTY_STATUSES, [])) as collect, \
                 patch.object(pr_gatekeeper, "_post") as post:
                self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token", seed=True), 0)
            collect.assert_called_once()
            body = post.call_args.args[2]
            self.assertNotEqual(body["output"]["title"], "Gate seeded, awaiting CI")
            self.assertEqual(body.get("conclusion"), conclusion)
            self.assertEqual(body["status"], "in_progress" if status == "queued" else status)

    def test_seed_read_failure_blocks_instead_of_assuming_no_prior_verdict(self):
        with patch.object(pr_gatekeeper, "has_evaluated_gate", side_effect=urllib.error.URLError("unavailable")), \
             patch.object(pr_gatekeeper, "previous_publication_refs", return_value=set()), \
             patch.object(pr_gatekeeper, "_post") as post:
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token", seed=True), 2)
        self.assertEqual(post.call_args.args[2]["conclusion"], "failure")


if __name__ == "__main__":
    unittest.main()
