"""Queued workflows must block before their jobs have check runs."""

import pathlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import yaml

from test_pr_gatekeeper import EMPTY_STATUSES, pr_gatekeeper

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
SCRIPTS = ROOT / "scripts"
REUSABLE_GATEKEEPER = WORKFLOWS / "reusable-pr-gatekeeper.yml"
GATE_CALLER = WORKFLOWS / "call-reusable-pr-gatekeeper.yml"
# Any 40-hex value is a valid identity; the fixture asserts the *exact same*
# literal is reused for every fetch and that no branch name or caller ref
# reaches the URL.
STAGING_SHA = "a" * 40
# The only keys GitHub accepts on a job that calls a reusable workflow.
REUSABLE_CALLER_KEYS = {"name", "uses", "with", "secrets", "needs", "if",
                        "permissions", "strategy", "concurrency"}
# actionlint predates `concurrency.queue`, which GitHub accepts.
ACTIONLINT_IGNORES = [r'unexpected key "queue" for "concurrency" section']


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
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "token", open_prs=[{"head": {"sha": "head"}}]), 0)
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
                checks, _ = pr_gatekeeper.collect_workflow_checks("org/repo", "head", "token")
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


class RequiredWorkflowTests(unittest.TestCase):
    """Real report/collector seams; all reads and publications are synthetic.

    REST run/job head_sha is the Actions identity, not necessarily checkout's
    GITHUB_SHA. The manifest selects that binding explicitly. See GitHub's
    REST workflow-runs/workflow-jobs and events-that-trigger-workflows docs.
    """

    def setUp(self):
        for context in (patch.dict(os.environ, {"PERSONA_REVIEW_REQUIRED": "false", "GITHUB_RUN_ID": "999",
                                                "GATEKEEPER_REQUIRED_WORKFLOWS": ""}),
                        patch.object(pr_gatekeeper.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")),
                        patch.object(pr_gatekeeper.time, "sleep")):
            context.start()
            self.addCleanup(context.stop)
        self.prs = [{"number": 7, "head": {"sha": "head", "ref": "feature", "repo": {"id": 42}},
                     "merge_commit_sha": "merge"}]
        self.reads, self.bodies = [], []
        self.inventories, self.details, self.jobs, self.after = {}, {}, {}, {}
        self.metadata = {"id": 10, "path": ".github/workflows/test.yml", "state": "active"}
        self.statuses = EMPTY_STATUSES
        self.post_error = None

    def manifest(self, ref="head", event="pull_request", path=".github/workflows/test.yml"):
        return json.dumps([{"path": path, "event": event, "ref": ref}])

    def required_run(self, ref="head", **changes):
        fields = {"head_sha": ref, "head_repository": {"id": 42},
                  "pull_requests": [{"number": 7, "head": {"sha": "head"}}], **changes}
        return workflow(**fields)

    def successful_job(self, run, **changes):
        return {"id": 301, "run_id": run["id"], "run_attempt": run["run_attempt"],
                "head_sha": run["head_sha"], "status": "completed", "conclusion": "success", **changes}

    def install(self, run):
        self.inventories.setdefault(run["head_sha"], []).append(run)
        self.details[run["id"]] = run
        self.jobs[run["id"]] = [self.successful_job(run)]

    def get(self, path, token):
        self.reads.append(path)
        if "/pulls?state=open" in path:
            return self.prs
        if "/actions/workflows/" in path:
            return self.metadata
        if "/commits/" in path:
            if "/check-runs?" in path:
                # Arbitrary success (even named like the mandatory workflow)
                # cannot establish workflow completeness.
                return {"check_runs": [{"name": "CodeQL / test.yml", "id": 777,
                                        "status": "completed", "conclusion": "success"}]}
            if "/status?" in path:
                return self.statuses
            if "/check-suites?" in path:
                return {"check_suites": []}
        if "/actions/runs?head_sha=" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            runs = self.inventories.get(sha, [])
            return {"workflow_runs": runs, "total_count": len(runs)}
        if "/actions/runs/" in path:
            ident = int(path.split("/actions/runs/")[1].split("/")[0])
            if "/jobs?" in path:
                jobs = self.jobs[ident]
                if isinstance(jobs, dict):
                    return jobs
                page = int(path.split("page=")[-1])
                return {"jobs": jobs[(page - 1) * 100:page * 100], "total_count": len(jobs)}
            # Allow a detail change only after the attempt jobs were read.
            if ident in self.after and any(f"/runs/{ident}/attempts/" in p for p in self.reads):
                return self.after[ident]
            return self.details[ident]
        self.fail(f"unexpected read: {path}")

    def post(self, path, token, body):
        self.assertEqual(path, "/repos/org/repo/check-runs")
        self.bodies.append(body)
        if body["head_sha"] == self.post_error:
            raise urllib.error.URLError("fixture publication failure")

    def invoke(self, manifest=None, *, extra=(), trigger="head", reconcile=False, seed=False):
        with patch.object(pr_gatekeeper, "_get", side_effect=self.get), \
             patch.object(pr_gatekeeper, "_post", side_effect=self.post), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "fixture-only"}):
            args = ["--repo", "org/repo", "--sha", trigger, "--require-ci", *extra]
            if manifest is not None:
                args += ["--required-workflows", manifest]
            if reconcile:
                args += ["--reconcile-open"]
            if seed:
                args += ["--seed"]
            return pr_gatekeeper.main(args)

    def assert_gate(self, state):
        self.assertEqual({body["head_sha"] for body in self.bodies}, {"head", "merge"})
        self.assertEqual(len(self.bodies), 2)
        self.assertTrue(all((body["status"], body.get("conclusion")) == state for body in self.bodies), self.bodies)

    def test_latest_failed_cancelled_stale_and_pending_block_both_destinations_without_manifest(self):
        for ref in ("head", "merge"):
            for conclusion in ("failure", "cancelled", "stale", None):
                with self.subTest(ref=ref, conclusion=conclusion):
                    self.inventories = {ref: [self.required_run(ref, ident=1),
                        self.required_run(ref, ident=2, conclusion=conclusion,
                                          status="queued" if conclusion is None else "completed")]}
                    self.bodies = []
                    self.assertEqual(self.invoke(), 0)
                    self.assert_gate(("in_progress", None) if conclusion is None else ("completed", "failure"))
                    if conclusion == "stale":
                        self.assertTrue(all("stale" in b["output"]["summary"] for b in self.bodies))

    def test_missing_declared_workflow_blocks_despite_other_ci_on_either_destination(self):
        for ref in ("head", "merge"):
            with self.subTest(ref=ref):
                self.bodies = []
                self.assertEqual(self.invoke(self.manifest(ref)), 0)
                self.assert_gate(("in_progress", None))
                self.assertIn("missing current-ref run", self.bodies[0]["output"]["summary"])

    def test_head_and_merge_binding_succeed_and_share_registration_reads(self):
        for ref in ("head", "merge"):
            with self.subTest(ref=ref):
                self.inventories, self.reads, self.bodies = {}, [], []
                self.install(self.required_run(ref))
                self.assertEqual(self.invoke(self.manifest(ref), reconcile=True), 0)
                self.assert_gate(("completed", "success"))
                for sha in ("head", "merge"):
                    self.assertEqual(sum(f"/actions/runs?head_sha={sha}&" in p for p in self.reads), 1)
                    self.assertEqual(sum(f"/commits/{sha}/check-runs?filter=latest" in p for p in self.reads), 1)
                self.assertEqual(sum("/actions/workflows/" in p for p in self.reads), 1)
                self.assertEqual(sum("/attempts/1/jobs?" in p for p in self.reads), 1)

    def test_manifest_can_require_both_ref_bindings(self):
        for ident, ref in enumerate(("head", "merge"), 1):
            self.install(self.required_run(ref, ident=ident))
        manifest = json.dumps([json.loads(self.manifest(ref))[0] for ref in ("head", "merge")])
        self.assertEqual(self.invoke(manifest), 0)
        self.assert_gate(("completed", "success"))

    def test_other_path_identity_event_sha_branch_repository_and_pr_cannot_satisfy_manifest(self):
        for changes in ({"path": ".github/workflows/other.yml", "name": "test.yml"},
                        {"workflow_id": 11}, {"event": "push"}, {"event": "schedule"},
                        {"event": "pull_request_target"}, {"head_sha": "old-head"},
                        {"head_sha": "merge"}, {"head_branch": "another"},
                        {"head_repository": {"id": 99}}, {"pull_requests": [{"number": 8, "head": {"sha": "head"}}]},
                        {"pull_requests": [{"number": 7, "head": {"sha": "old-head"}}]},
                        {"pull_requests": None}):
            with self.subTest(changes=changes):
                self.inventories, self.bodies = {}, []
                candidate = self.required_run()
                candidate.update(changes)
                self.install(candidate)
                self.assertEqual(self.invoke(self.manifest()), 0)
                self.assert_gate(("in_progress", None))

    def test_fork_empty_association_and_path_revision_suffix_use_exact_identity(self):
        self.install(self.required_run(pull_requests=[], path=".github/workflows/test.yml@feature"))
        self.assertEqual(self.invoke(self.manifest()), 0)
        self.assert_gate(("completed", "success"))

    def test_caller_declared_push_and_dispatch_events_are_supported(self):
        for event in ("push", "workflow_dispatch"):
            with self.subTest(event=event):
                self.inventories, self.bodies = {}, []
                self.install(self.required_run(event=event, pull_requests=[]))
                self.assertEqual(self.invoke(self.manifest(event=event)), 0)
                self.assert_gate(("completed", "success"))

    def test_latest_current_ref_run_and_attempt_override_older_success(self):
        for ref in ("head", "merge"):
            for status, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"),
                                       ("completed", "cancelled"), ("completed", "stale"),
                                       ("completed", "neutral"), ("completed", "skipped")):
                with self.subTest(ref=ref, status=status, conclusion=conclusion):
                    self.inventories, self.bodies = {}, []
                    self.install(self.required_run(ref, ident=1))
                    newer = self.required_run(ref, ident=2)
                    self.install(newer)
                    self.details[2] = dict(newer, run_attempt=2, status=status, conclusion=conclusion)
                    self.assertEqual(self.invoke(self.manifest(ref)), 0)
                    self.assert_gate(("completed", "failure") if status == "completed" else ("in_progress", None))

    def test_latest_successful_attempt_reads_only_current_jobs(self):
        run = self.required_run()
        self.install(run)
        self.details[1] = dict(run, run_attempt=2)
        self.jobs[1] = [self.successful_job(self.details[1])]
        self.assertEqual(self.invoke(self.manifest()), 0)
        self.assert_gate(("completed", "success"))
        self.assertTrue(any("/attempts/2/jobs?" in p for p in self.reads))
        self.assertFalse(any("/attempts/1/jobs?" in p for p in self.reads))

    def test_empty_all_skipped_pending_and_unsuccessful_jobs_never_green(self):
        self.install(self.required_run())
        good = self.successful_job(self.details[1])
        cases = [([], ("in_progress", None)), ([dict(good, conclusion="skipped")], ("in_progress", None)),
                 ([good, dict(good, id=302, status="queued", conclusion=None)], ("in_progress", None))]
        cases += [([good, dict(good, id=302, conclusion=bad)], ("completed", "failure"))
                  for bad in ("stale", "failure", "cancelled", "neutral", None)]
        for jobs, expected in cases:
            with self.subTest(jobs=jobs):
                self.bodies, self.jobs[1] = [], jobs
                self.assertEqual(self.invoke(self.manifest()), 0)
                self.assert_gate(expected)

    def test_conditional_skipped_jobs_do_not_replace_required_success(self):
        self.install(self.required_run())
        self.jobs[1].append(self.successful_job(self.details[1], id=302, conclusion="skipped"))
        self.assertEqual(self.invoke(self.manifest()), 0)
        self.assert_gate(("completed", "success"))

    def test_job_pagination_checks_late_failure_and_exact_attempt(self):
        self.install(self.required_run())
        self.jobs[1] = [self.successful_job(self.details[1], id=i) for i in range(1, 102)]
        self.jobs[1][-1]["conclusion"] = "failure"
        self.assertEqual(self.invoke(self.manifest()), 0)
        self.assert_gate(("completed", "failure"))
        self.assertTrue(any("/attempts/1/jobs?per_page=100&page=2" in p for p in self.reads))

    def test_mismatched_job_identity_attempt_sha_and_incomplete_inventory_fail_closed(self):
        self.install(self.required_run())
        for changes in ({"run_id": 9}, {"run_attempt": 0}, {"run_attempt": 2}, {"head_sha": "merge"},
                        {"head_sha": "old"}, {"id": None}):
            with self.subTest(changes=changes):
                self.bodies = []
                self.jobs[1] = [self.successful_job(self.details[1], **changes)]
                self.assertEqual(self.invoke(self.manifest()), 2)
                self.assert_gate(("completed", "failure"))
        for payload in ({"jobs": [], "total_count": 1}, {"jobs": [], "total_count": True},
                        {"jobs": [], "total_count": -1}, {},
                        {"jobs": [self.successful_job(self.details[1])] * 2, "total_count": 2}):
            with self.subTest(payload=payload):
                self.bodies, self.jobs[1] = [], payload
                self.assertEqual(self.invoke(self.manifest()), 2)
                self.assert_gate(("completed", "failure"))

    def test_detail_or_attempt_changes_during_reads_fail_closed(self):
        self.install(self.required_run())
        original = self.details[1]
        for changes in ({"head_sha": "old"}, {"workflow_id": 11}, {"path": ".github/workflows/other.yml"},
                        {"event": "push"}, {"head_branch": "other"}, {"run_attempt": None},
                        {"run_attempt": 0}, {"pull_requests": [] , "head_repository": {"id": 99}}):
            with self.subTest(changes=changes):
                self.bodies = []
                self.details[1] = dict(original, **changes)
                self.assertEqual(self.invoke(self.manifest()), 2)
                self.assert_gate(("completed", "failure"))
        self.details[1] = original
        self.bodies, self.reads = [], []
        self.after[1] = dict(original, run_attempt=2, status="queued", conclusion=None)
        self.assertEqual(self.invoke(self.manifest()), 2)
        self.assert_gate(("completed", "failure"))

    def test_invalid_manifest_and_inactive_or_wrong_metadata_block_both_destinations(self):
        invalid = ["not JSON", "null", "{}", '["test.yml"]', '[{"path":".github/workflows/test.yml"}]',
                   self.manifest(event="schedule"), self.manifest(ref="base"),
                   self.manifest(path=".github/workflows/../test.yml"), self.manifest(path=pr_gatekeeper.GATE_WORKFLOW_PATH),
                   self.manifest()[:-1] + "," + self.manifest()[1:],
                   json.dumps([{**json.loads(self.manifest())[0], "extra": True}]), " " * 16385]
        for value in invalid:
            with self.subTest(value=value[:100]):
                self.bodies = []
                self.assertEqual(self.invoke(value), 2)
                self.assert_gate(("completed", "failure"))
        for changes in ({"id": 0}, {"id": "10"}, {"path": ".github/workflows/other.yml"}, {"state": "disabled_manually"}):
            with self.subTest(metadata=changes):
                self.bodies = []
                self.metadata = {"id": 10, "path": ".github/workflows/test.yml", "state": "active", **changes}
                self.assertEqual(self.invoke(self.manifest()), 2)
                self.assert_gate(("completed", "failure"))

    def test_empty_manifest_has_identical_legacy_reads_publications_and_api_behavior(self):
        self.assertEqual(self.invoke(), 0)
        reads, bodies = self.reads[:], self.bodies[:]
        self.reads, self.bodies = [], []
        self.assertEqual(self.invoke("[]"), 0)
        self.assertEqual(self.reads, reads)
        self.assertEqual(self.bodies, bodies)
        # Plain API calls retain empty-inventory success and no PR-list read.
        self.reads, self.bodies = [], []
        with patch.object(pr_gatekeeper, "_get", side_effect=self.get), patch.object(pr_gatekeeper, "_post", side_effect=self.post):
            self.assertEqual(pr_gatekeeper.report("org/repo", "head", "fixture", required_workflows="[]"), 0)
        self.assertFalse(any("/pulls?" in p for p in self.reads))

    def test_unrelated_prs_have_independent_evidence_and_cached_workflow_metadata(self):
        self.prs.append({"number": 8, "head": {"sha": "head-b", "ref": "other", "repo": {"id": 43}},
                        "merge_commit_sha": "merge-b"})
        self.install(self.required_run())
        self.assertEqual(self.invoke(self.manifest(), reconcile=True), 0)
        self.assertEqual([b["head_sha"] for b in self.bodies], ["head", "merge", "head-b", "merge-b"])
        self.assertTrue(all(b.get("conclusion") == "success" for b in self.bodies[:2]))
        self.assertTrue(all(b["status"] == "in_progress" for b in self.bodies[2:]))
        self.assertEqual(sum("/actions/workflows/" in p for p in self.reads), 1)

    def test_shared_head_must_meet_each_pr_and_merge_manifest(self):
        self.prs.append({"number": 8, "head": self.prs[0]["head"], "merge_commit_sha": "merge-b"})
        self.install(self.required_run("merge"))
        self.assertEqual(self.invoke(self.manifest("merge"), reconcile=True), 0)
        self.assertEqual({b["head_sha"] for b in self.bodies}, {"head", "merge", "merge-b"})
        self.assertTrue(all(b["status"] == "in_progress" for b in self.bodies))

    def test_empty_pr_association_cannot_satisfy_ambiguous_shared_head(self):
        self.prs.append({"number": 8, "head": self.prs[0]["head"], "merge_commit_sha": "merge-b"})
        self.install(self.required_run(pull_requests=[]))
        self.assertEqual(self.invoke(self.manifest(), reconcile=True), 0)
        self.assertEqual({b["head_sha"] for b in self.bodies}, {"head", "merge", "merge-b"})
        self.assertTrue(all(b["status"] == "in_progress" for b in self.bodies))

    def test_partial_publication_retains_failure_without_duplicate_writes(self):
        self.install(self.required_run())
        self.post_error = "head"
        self.assertEqual(self.invoke(self.manifest(), reconcile=True), 2)
        self.assert_gate(("completed", "success"))

    def test_workflow_or_attempt_api_errors_replace_all_destinations_with_failure(self):
        self.install(self.required_run())
        get = self.get
        for endpoint in ("/actions/workflows/", "/actions/runs/1", "/attempts/1/jobs?"):
            with self.subTest(endpoint=endpoint):
                self.bodies = []
                def failed(path, token):
                    if endpoint in path:
                        raise urllib.error.URLError("fixture unavailable")
                    return get(path, token)
                with patch.object(self, "get", side_effect=failed):
                    self.assertEqual(self.invoke(self.manifest()), 2)
                self.assert_gate(("completed", "failure"))

    def test_manifest_environment_and_literal_argv_are_equivalent(self):
        self.install(self.required_run())
        self.assertEqual(self.invoke(self.manifest()), 0)
        reads, bodies = self.reads[:], self.bodies[:]
        self.reads, self.bodies = [], []
        with patch.dict(os.environ, {"GATEKEEPER_REQUIRED_WORKFLOWS": self.manifest()}):
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.reads, reads)
        self.assertEqual(self.bodies, bodies)

    def test_seed_then_evaluation_and_review_resolution_keep_manifest(self):
        self.assertEqual(self.invoke(self.manifest(), seed=True), 0)
        self.assertEqual(len(self.bodies), 1)
        self.assertEqual(self.bodies[0]["status"], "in_progress")
        self.assertFalse(any("/actions/workflows/" in p for p in self.reads))
        self.bodies, self.reads = [], []
        with patch.object(pr_gatekeeper, "has_evaluated_gate", return_value=True):
            self.assertEqual(self.invoke(self.manifest(), seed=True), 0)
        self.assert_gate(("in_progress", None))
        self.bodies = []
        with patch.object(pr_gatekeeper, "resolve_review_heads", return_value=["head"]):
            self.assertEqual(self.invoke(self.manifest(), extra=["--review-run", "88"]), 0)
        self.assert_gate(("in_progress", None))

    def test_changed_or_closed_pr_snapshot_cannot_use_previous_manifest_evidence(self):
        self.install(self.required_run())
        original = self.prs[:]
        current = [{"number": 7, "head": {"sha": "new-head", "ref": "feature", "repo": {"id": 42}},
                    "merge_commit_sha": "new-merge"}]
        get = self.get
        for snapshot in (current, []):
            with self.subTest(snapshot=snapshot):
                self.prs, self.bodies, self.reads = original, [], []
                def changed(path, token):
                    if "/pulls?state=open" in path and any("/actions/workflows/" in p for p in self.reads):
                        self.prs = snapshot
                    return get(path, token)
                with patch.object(self, "get", side_effect=changed):
                    self.assertEqual(self.invoke(self.manifest(), reconcile=True), 0)
                self.assertTrue(all(b.get("conclusion") == "success" for b in self.bodies[:2]))
                if snapshot:
                    self.assertEqual({b["head_sha"] for b in self.bodies[2:]}, {"new-head", "new-merge"})
                    self.assertTrue(all(b["status"] == "in_progress" for b in self.bodies[2:]))
                else:
                    self.assertEqual(len(self.bodies), 2)


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



