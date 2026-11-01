#!/usr/bin/env python3
"""Single-head gate evaluation (`sweep: event`) and its caller contract.

Covers seankoji-com/.github#74: an event or dispatch job evaluates only its own
head (plus that head's merge refs and every PR sharing it), routine CI arrivals
are admitted per head, every publisher still holds one repo-wide writer group,
and an unassociated review signal re-evaluates only the open PRs its previous
gate published to.

The API is an in-process fake that records every read, so "evaluates no other
open heads" is asserted on the request log rather than inferred.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

import yaml

from test_pr_gatekeeper import pr_gatekeeper

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github/workflows"
REUSABLE = WORKFLOWS / "reusable-pr-gatekeeper.yml"
CALLER = WORKFLOWS / "call-reusable-pr-gatekeeper.yml"

H1, H2, H3, NEW = "1" * 40, "2" * 40, "3" * 40, "4" * 40
M1, M2, M3, M4 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
REPO = "org/repo"
ACTIONS = pr_gatekeeper.GATE_APP_ID


# ── GitHub expression evaluation ─────────────────────────────────────────────


class _Null:
    """GitHub's null: property and index access on it yield null, never raise."""

    def __getattr__(self, name):
        return NULL

    def __getitem__(self, key):
        return NULL

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other is None or isinstance(other, _Null)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return 0

    def __str__(self):
        return ""


NULL = _Null()


