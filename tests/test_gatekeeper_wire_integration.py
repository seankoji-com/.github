#!/usr/bin/env python3
"""HTTP wire integration tests for scripts/pr-gatekeeper.py and caller workflows.

Exercises real loopback HTTP socket transport without mocking _get, _post,
urllib, or HTTP parsing. Confirms Authorization header transmission, HTTP 403
fail-closed handling for unprivileged tokens, and end-to-end check run
publication on privileged workflow_run and same-repo review events.

Integrates caller workflow YAML admission evaluation (.github/workflows)
with the loopback HTTP execution to verify that job admission decisions
and wire publication are strictly aligned.
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE_CALLER = ROOT / ".github" / "workflows" / "call-reusable-pr-gatekeeper.yml"
REVIEW_EVENT_CALLER = ROOT / ".github" / "workflows" / "call-reusable-review-event.yml"

_SPEC = importlib.util.spec_from_file_location(
    "pr_gatekeeper",
    ROOT / "scripts" / "pr-gatekeeper.py",
)
pr_gatekeeper = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("pr_gatekeeper", pr_gatekeeper)
_SPEC.loader.exec_module(pr_gatekeeper)


def evaluate_gatekeeper_caller_admission(
    event_name: str,
    repository: str,
    head_repo_name: str,
    workflow_run_event: str | None = None,
    workflow_run_name: str | None = None,
) -> bool:
    """Evaluate jobs.gatekeeper.if in call-reusable-pr-gatekeeper.yml."""
    caller = yaml.safe_load(GATE_CALLER.read_text())
    gate_if = caller["jobs"]["gatekeeper"]["if"]
    expression = (
        " ".join(gate_if.split())
        .replace("&&", " and ")
        .replace("||", " or ")
        .replace("null", "None")
    )
    github = SimpleNamespace(
        event_name=event_name,
        repository=repository,
        event=SimpleNamespace(
            pull_request=SimpleNamespace(
                head=SimpleNamespace(repo=SimpleNamespace(full_name=head_repo_name))
            ),
            workflow_run=SimpleNamespace(
                event=workflow_run_event,
                name=workflow_run_name,
                pull_requests=[{}] if workflow_run_event else [None],
            )
            if event_name == "workflow_run"
            else SimpleNamespace(),
        ),
    )
    context = {
        "github": github,
        "fromJSON": json.loads,
        "contains": lambda values, value: value in values,
    }
    return bool(eval(expression, {"__builtins__": {}}, context))


class TestReviewEventHTTPWireIntegration(unittest.TestCase):
    """End-to-end integration tests over real loopback HTTP sockets."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            self.server.requests.append(("GET", self.path, auth, None))
            if self.path.startswith("/repos/org/repo/pulls?"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                repo_name = getattr(self.server, "head_repo_name", "fork-contributor/repo")
                prs = [{
                    "number": 7,
                    "head": {"sha": "head_sha", "repo": {"full_name": repo_name}},
                    "base": {"sha": "base_sha"},
                }]
                self.wfile.write(json.dumps(prs).encode())
            elif self.path == "/repos/org/repo/actions/runs/123":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                run_data = {
                    "event": "pull_request_review",
                    "head_sha": "head_sha",
                    "pull_requests": [{"number": 7}],
                }
                self.wfile.write(json.dumps(run_data).encode())
            elif self.path.startswith("/repos/org/repo/pulls/7/reviews"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                reviews = [{
                    "id": 101,
                    "user": {"id": 283599686, "login": "bot-grumpy-engineer[bot]", "type": "Bot"},
                    "state": "APPROVED",
                    "commit_id": "head_sha",
                    "submitted_at": "2026-10-18T00:00:00Z",
                }]
                self.wfile.write(json.dumps(reviews).encode())
            elif "/check-runs" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                checks = {
                    "total_count": 1,
                    "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success", "app": {"id": 15368}}],
                }
                self.wfile.write(json.dumps(checks).encode())
            elif "/status?" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"total_count": 0, "statuses": []}).encode())
            elif "/check-suites?" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"total_count": 0, "check_suites": []}).encode())
            elif "/actions/runs?" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"total_count": 0, "workflow_runs": []}).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode()) if length else None
            self.server.requests.append(("POST", self.path, auth, body))
            if self.path == "/repos/org/repo/check-runs":
                if "unprivileged_token" in auth:
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"message": "Resource not accessible by integration"}')
                else:
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "id": 999,
                        "status": body.get("status"),
                        "conclusion": body.get("conclusion"),
                    }).encode())
            else:
                self.send_response(404)
                self.end_headers()

    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), self._Handler)
        self.server.requests = []
        self.server.head_repo_name = "fork-contributor/repo"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_fork_unprivileged_token_fails_with_403_diagnostic_over_wire(self):
        # 1. Verify caller admission expression correctly rejects direct fork review execution
        admitted = evaluate_gatekeeper_caller_admission(
            event_name="pull_request_review",
            repository="org/repo",
            head_repo_name="fork-contributor/repo",
        )
        self.assertFalse(admitted, "Direct fork PR review must NOT be admitted to call-reusable-pr-gatekeeper")

        # 2. Verify defense-in-depth: if unprivileged token runs anyway, 403 Forbidden is trapped over wire
        stderr = io.StringIO()
        env = {
            "GITHUB_API_URL": f"http://127.0.0.1:{self.port}",
            "GITHUB_TOKEN": "unprivileged_token",
            "PERSONA_REVIEW_REQUIRED": "true",
        }
        with patch.dict(os.environ, env), patch("sys.stderr", stderr):
            code = pr_gatekeeper.main(["--repo", "org/repo", "--sha", "head_sha", "--reconcile-open"])
        self.assertEqual(code, 2)
        err = stderr.getvalue()
        self.assertIn("403 Forbidden", err)
        self.assertIn("fork review token restriction", err)
        post_reqs = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(post_reqs), 1)
        self.assertEqual(post_reqs[0][2], "Bearer unprivileged_token")

    def test_fork_privileged_workflow_run_followup_publishes_success_over_wire(self):
        # 1. Verify PR Review Event caller triggers on pull_request_review
        review_caller = yaml.safe_load(REVIEW_EVENT_CALLER.read_text())
        self.assertIn("pull_request_review", (review_caller.get("on") or review_caller.get(True)))

        # 2. Verify call-reusable-pr-gatekeeper admits completion of PR Review Event via workflow_run
        admitted = evaluate_gatekeeper_caller_admission(
            event_name="workflow_run",
            repository="org/repo",
            head_repo_name="fork-contributor/repo",
            workflow_run_event="pull_request_review",
            workflow_run_name="PR Review Event",
        )
        self.assertTrue(admitted, "workflow_run follow-up for PR Review Event must be admitted")

        # 3. Verify privileged follow-up evaluates persona review and publishes success over wire
        env = {
            "GITHUB_API_URL": f"http://127.0.0.1:{self.port}",
            "GITHUB_TOKEN": "privileged_token",
            "PERSONA_REVIEW_REQUIRED": "true",
        }
        with patch.dict(os.environ, env):
            code = pr_gatekeeper.main([
                "--repo", "org/repo", "--sha", "head_sha", "--reconcile-open", "--review-run", "123"
            ])
        self.assertEqual(code, 0)
        post_reqs = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(post_reqs), 1)
        _, path, auth, body = post_reqs[0]
        self.assertEqual(path, "/repos/org/repo/check-runs")
        self.assertEqual(auth, "Bearer privileged_token")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["conclusion"], "success")
        self.assertIn("<!-- grumpy-review:7:completed:success -->", body["output"]["summary"])

    def test_same_repo_review_event_evaluates_and_publishes_success_over_wire(self):
        # 1. Verify direct same-repo pull_request_review is admitted
        admitted = evaluate_gatekeeper_caller_admission(
            event_name="pull_request_review",
            repository="org/repo",
            head_repo_name="org/repo",
        )
        self.assertTrue(admitted, "Same-repo pull_request_review must be directly admitted")

        # 2. Verify direct same-repo evaluation publishes success over wire
        self.server.head_repo_name = "org/repo"
        env = {
            "GITHUB_API_URL": f"http://127.0.0.1:{self.port}",
            "GITHUB_TOKEN": "privileged_token",
            "PERSONA_REVIEW_REQUIRED": "true",
        }
        with patch.dict(os.environ, env):
            code = pr_gatekeeper.main([
                "--repo", "org/repo", "--sha", "head_sha", "--reconcile-open"
            ])
        self.assertEqual(code, 0)
        post_reqs = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(post_reqs), 1)
        _, path, auth, body = post_reqs[0]
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["conclusion"], "success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