class WorkflowSyntaxTests(unittest.TestCase):
    """Errors GitHub rejects at parse time.

    A parse failure shows the workflow by path, runs zero jobs, and never
    posts `gatekeeper / all-checks-passed`, so every PR in the org blocks.
    """

    def workflows(self):
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            yield path, yaml.safe_load(path.read_text())

    def test_reusable_caller_jobs_use_only_allowed_keys(self):
        for path, workflow in self.workflows():
            for name, job in (workflow.get("jobs") or {}).items():
                if "uses" not in job:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual(set(job) - REUSABLE_CALLER_KEYS, set())

    def test_job_level_if_never_reads_secrets(self):
        for path, workflow in self.workflows():
            for name, job in (workflow.get("jobs") or {}).items():
                with self.subTest(workflow=path.name, job=name):
                    self.assertNotRegex(str(job.get("if", "")), r"\bsecrets\.")

    def test_gatekeeper_caller_passes_actionlint(self):
        # CI installs a pinned actionlint; locally it is optional.
        actionlint = shutil.which("actionlint")
        if not actionlint:
            self.skipTest("actionlint not installed")
        args = [actionlint, "-oneline", "-shellcheck=", "-pyflakes="]
        for pattern in ACTIONLINT_IGNORES:
            args += ["-ignore", pattern]
        result = subprocess.run(args + [str(GATE_CALLER)], cwd=ROOT,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CoalescedRefreshTests(unittest.TestCase):
    def test_required_workflow_input_is_optional_and_passed_only_as_environment_data(self):
        config = yaml.safe_load((WORKFLOWS / "reusable-pr-gatekeeper.yml").read_text())
        contract = config[True]["workflow_call"]["inputs"]["required_workflows"]
        self.assertEqual(contract["type"], "string")
        self.assertEqual(contract["default"], "[]")
        self.assertFalse(contract.get("required", False))
        step = config["jobs"]["all-checks-passed"]["steps"][1]
        self.assertEqual(step["env"]["GATEKEEPER_REQUIRED_WORKFLOWS"], "${{ inputs.required_workflows }}")
        self.assertNotIn("${{", step["run"])
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "should-not-exist"
            value = json.dumps([{"path": f"$(touch {marker}) `touch {marker}` ${{{{ inputs.head_sha }}}}",
                                 "event": "pull_request", "ref": "head"}])
            (root / "pr-gatekeeper.py").write_text(
                "import json, os, sys\nprint(json.dumps([os.environ['GATEKEEPER_REQUIRED_WORKFLOWS'], sys.argv[1:]]))\n")
            result = subprocess.run(["bash", "-eu", "-c", step["run"]],
                                    env={**os.environ, "RUNNER_TEMP": directory, "GITHUB_REPOSITORY": "org/repo",
                                         "HEAD_SHA": "head", "REQUIRE_CI": "true", "SEED": "false",
                                         "REVIEW_RUN_ID": "", "PERSONA_STATE": "", "PERSONA_PR": "",
                                         "GATEKEEPER_REQUIRED_WORKFLOWS": value},
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), [value, ["--repo", "org/repo", "--sha", "head", "--require-ci", "--reconcile-open"]])
            self.assertFalse(marker.exists())

    def test_refresh_shares_one_invocation_and_preserves_seed_and_signal_arguments(self):
        config = yaml.safe_load((WORKFLOWS / "reusable-pr-gatekeeper.yml").read_text())
        self.assertEqual(config[True]["workflow_call"]["inputs"]["reconcile_open"], {
            "description": "Refresh every open PR after handling the triggering event. Required for a bounded caller queue.",
            "type": "boolean", "default": False,
        })
        steps = config["jobs"]["all-checks-passed"]["steps"]
        self.assertEqual(steps[0]["id"], "evaluator")
        self.assertEqual(len(steps), 2)
        refresh = steps[1]
        self.assertEqual(refresh["env"]["REQUIRE_CI"], "${{ inputs.reconcile_open }}")
        self.assertEqual(config["jobs"]["all-checks-passed"]["runs-on"], "${{ fromJSON(inputs.runner-json) }}")
        self.assertEqual(config[True]["workflow_call"]["inputs"]["runner-json"]["default"], '["ubuntu-latest"]')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "pr-gatekeeper.py").write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            for enabled, seed, review, state, expected in (
                ("true", "false", "", "", ["--require-ci", "--reconcile-open"]),
                ("false", "false", "", "", []),
                ("true", "true", "", "", ["--require-ci", "--seed"]),
                ("true", "false", "123", "", ["--require-ci", "--reconcile-open", "--review-run", "123"]),
                ("true", "false", "", "failed", ["--require-ci", "--reconcile-open", "--persona-state", "failed", "--persona-pr", "42"]),
            ):
                with self.subTest(enabled=enabled, seed=seed, review=review, state=state):
                    result = subprocess.run(["bash", "-eu", "-c", refresh["run"]],
                                            env={**os.environ, "RUNNER_TEMP": directory,
                                                 "GITHUB_REPOSITORY": "org/repo", "HEAD_SHA": "head",
                                                 "REQUIRE_CI": enabled, "SEED": seed, "REVIEW_RUN_ID": review,
                                                 "PERSONA_STATE": state, "PERSONA_PR": "42" if state else ""},
                                            capture_output=True, text=True, check=True)
                    import json
                    self.assertEqual(json.loads(result.stdout), ["--repo", "org/repo", "--sha", "head"] + expected)


    def test_unrelated_completions_cannot_replace_pr_refreshes(self):
        import json
        from types import SimpleNamespace

        # Evaluate the rendered admission expression against concrete events,
        # including runs that the downstream gatekeeper job will skip.
        cases = [
            ("workflow_run", "pull_request", "CI", False, True),
            ("workflow_run", "dynamic", "CI", False, True),
            ("workflow_run", "push", "CI", True, True),
            ("workflow_run", "push", "CI", False, False),
            ("workflow_run", "schedule", "Maintenance", False, False),
            ("workflow_run", "pull_request", "PR Gatekeeper", True, False),
            ("workflow_run", "pull_request_review", "PR Gatekeeper", True, False),
            ("pull_request_target", "", "", False, False),
            ("workflow_dispatch", "", "", False, False),
        ]
        for visibility in ("public",):
            caller = yaml.safe_load(GATE_CALLER.read_text())
            expression = caller["concurrency"]["group"][3:-2].strip()
            expression = expression.replace("&&", " and ").replace("||", " or ").replace("null", "None")
            for event, upstream, name, attached, coalesces in cases:
                with self.subTest(visibility=visibility, event=event, upstream=upstream, name=name, attached=attached):
                    github = SimpleNamespace(event_name=event, repository="org/repo", run_id="42",
                        event=SimpleNamespace(workflow_run=SimpleNamespace(event=upstream, name=name,
                            pull_requests=[{}] if attached else [None])))
                    actual = eval(expression, {"__builtins__": {}}, {
                        "github": github, "fromJSON": json.loads,
                        "contains": lambda values, value: value in values,
                        "format": lambda pattern, value: pattern.format(value),
                    })
                    expected = "pr-gatekeeper-refresh-org/repo" if coalesces else "pr-gatekeeper-event-42"
                    self.assertEqual(actual, expected)