class Ctx:
    """A context object whose missing properties are null, as in Actions."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        return NULL


class Arr(list):
    """An array whose out-of-range index is null, as in Actions."""

    def __getitem__(self, index):
        try:
            return super().__getitem__(index)
        except IndexError:
            return NULL


def _python(expression: str) -> str:
    return (" ".join(expression.split()).replace("&&", " and ").replace("||", " or ")
            .replace("null", "None"))


def evaluate(expression: str, context: dict):
    return eval(_python(expression), {"__builtins__": {}}, {
        **context,
        "fromJSON": json.loads,
        "contains": lambda values, value: value in values,
        "format": lambda pattern, *values: pattern.format(*(str(v) for v in values)),
    })


def render(template: str, context: dict) -> str:
    """Interpolate every `${{ ... }}` in a template string."""
    out, rest = "", template
    while "${{" in rest:
        before, _, tail = rest.partition("${{")
        expression, _, rest = tail.partition("}}")
        out += before + str(evaluate(expression, context))
    return out + rest


def pull(head_sha):
    return Ctx(head=Ctx(sha=head_sha))


def workflow_run_event(upstream, name, *, head_sha, pull_requests=()):
    return Ctx(workflow_run=Ctx(event=upstream, name=name, head_sha=head_sha, id=555,
                                pull_requests=Arr(pull(sha) for sha in pull_requests)))


def context(event_name, event, inputs=None, head_repo=REPO):
    if event_name in ("pull_request_target", "pull_request_review"):
        event = Ctx(pull_request=Ctx(number=7, head=Ctx(sha=H1, repo=Ctx(full_name=head_repo))))
    return {"github": Ctx(event_name=event_name, repository=REPO, run_id="42", event=event),
            "inputs": Ctx(**(inputs or {}))}


# Every caller event shape that can reach the gatekeeper workflow. Values are
# (context, admitted to the gatekeeper job, expected admission group, head).
CASES = {
    "CI on a same-repo PR": (
        context("workflow_run", workflow_run_event("pull_request", "CI", head_sha=H1, pull_requests=[H1])),
        True, f"pr-gatekeeper-refresh-{REPO}-{H1}", H1),
    "CI run whose PR head has since moved": (
        context("workflow_run", workflow_run_event("pull_request", "CI", head_sha=H2, pull_requests=[H1])),
        True, f"pr-gatekeeper-refresh-{REPO}-{H1}", H1),
    "fork CI with empty pull_requests": (
        context("workflow_run", workflow_run_event("pull_request", "CI", head_sha=H2)),
        True, f"pr-gatekeeper-refresh-{REPO}-{H2}", H2),
    "dynamic run with empty pull_requests": (
        context("workflow_run", workflow_run_event("dynamic", "CodeQL", head_sha=H3)),
        True, f"pr-gatekeeper-refresh-{REPO}-{H3}", H3),
    "push to a PR branch": (
        context("workflow_run", workflow_run_event("push", "CI", head_sha=H1, pull_requests=[H1])),
        True, f"pr-gatekeeper-refresh-{REPO}-{H1}", H1),
    "the gate's own completion": (
        context("workflow_run", workflow_run_event("pull_request_target", "PR Gatekeeper",
                                                   head_sha=H1, pull_requests=[H1])),
        False, "pr-gatekeeper-event-42", H1),
    "review bridge": (
        context("workflow_run", workflow_run_event("pull_request_review", "PR Gatekeeper",
                                                   head_sha=H1, pull_requests=[H1])),
        True, "pr-gatekeeper-event-42", H1),
    "fork review bridge": (
        context("workflow_run", workflow_run_event("pull_request_review", "PR Gatekeeper", head_sha=M1)),
        True, "pr-gatekeeper-event-42", M1),
    "controller dispatch": (
        context("workflow_dispatch", Ctx(), {"head_sha": H2}),
        True, "pr-gatekeeper-event-42", H2),
    "persona dispatch": (
        context("workflow_dispatch", Ctx(), {"head_sha": H3, "persona_state": "failed", "persona_pr": "9"}),
        True, "pr-gatekeeper-event-42", H3),
    "seed": (context("pull_request_target", Ctx()), True, "pr-gatekeeper-event-42", H1),
    "same-repo review": (context("pull_request_review", Ctx()), True, "pr-gatekeeper-event-42", H1),
    "unrelated push": (
        context("workflow_run", workflow_run_event("push", "Deploy", head_sha=H1)),
        False, "pr-gatekeeper-event-42", H1),
}


class CallerExpressionTests(unittest.TestCase):
    def setUp(self):
        self.caller = yaml.safe_load(CALLER.read_text())
        self.job = self.caller["jobs"]["gatekeeper"]

    def test_routine_ci_is_admitted_per_evaluated_head(self):
        group = self.caller["concurrency"]["group"]
        self.assertFalse(self.caller["concurrency"]["cancel-in-progress"])
        for label, (ctx, admitted, expected, head) in CASES.items():
            with self.subTest(label):
                self.assertEqual(render(group, ctx), expected)
                self.assertEqual(bool(evaluate(self.job["if"], ctx)), admitted)
                self.assertEqual(render(self.job["with"]["head_sha"], ctx), head)
                if expected.startswith("pr-gatekeeper-refresh-"):
                    # The coalescing key is the head the job then evaluates, so a
                    # replaced pending event is always for this same head.
                    self.assertTrue(expected.endswith("-" + head))

    def test_every_publisher_holds_one_repository_writer_group(self):
        self.assertEqual(self.job["concurrency"], {
            "group": "pr-gatekeeper-${{ github.repository }}", "cancel-in-progress": False, "queue": "max"})
        groups = {render(self.job["concurrency"]["group"], ctx)
                  for ctx, admitted, _, _ in CASES.values() if admitted}
        self.assertEqual(groups, {f"pr-gatekeeper-{REPO}"})
        self.assertEqual({name for name, job in self.caller["jobs"].items() if "concurrency" in job},
                         {"gatekeeper"})
        reusable = yaml.safe_load(REUSABLE.read_text())
        self.assertNotIn("concurrency", reusable["jobs"]["all-checks-passed"])

    def test_signal_inputs_follow_their_event(self):
        bridge = CASES["review bridge"][0]
        self.assertEqual(render(self.job["with"]["review_run_id"], bridge), "555")
        self.assertEqual(render(self.job["with"]["review_run_id"], CASES["CI on a same-repo PR"][0]), "")
        persona = CASES["persona dispatch"][0]
        self.assertEqual(render(self.job["with"]["persona_state"], persona), "failed")
        self.assertEqual(render(self.job["with"]["persona_pr"], persona), "9")
        self.assertEqual(render(self.job["with"]["seed"], CASES["seed"][0]), "True")

    def test_caller_selects_event_sweep_and_keeps_the_contract(self):
        self.assertEqual(self.job["with"]["sweep"], "event")
        self.assertNotIn("reconcile_open", self.job["with"])
        self.assertEqual(self.caller[True]["workflow_run"], {"workflows": ["*"], "types": ["completed"]})
        self.assertEqual(self.caller["permissions"]["checks"], "write")


# ── The reusable step's argument contract ────────────────────────────────────


class SweepInputTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(REUSABLE.read_text())
        self.step = self.config["jobs"]["all-checks-passed"]["steps"][1]

    def run_step(self, **env):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "pr-gatekeeper.py").write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            values = {"RUNNER_TEMP": directory, "GITHUB_REPOSITORY": REPO, "HEAD_SHA": H1,
                      "REQUIRE_CI": "false", "SEED": "false", "REVIEW_RUN_ID": "",
                      "PERSONA_STATE": "", "PERSONA_PR": "", **env}
            return subprocess.run(["bash", "-eu", "-c", self.step["run"]], env={**os.environ, **values},
                                  capture_output=True, text=True)

    def test_input_defaults_to_the_legacy_sweep(self):
        contract = self.config[True]["workflow_call"]["inputs"]["sweep"]
        self.assertEqual((contract["type"], contract["default"]), ("string", "open"))
        self.assertFalse(contract.get("required", False))
        self.assertEqual(self.step["env"]["SWEEP"], "${{ inputs.sweep }}")

    def test_arguments_per_mode(self):
        base = ["--repo", REPO, "--sha", H1]
        for env, expected in (
            ({}, []),
            ({"REQUIRE_CI": "true"}, ["--require-ci", "--reconcile-open"]),
            ({"SWEEP": "open", "REQUIRE_CI": "true"}, ["--require-ci", "--reconcile-open"]),
            ({"SWEEP": "event"}, ["--require-ci"]),
            ({"SWEEP": "event", "REQUIRE_CI": "true"}, ["--require-ci"]),
            ({"SWEEP": "event", "SEED": "true"}, ["--require-ci", "--seed"]),
            ({"SWEEP": "event", "REVIEW_RUN_ID": "123"}, ["--require-ci", "--review-run", "123"]),
            ({"SWEEP": "event", "PERSONA_STATE": "running", "PERSONA_PR": "9"},
             ["--require-ci", "--persona-state", "running", "--persona-pr", "9"]),
        ):
            with self.subTest(env=env):
                result = self.run_step(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), base + expected)

    def test_event_and_persona_modes_reject_a_missing_sha(self):
        for env in ({"SWEEP": "event", "HEAD_SHA": ""},
                    {"SWEEP": "event", "HEAD_SHA": "main"},
                    {"SWEEP": "event", "HEAD_SHA": H1 + "\n" + H2},
                    {"SWEEP": "event", "HEAD_SHA": "", "PERSONA_STATE": "failed", "PERSONA_PR": "9"}):
            with self.subTest(env=env):
                result = self.run_step(**env)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("--repo", result.stdout)
                self.assertIn("::error::sweep=event requires a 40-hex head_sha", result.stdout)

    def test_unknown_sweep_is_rejected_before_evaluation(self):
        result = self.run_step(SWEEP="evnt")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("--repo", result.stdout)
        self.assertIn("::error::Invalid sweep", result.stdout)


# ── A recording fake of the GitHub API ───────────────────────────────────────


def check(name, conclusion="success", status="completed", ident=1, suite=10):
    return {"id": ident, "name": name, "status": status, "conclusion": conclusion,
            "app": {"id": ACTIONS}, "check_suite": {"id": suite}}


def pr(number, head, merge, repo_id=1, ref=None):
    return {"number": number, "merge_commit_sha": merge,
            "head": {"sha": head, "ref": ref or f"branch-{number}", "repo": {"id": repo_id}}}


class FakeAPI:
    def __init__(self, prs, checks=None, runs=None, reviews=None, action_runs=None, gate_history=None):
        self.prs = prs
        self.checks = checks or {}
        self.runs = runs or {}
        self.reviews = reviews or {}
        self.action_runs = action_runs or {}
        self.gate_history = gate_history or {}
        self.reads: list[str] = []
        self.posts: list[dict] = []
        self.lock = threading.Lock()

    def ref_of(self, path):
        return path.split("/commits/", 1)[1].split("/", 1)[0]

    def get(self, path, token):
        with self.lock:
            self.reads.append(path)
        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path
        if route == f"/repos/{REPO}/pulls" and query.get("state") == ["open"]:
            return list(self.prs)
        if route.startswith(f"/repos/{REPO}/pulls/") and route.endswith("/reviews"):
            return list(self.reviews.get(int(route.split("/")[5]), []))
        if route.endswith("/check-runs") and query.get("filter") == ["all"]:
            return {"check_runs": list(self.gate_history.get(self.ref_of(route), []))}
        if route.endswith("/check-runs"):
            return {"check_runs": list(self.checks.get(self.ref_of(route), []))}
        if route.endswith("/status"):
            return {"total_count": 0, "statuses": []}
        if route.endswith("/check-suites"):
            return {"check_suites": []}
        if route == f"/repos/{REPO}/actions/runs":
            runs = self.action_runs.get(query["head_sha"][0], [])
            return {"total_count": len(runs), "workflow_runs": list(runs)}
        if route.startswith(f"/repos/{REPO}/actions/runs/"):
            return self.runs[int(route.rsplit("/", 1)[1])]
        raise AssertionError(f"unexpected read {path}")

    def post(self, path, token, body):
        assert path == f"/repos/{REPO}/check-runs", path
        with self.lock:
            self.posts.append(body)
        return {"id": len(self.posts)}

    def read_refs(self):
        return {self.ref_of(p) for p in self.reads if "/commits/" in p} | {
            urllib.parse.parse_qs(urllib.parse.urlparse(p).query)["head_sha"][0]
            for p in self.reads if "/actions/runs?" in p}

    def final(self):
        """The last publication per destination, as the PR would show it."""
        out = {}
        for body in self.posts:
            out[body["head_sha"]] = (body["status"], body.get("conclusion"))
        return out


AMBIENT = ("PERSONA_REVIEW_REQUIRED", "GITHUB_STEP_SUMMARY", "GITHUB_RUN_ID", "GATEKEEPER_REQUIRED_WORKFLOWS")


def isolated_environment(**env):
    """patch.dict over os.environ with ambient Actions settings removed."""
    environment = patch.dict(os.environ, env)
    environment.start()
    for key in AMBIENT:
        if key not in env:
            os.environ.pop(key, None)
    return environment


def main_with(api, argv, env=None):
    environment = isolated_environment(GITHUB_TOKEN="caller-token", **(env or {}))
    try:
        with patch.object(pr_gatekeeper, "_get", side_effect=api.get), \
             patch.object(pr_gatekeeper, "_post", side_effect=api.post), \
             patch("sys.stderr"), patch("sys.stdout"):
            return pr_gatekeeper.main(argv)
    finally:
        environment.stop()


def persona_review(sha, state):
    return {"id": 3, "user": {"id": pr_gatekeeper.PERSONA_USER_ID, "type": "Bot"}, "state": state,
            "commit_id": sha, "submitted_at": "2026-11-01T00:00:00Z"}


GREEN = [check("ci")]


class SingleHeadEvaluationTests(unittest.TestCase):
    """`--require-ci` without `--reconcile-open`, as `sweep: event` invokes it."""

    def fleet(self, **overrides):
        # PR 1 and PR 2 share head H1; PR 3 is an unrelated open head.
        prs = [pr(1, H1, M1), pr(2, H1, M2), pr(3, H3, M3)]
        checks = {H1: GREEN, M1: GREEN, M2: GREEN, H3: [check("ci", "failure")], M3: GREEN}
        return FakeAPI(overrides.pop("prs", prs), overrides.pop("checks", checks), **overrides)

    def test_event_evaluates_only_its_head_merge_refs_and_shared_prs(self):
        api = self.fleet()
        self.assertEqual(main_with(api, ["--repo", REPO, "--sha", H1, "--require-ci"]), 0)
        self.assertEqual(api.read_refs(), {H1, M1, M2})
        self.assertEqual(api.final(), {ref: ("completed", "success") for ref in (H1, M1, M2)})
        self.assertFalse(any(H3 in path or M3 in path for path in api.reads))

    def test_dispatch_persona_signal_applies_to_the_dispatched_head_only(self):
        api = self.fleet(reviews={1: [persona_review(H1, "APPROVED")], 2: [persona_review(H1, "APPROVED")],
                                  3: [persona_review(H3, "APPROVED")]})
        code = main_with(api, ["--repo", REPO, "--sha", H1, "--require-ci",
                               "--persona-state", "failed", "--persona-pr", "1"],
                         env={"PERSONA_REVIEW_REQUIRED": "true"})
        self.assertEqual(code, 0)
        self.assertEqual(api.final(), {ref: ("completed", "success") for ref in (H1, M1, M2)})
        api = self.fleet(reviews={1: [], 2: [persona_review(H1, "APPROVED")]})
        main_with(api, ["--repo", REPO, "--sha", H1, "--require-ci", "--persona-state", "failed",
                        "--persona-pr", "1"], env={"PERSONA_REVIEW_REQUIRED": "true"})
        self.assertEqual(api.final(), {ref: ("completed", "failure") for ref in (H1, M1, M2)})
        self.assertFalse(any("/pulls/3/" in path or H3 in path for path in api.reads))

    def test_red_check_fails_every_destination(self):
        api = self.fleet(checks={H1: GREEN, M1: GREEN, M2: [check("merge test", "failure")]})
        main_with(api, ["--repo", REPO, "--sha", H1, "--require-ci"])
        self.assertEqual(api.final(), {ref: ("completed", "failure") for ref in (H1, M1, M2)})

    def test_shared_head_red_on_one_head_check_blocks_both_prs(self):
        api = self.fleet(checks={H1: [check("ci", "failure")], M1: GREEN, M2: GREEN})
        main_with(api, ["--repo", REPO, "--sha", M2, "--require-ci"])
        self.assertEqual(api.final(), {ref: ("completed", "failure") for ref in (H1, M1, M2)})

    def test_queued_workflow_without_jobs_holds_the_gate_pending(self):
        queued = {"id": 50, "head_sha": H1, "path": ".github/workflows/ci.yml", "workflow_id": 9,
                  "event": "pull_request", "head_branch": "branch-1", "status": "queued",
                  "conclusion": None, "check_suite_id": 77}
        api = self.fleet(action_runs={H1: [queued]})
        main_with(api, ["--repo", REPO, "--sha", H1, "--require-ci"])
        self.assertEqual(api.final(), {ref: ("in_progress", None) for ref in (H1, M1, M2)})

    def test_fork_pr_without_ci_registration_stays_pending(self):
        fork = pr(4, NEW, M4, repo_id=99)
        api = FakeAPI([fork, pr(3, H3, M3)], {H3: GREEN, M3: GREEN})
        main_with(api, ["--repo", REPO, "--sha", NEW, "--require-ci"])
        self.assertEqual(api.read_refs(), {NEW, M4})
        self.assertEqual(api.final(), {ref: ("in_progress", None) for ref in (NEW, M4)})

    def test_review_event_resolving_to_a_newer_head_evaluates_that_head(self):
        run = {"event": "pull_request_review", "head_sha": H2, "pull_requests": [{"number": 1}],
               "head_repository": {"id": 1}, "head_branch": "branch-1"}
        api = self.fleet(prs=[pr(1, NEW, M4), pr(3, H3, M3)],
                         checks={NEW: [check("ci", "failure")], M4: GREEN}, runs={5: run})
        code = main_with(api, ["--repo", REPO, "--sha", H2, "--require-ci", "--review-run", "5"])
        self.assertEqual(code, 0)
        self.assertEqual(api.final(), {NEW: ("completed", "failure"), M4: ("completed", "failure")})
        self.assertEqual(api.read_refs(), {NEW, M4})

    def test_unreadable_pr_list_fails_closed_on_the_event_head(self):
        api = self.fleet()
        read = api.get

        def broken(path, token):
            if "/pulls?" in path:
                raise ValueError("invalid paginated list response")
            return read(path, token)

        api.get = broken
        self.assertEqual(main_with(api, ["--repo", REPO, "--sha", H1, "--require-ci"]), 2)
        self.assertEqual(api.final(), {H1: ("completed", "failure")})


def previous_gate(refs):
    summary = (f"x\n{pr_gatekeeper.EVALUATED_MARKER}\n<!-- gatekeeper-refs:{json.dumps(sorted(refs))} -->"
               "\n<!-- gatekeeper-refs-complete -->")
    return {"id": 8, "name": pr_gatekeeper.GATE_CHECK_NAME, "app": {"id": ACTIONS},
            "status": "completed", "conclusion": "success", "output": {"summary": summary}}


class StaleReviewSignalTests(unittest.TestCase):
    """A review signal no open PR claims, evaluated without a sweep."""

    RUN = {"event": "pull_request_review", "head_sha": M1, "pull_requests": [],
           "head_repository": None, "head_branch": "gone"}

    def test_reevaluates_open_prs_named_by_the_previous_gate(self):
        # A deleted fork: no branch identity, and base movement replaced M1.
        api = FakeAPI([pr(1, H1, M2, repo_id=None), pr(3, H3, M3)],
                      {H1: [check("ci", "failure")], M2: GREEN},
                      runs={5: self.RUN}, gate_history={M1: [previous_gate({H1, M1})]})
        self.assertEqual(main_with(api, ["--repo", REPO, "--sha", M1, "--require-ci", "--review-run", "5"]), 0)
        self.assertEqual(api.final(), {H1: ("completed", "failure"), M2: ("completed", "failure")})
        self.assertNotIn(H3, api.read_refs())

    def test_signal_for_a_closed_pr_publishes_nothing(self):
        api = FakeAPI([pr(3, H3, M3)], runs={5: self.RUN}, gate_history={M1: [previous_gate({H1, M1})]})
        self.assertEqual(main_with(api, ["--repo", REPO, "--sha", M1, "--require-ci", "--review-run", "5"]), 0)
        self.assertEqual(api.posts, [])

    def test_unreadable_previous_gate_fails_closed(self):
        api = FakeAPI([pr(3, H3, M3)], runs={5: self.RUN},
                      gate_history={M1: [dict(previous_gate({H1}), output={"summary":
                                    "<!-- gatekeeper-refs:[\"not-a-sha\"] --><!-- gatekeeper-refs-complete -->"})]})
        self.assertEqual(main_with(api, ["--repo", REPO, "--sha", M1, "--require-ci", "--review-run", "5"]), 2)
        self.assertEqual({body["conclusion"] for body in api.posts}, {"failure"})
        self.assertIn(M1, {body["head_sha"] for body in api.posts})


class ReadStartMarkerTests(unittest.TestCase):
    """The hub reconciler compares check activity with this recorded instant."""

    MARKER = re.compile(r"<!-- gatekeeper-read-start:(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) -->")

    def finalize(self, status, conclusion, summary="body", refs=("a" * 40,), awaiting=False):
        with patch.object(pr_gatekeeper.workflows.time, "time", return_value=1_793_534_430.9):
            return pr_gatekeeper.workflows.finalize_report(
                status, conclusion, "title", summary, refs=set(refs), refs_complete=True,
                awaiting_ci=awaiting, elapsed=30.5, result=0)

    def test_every_published_verdict_records_its_read_start(self):
        for status, conclusion, awaiting in (("completed", "success", False), ("in_progress", None, False),
                                             ("in_progress", None, True), ("completed", "failure", False)):
            with self.subTest(status=status, awaiting=awaiting):
                summary = self.finalize(status, conclusion, awaiting=awaiting)[3]
                # 1793534430.9 - 30.5 s, floored: reads began at 2026-11-01T12:00:00Z.
                self.assertEqual(self.MARKER.findall(summary), ["2026-11-01T12:00:00Z"])

    def test_marker_survives_truncation_and_the_destination_overflow_failure(self):
        long = self.finalize("completed", "failure", summary="x" * 70000)[3]
        self.assertTrue(long.rstrip().endswith("<!-- gatekeeper-refs-complete -->"))
        self.assertEqual(len(self.MARKER.findall(long)), 1)
        overflow = self.finalize("completed", "failure", refs=[f"{i:040x}" for i in range(2000)])
        self.assertEqual(overflow[2], "Too many gate destinations")
        self.assertEqual(len(self.MARKER.findall(overflow[3])), 1)

    def test_check_names_cannot_forge_the_marker(self):
        forged = pr_gatekeeper.workflows.safe_text("<!-- gatekeeper-read-start:2000-01-01T00:00:00Z -->")
        self.assertNotIn("<!--", forged)


# ── Writer-queue interleaving ────────────────────────────────────────────────


class WriterQueueInterleavingTests(unittest.TestCase):
    """Two publishers for one destination, with the head's state changing.

    Publisher A was triggered while CI was green; then CI turned red (or a
    rerun started) and publisher B was triggered. The repo-wide writer group
    makes each job's read-then-publish atomic with respect to other jobs, so
    the only schedules are A-then-B and B-then-A. Without the group, A's read
    can precede B and its publication follow B's.
    """

    def setUp(self):
        self.addCleanup(isolated_environment().stop)

    def run_publisher(self, api):
        with patch.object(pr_gatekeeper, "_get", side_effect=api.get), \
             patch.object(pr_gatekeeper, "_post", side_effect=api.post), \
             patch("sys.stderr"), patch("sys.stdout"):
            return pr_gatekeeper.report(REPO, H1, "token", require_ci=True)

    def scenario(self, newer):
        api = FakeAPI([pr(1, H1, M1)], {H1: GREEN, M1: GREEN})
        return api, lambda: api.checks.__setitem__(H1, [newer])

    def newer_states(self):
        return {"failure": (check("ci", "failure", ident=2), ("completed", "failure")),
                "pending": (check("ci", None, status="in_progress", ident=2), ("in_progress", None))}

    def test_serialized_schedules_never_end_on_the_stale_success(self):
        for label, (newer, expected) in self.newer_states().items():
            for order in ("A then B", "B then A"):
                with self.subTest(newer=label, order=order):
                    api, change = self.scenario(newer)
                    if order == "A then B":
                        self.run_publisher(api)   # A reads green, publishes green
                        change()
                        self.run_publisher(api)   # B reads the newer state
                    else:
                        change()
                        self.run_publisher(api)   # B
                        self.run_publisher(api)   # A, admitted late, re-reads
                    self.assertEqual(api.final(), {H1: expected, M1: expected})

    def test_without_the_writer_group_a_stale_success_can_land_last(self):
        newer, _ = self.newer_states()["failure"]
        api, change = self.scenario(newer)
        a_read, release_a = threading.Event(), threading.Event()
        post = api.post

        def delayed_post(path, token, body):
            if threading.current_thread().name == "A":
                a_read.set()
                release_a.wait(5)
            return post(path, token, body)

        api.post = delayed_post
        a = threading.Thread(target=self.run_publisher, args=(api,), name="A")
        a.start()
        self.assertTrue(a_read.wait(5))
        change()
        self.run_publisher(api)  # B publishes the failure first
        release_a.set()
        a.join(5)
        self.assertEqual(api.final()[H1], ("completed", "success"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