FIXTURE_CURL = r'''#!/bin/sh
# Inert stand-in for curl. It never opens a socket: it validates the URL it was
# handed against the exact trusted boundary (seankoji-com/.github at one 40-hex
# revision, scripts/ only), records it, then serves the committed file from
# FIXTURE_SOURCE_DIR. Any other repository, ref or path is a violation.
url=""
dest=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift; dest="$1" ;;
    -*) ;;
    *) url="$1" ;;
  esac
  shift
done
printf '%s\n' "$url" >> "$URL_LOG"
file=${url##*/}
case "$url" in
  "https://raw.githubusercontent.com/seankoji-com/.github/${FIXTURE_SHA}/scripts/${file}") ;;
  *) printf '%s\n' "$url" >> "$VIOLATION_LOG"; exit 3 ;;
esac
[ "$FIXTURE_FAIL" = "$file" ] && exit 7
if [ "$FIXTURE_EMPTY" = "$file" ]; then : > "$dest"; exit 0; fi
cp "${FIXTURE_SOURCE_DIR}/${file}" "$dest"
'''

EVALUATOR_TRIPWIRE = r'''#!/bin/sh
# Records any attempt to execute the evaluator while it is being staged.
# Staging must never run it: a non-empty log is a failure.
printf '%s\n' "$@" >> "$TRIPWIRE"
exit 99
'''


def gatekeeper_interpreters():
    """Interpreters the staged evaluator must load under.

    The running interpreter covers whichever of 3.10/3.12 executes this suite;
    an explicit python3.10/python3.12 on PATH is exercised too, so a single run
    (for example the 3.10 gate) loads the staged bundle under both.
    """
    found = []
    for candidate in (sys.executable, shutil.which("python3.10"), shutil.which("python3.12")):
        if not candidate:
            continue
        resolved = str(pathlib.Path(candidate).resolve())
        if resolved not in found:
            found.append(resolved)
    return found


class EvaluatorStagingTests(unittest.TestCase):
    """The literal `Stage the evaluator` shell, executed offline.

    scripts/pr-gatekeeper.py imports the sibling module gatekeeper_workflows at
    module load. A stage step that fetches only the entrypoint therefore leaves
    the evaluator unable to start on every runner: `python3 -I pr-gatekeeper.py`
    exits 1 with ModuleNotFoundError before main() runs, and no
    `gatekeeper / all-checks-passed` check run is ever posted. These tests run
    the real `run` block from reusable-pr-gatekeeper.yml against a fixture curl
    that serves the committed files by URL and asserts the URL it was handed,
    then load the staged bundle under real interpreters.
    """

    def setUp(self):
        self.job = yaml.safe_load(REUSABLE_GATEKEEPER.read_text())["jobs"]["all-checks-passed"]
        self.stage_step = self.job["steps"][0]

    def environment(self, root, *, sha=STAGING_SHA, fail="", empty="", stale_helper=""):
        binary = root / "bin"
        binary.mkdir(exist_ok=True)
        for name, body in (("curl", FIXTURE_CURL), ("python3", EVALUATOR_TRIPWIRE)):
            program = binary / name
            program.write_text(body)
            program.chmod(0o755)
        runner_temp = root / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        if stale_helper:
            (runner_temp / stale_helper).write_text('raise SystemExit("stale leftover")\n')
        for name in ("urls", "violations"):
            (root / name).write_text("")
        return {
            # Fixture-only PATH: the dummy curl replaces the real one, and the
            # tripwire python3 would record any evaluator execution.
            "PATH": f"{binary}:/usr/bin:/bin",
            "JOB_WORKFLOW_SHA": sha,
            "RUNNER_TEMP": str(runner_temp),
            "FIXTURE_SHA": STAGING_SHA,
            "FIXTURE_SOURCE_DIR": str(SCRIPTS),
            "FIXTURE_FAIL": fail,
            "FIXTURE_EMPTY": empty,
            "URL_LOG": str(root / "urls"),
            "VIOLATION_LOG": str(root / "violations"),
            "TRIPWIRE": str(root / "tripwire"),
        }

    def run_stage(self, root, environment):
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-euo", "pipefail", "-c", self.stage_step["run"]],
            cwd=root, env=environment, capture_output=True, text=True, timeout=30,
        )

    def urls(self, root):
        return (root / "urls").read_text().splitlines()

    def assert_rejected(self, result, root):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / "tripwire").exists(), "staging must not execute the evaluator")
        self.assertEqual((root / "violations").read_text(), "", "staging fetched an untrusted URL")
        self.assertTrue(all("api.github.com" not in url for url in self.urls(root)))

    def assert_loads(self, bundle, cwd):
        """Load the staged entrypoint with `-I --help`: no token, no network."""
        interpreters = gatekeeper_interpreters()
        self.assertTrue(interpreters, "no interpreter available to load the staged bundle")
        for interpreter in interpreters:
            with self.subTest(interpreter=interpreter):
                result = subprocess.run(
                    [interpreter, "-I", str(bundle), "--help"], cwd=cwd,
                    env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: ", result.stdout)
                self.assertIn("--reconcile-open", result.stdout)
        return interpreters

    def test_stage_fetches_both_modules_at_one_sha_into_a_loadable_bundle(self):
        with tempfile.TemporaryDirectory(prefix="gatekeeper-stage-") as directory:
            root = pathlib.Path(directory)
            environment = self.environment(root)
            # A decoy in the working directory: the sibling import must come
            # from the staged directory, never from the caller's checkout.
            (root / "gatekeeper_workflows.py").write_text('raise SystemExit("decoy helper loaded")\n')
            result = self.run_stage(root, environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = [
                f"https://raw.githubusercontent.com/seankoji-com/.github/{STAGING_SHA}/scripts/pr-gatekeeper.py",
                f"https://raw.githubusercontent.com/seankoji-com/.github/{STAGING_SHA}/scripts/gatekeeper_workflows.py",
            ]
            self.assertEqual(self.urls(root), expected)
            self.assertEqual((root / "violations").read_text(), "")
            self.assertFalse((root / "tripwire").exists(), "staging must not execute the evaluator")
            runner_temp = pathlib.Path(environment["RUNNER_TEMP"])
            entrypoint = runner_temp / "pr-gatekeeper.py"
            for staged in (entrypoint, runner_temp / "gatekeeper_workflows.py"):
                self.assertTrue(staged.is_file(), staged)
                self.assertEqual(
                    hashlib.sha256(staged.read_bytes()).hexdigest(),
                    hashlib.sha256((SCRIPTS / staged.name).read_bytes()).hexdigest(),
                )
            self.assertTrue(self.assert_loads(entrypoint, root))

    def test_malformed_identity_never_fetches(self):
        for sha in ("", "main", "a" * 39, "a" * 41, "A" * 40, "a" * 40 + "\nstaged=true", "\n" + "a" * 40):
            with self.subTest(sha=sha), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                self.assert_rejected(self.run_stage(root, self.environment(root, sha=sha)), root)
                self.assertEqual(self.urls(root), [])

    def test_fetch_failure_or_empty_file_rejects_before_evaluation(self):
        for name in ("pr-gatekeeper.py", "gatekeeper_workflows.py"):
            for kind in ("fail", "empty"):
                with self.subTest(file=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    result = self.run_stage(root, self.environment(root, **{kind: name}))
                    self.assert_rejected(result, root)
                    urls = self.urls(root)
                    self.assertTrue(urls)
                    self.assertTrue(all(f"/{STAGING_SHA}/scripts/" in url for url in urls))

    def test_stale_helper_cannot_satisfy_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            environment = self.environment(
                root, fail="gatekeeper_workflows.py", stale_helper="gatekeeper_workflows.py"
            )
            self.assert_rejected(self.run_stage(root, environment), root)
            runner_temp = pathlib.Path(environment["RUNNER_TEMP"])
            self.assertFalse(
                (runner_temp / "gatekeeper_workflows.py").exists(),
                "a leftover helper must be removed, never accepted",
            )

    def test_entrypoint_only_bundle_cannot_start_the_evaluator(self):
        """The defect this change repairs: the evaluator's own import.

        The old staging fetched only pr-gatekeeper.py. An entrypoint-only
        directory cannot start it, so the literal stage step above is what makes
        the bundle usable.
        """
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "bundle"
            bundle.mkdir()
            shutil.copy(SCRIPTS / "pr-gatekeeper.py", bundle / "pr-gatekeeper.py")
            result = subprocess.run(
                [gatekeeper_interpreters()[0], "-I", str(bundle / "pr-gatekeeper.py"), "--help"],
                cwd=directory, env={"PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ModuleNotFoundError", result.stderr)
            self.assertIn("gatekeeper_workflows", result.stderr)

    def test_stage_step_pins_one_revision_for_both_modules_without_checkout(self):
        self.assertFalse(
            any("actions/checkout" in step.get("uses", "") for step in self.job["steps"]),
            "the gate must never check out caller or PR code",
        )
        self.assertEqual(self.stage_step["id"], "evaluator")
        self.assertEqual(self.stage_step["env"]["JOB_WORKFLOW_SHA"], "${{ job.workflow_sha }}")
        self.assertIn('=~ ^[0-9a-f]{40}$', self.stage_step["run"])
        self.assertNotIn("${{", self.stage_step["run"])
        self.assertIn("for file in pr-gatekeeper.py gatekeeper_workflows.py; do", self.stage_step["run"])
        self.assertIn(
            '"https://raw.githubusercontent.com/seankoji-com/.github/${REF}/scripts/${file}"',
            self.stage_step["run"],
        )
        self.assertIn('python3 "${RUNNER_TEMP}/pr-gatekeeper.py"', self.job["steps"][1]["run"])


if __name__ == "__main__":
    unittest.main()
