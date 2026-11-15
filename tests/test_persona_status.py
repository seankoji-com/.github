"""Persona policy and actual HTTP wire tests; stdlib-only for Python 3.10 CI."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "actions/persona-status/publish.py"
SPEC = importlib.util.spec_from_file_location("persona_status", SOURCE)
persona = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = persona
SPEC.loader.exec_module(persona)
HEAD, NEXT = "a" * 40, "b" * 40
REPO, PUBLISHER = "example/project", 9001


def pr(number=1, head=HEAD, state="open"):
    return {"id": number, "number": number, "state": state, "head": {"sha": head}, "merge_commit_sha": None,
            "base": {"repo": {"full_name": REPO}}}


def review(ident=1, state="APPROVED", head=HEAD, actor=persona.REVIEWER_ID, kind="Bot"):
    return {"id": ident, "state": state, "commit_id": head,
            "submitted_at": "2026-11-15T00:00:00Z", "user": {"id": actor, "type": kind},
            "body": "fixture-private-review-body"}


class GitHub:
    """REST-shaped fixture, storing statuses newest-first as the API does."""
    def __init__(self):
        self.prs = {1: pr()}
        self.reviews = {1: [review()]}
        self.statuses = []
        self.posts, self.reads = [], []
        self.writer = PUBLISHER
        self.hook = None
        self.broken = None

    def request(self, path, body=None):
        if self.hook:
            self.hook(path, body)
        if self.broken and self.broken(path, body):
            raise persona.Unverified("api_http_403")
        if body is not None:
            assert path == "/repos/" + REPO + "/statuses/" + HEAD
            status = {**body, "id": max([s["id"] for s in self.statuses] + [0]) + 1,
                      "creator": {"id": self.writer, "type": "Bot"}}
            self.statuses.insert(0, status)
            self.posts.append(copy.deepcopy(status))
            return copy.deepcopy(status)
        self.reads.append(path)
        parsed = urlsplit(path)
        page = int(parse_qs(parsed.query).get("page", [1])[0])
        suffix = parsed.path.removeprefix("/repos/" + REPO)
        if suffix.startswith("/pulls/") and not suffix.endswith("/reviews"):
            return copy.deepcopy(self.prs[int(suffix.rsplit("/", 1)[1])])
        if suffix.endswith("/reviews"):
            data = self.reviews.get(int(suffix.split("/")[-2]), [])
        elif suffix == "/pulls":
            data = [p for p in self.prs.values() if p["state"] == "open"]
        elif suffix.endswith("/statuses"):
            data = sorted(self.statuses, key=lambda s: s["id"], reverse=True)
        elif suffix.endswith("/status"):
            latest = {}
            for status in sorted(self.statuses, key=lambda s: s["id"], reverse=True):
                latest.setdefault(status["context"].casefold(), status)
            # GitHub's Simple Commit Status omits creator. Only the list/create
            # schemas contain identity: do not make this fixture over-permissive.
            data = [{k: v for k, v in status.items() if k != "creator"}
                    for status in latest.values()]
            return {"sha": HEAD, "statuses": copy.deepcopy(data[(page - 1) * 100:page * 100])}
        else:
            raise AssertionError("unexpected fixture endpoint")
        return copy.deepcopy(data[(page - 1) * 100:page * 100])


class PersonaTests(unittest.TestCase):
    def publisher(self, api=None, *, dry=False, number=1):
        return persona.Publisher(persona.Target(REPO, number, HEAD, PUBLISHER, dry), api or GitHub(), policy_hash="policy-fixture")

    def test_exact_submitted_bot_approval_only(self):
        for records, state in (([review()], "success"), ([], "pending"),
                               ([review(state="DISMISSED")], "pending"),
                               ([review(state="CHANGES_REQUESTED")], "failure"),
                               ([review(state="COMMENTED")], "pending"),
                               ([review(state="PENDING")], "pending"),
                               ([review(head=NEXT)], "pending"),
                               ([review(actor=42)], "pending"),
                               ([review(kind="User")], "pending")):
            with self.subTest(state=state, records=records):
                api = GitHub()
                api.reviews[1] = records
                result, code = self.publisher(api).run()
                self.assertEqual((result["state"], code), (state, 0))
                self.assertTrue(result["status_verified"])
                self.assertEqual(api.posts[-1]["state"], state)
                self.assertEqual({p["context"] for p in api.posts}, {persona.CONTEXT})

    def test_schema_accurate_creator_readback_and_repeat_error_dedup(self):
        api = GitHub()
        result, code = self.publisher(api).run()
        self.assertEqual((result["state"], code), ("success", 0))
        self.assertTrue(result["status_verified"])
        combined = api.request("/repos/" + REPO + "/commits/" + HEAD + "/status")
        self.assertNotIn("creator", combined["statuses"][0])
        self.assertFalse(any("/status?" in path for path in api.reads))
        api.reviews[1] = [review(2, "CHANGES_REQUESTED")]
        result, code = self.publisher(api).run()
        self.assertEqual((result["state"], code), ("failure", 0))
        count = len(api.posts)
        self.publisher(api).run()
        self.assertEqual(len(api.posts), count)

    def test_latest_submission_and_paginated_reviews_control_verdict(self):
        api = GitHub()
        api.reviews[1] = [review(i, actor=42) for i in range(1, 101)] + [review(101, "CHANGES_REQUESTED")]
        result, _ = self.publisher(api).run()
        self.assertEqual(result["state"], "failure")
        self.assertTrue(any("/reviews?per_page=100&page=2" in p for p in api.reads))
        api.reviews[1] = [review(200, "APPROVED"), {**review(1, "DISMISSED"), "submitted_at": "2026-11-15T01:00:00Z"}]
        self.assertEqual(self.publisher(api).run()[0]["state"], "pending")

    def test_every_current_pr_sharing_head_needs_its_own_review(self):
        api = GitHub()
        api.prs[2] = pr(2)
        api.reviews[2] = []
        result, _ = self.publisher(api).run()
        self.assertEqual(result["state"], "pending")
        api.reviews[2] = [review(2)]
        self.assertEqual(self.publisher(api).run()[0]["state"], "success")

    def test_new_shared_head_pr_queued_after_success_cannot_reuse_old_certificate(self):
        api = GitHub()
        first, _ = self.publisher(api).run()
        api.prs[2] = pr(2)
        api.reviews[2] = []
        # Until the queued writer runs, a SHA status can still be old success.
        # Root's per-PR actual-review merge preflight must reject unreviewed PR2.
        self.assertEqual(api.statuses[0]["state"], "success")
        current, _ = self.publisher(api).run()  # surviving intent still targets PR1
        self.assertEqual(current["state"], "pending")
        self.assertNotEqual(first["certificate"], current["certificate"])

    def test_duplicates_are_idempotent_only_after_current_reads(self):
        api = GitHub()
        first, _ = self.publisher(api).run()
        count, reads = len(api.posts), len(api.reads)
        again, _ = self.publisher(api).run()
        self.assertEqual(len(api.posts), count)
        self.assertGreater(len(api.reads), reads)
        self.assertEqual(first["certificate"], again["certificate"])
        api.reviews[1][0]["state"] = "DISMISSED"
        self.assertEqual(self.publisher(api).run()[0]["state"], "pending")

    def test_policy_version_invalidates_cache(self):
        api = GitHub()
        first, _ = self.publisher(api).run()
        publisher = self.publisher(api)
        publisher.policy_hash = "changed-policy"
        second, _ = publisher.run()
        self.assertNotEqual(first["certificate"], second["certificate"])
        self.assertTrue(second["changed"])

    def test_wrong_status_creator_never_receives_success(self):
        api = GitHub()
        api.writer = 42
        result, code = self.publisher(api).run()
        self.assertEqual(code, 2)
        self.assertFalse(result["status_verified"])
        self.assertNotIn("success", [p["state"] for p in api.posts])

    def test_dismissal_during_history_or_after_success_is_refreshed(self):
        for trigger in ("history", "after_success"):
            with self.subTest(trigger=trigger):
                api = GitHub()
                fired = []
                def change(path, body):
                    if not fired and ((trigger == "history" and path.endswith("/statuses?per_page=100&page=1"))
                                      or (trigger == "after_success" and body and body["state"] == "success")):
                        fired.append(True)
                        api.reviews[1][0]["state"] = "DISMISSED"
                api.hook = change
                result, _ = self.publisher(api).run()
                self.assertTrue(fired)
                self.assertEqual(result["state"], "pending")
                self.assertEqual(api.statuses[0]["state"], "pending")

    def test_superseded_or_closed_without_members_writes_nothing(self):
        for target in (pr(head=NEXT), pr(state="closed")):
            api = GitHub()
            api.prs[1] = target
            result, code = self.publisher(api).run()
            self.assertEqual(code, 0)
            self.assertTrue(result["stale"])
            self.assertEqual(api.posts, [])

    def test_closed_same_head_refreshes_only_current_open_members(self):
        api = GitHub()
        api.prs[1] = pr(state="closed")
        api.prs[2] = pr(2)
        api.reviews[1] = [review(1)]
        api.reviews[2] = []
        self.assertEqual(self.publisher(api).run()[0]["state"], "pending")
        self.assertFalse(any("/pulls/1/reviews" in path for path in api.reads))
        api.reviews[2] = [review(2)]
        self.assertEqual(self.publisher(api).run()[0]["state"], "success")

    def test_merge_sha_equal_to_another_pr_head_blocks_without_cross_head_write(self):
        for target_is_merge in (False, True):
            with self.subTest(target_is_merge=target_is_merge):
                api = GitHub()
                api.prs[2] = pr(2, head=NEXT)
                api.reviews[2] = [review(2, head=NEXT)]
                if target_is_merge:
                    api.prs[2]["merge_commit_sha"] = HEAD
                else:
                    api.prs[1]["merge_commit_sha"] = NEXT
                result, code = self.publisher(api).run()
                self.assertEqual((result["state"], code), ("error", 2))
                self.assertEqual(result["reason"], "head_merge_collision")
                self.assertNotIn("success", [p["state"] for p in api.posts])

    def test_head_moves_after_write_revokes_only_original_head(self):
        api = GitHub()
        def move(path, body):
            if body and body["state"] == "success":
                api.prs[1]["head"]["sha"] = NEXT
        api.hook = move
        result, code = self.publisher(api).run()
        self.assertEqual((code, result["state"]), (2, "error"))
        self.assertTrue(result["bound"])
        self.assertEqual(api.statuses[0]["state"], "error")
        self.assertFalse(any(NEXT in path for path in api.reads))

    def test_api_errors_after_binding_invalidate_but_initial_failure_is_unverified(self):
        for initial in (False, True):
            api = GitHub()
            api.broken = lambda path, body: body is None and ("/reviews" in path or initial)
            result, code = self.publisher(api).run()
            self.assertEqual(code, 2)
            self.assertEqual(result["status_verified"], not initial)
            self.assertEqual(result["bound"], not initial)
            self.assertEqual(bool(api.posts), not initial)

    def test_dry_run_never_posts_even_on_faults(self):
        for records in ([review()], [], [review(state="CHANGES_REQUESTED")]):
            api = GitHub()
            api.reviews[1] = records
            result, _ = self.publisher(api, dry=True).run()
            self.assertTrue(result["dry_run"])
            self.assertEqual(api.posts, [])
        api = GitHub()
        api.broken = lambda path, body: "/reviews" in path
        self.assertEqual(self.publisher(api, dry=True).run()[1], 2)
        self.assertEqual(api.posts, [])

    def test_lost_post_response_is_not_retried_and_error_replaces_uncertain_success(self):
        api = GitHub()
        request = api.request
        lost = []
        def lose_response(path, body=None):
            result = request(path, body)
            if body and body["state"] == "success" and not lost:
                lost.append(True)
                raise persona.Unverified("api_unreadable")
            return result
        api.request = lose_response
        result, code = self.publisher(api).run()
        self.assertEqual((result["state"], code), ("error", 2))
        self.assertEqual([p["state"] for p in api.posts].count("success"), 1)
        self.assertEqual(api.statuses[0]["state"], "error")

    def test_unreadable_history_is_not_zero_and_dry_run_still_cannot_write(self):
        for dry in (False, True):
            api = GitHub()
            api.broken = lambda path, body: body is None and "/statuses?" in path
            result, code = self.publisher(api, dry=dry).run()
            self.assertEqual((result["state"], code), ("error", 2))
            self.assertEqual(bool(api.posts), not dry)

    def test_malformed_review_metadata_never_certifies_success(self):
        for item in ({**review(), "submitted_at": "bad-time"}, {**review(), "user": "malformed"},
                     {**review(), "state": "UNKNOWN"}):
            api = GitHub()
            api.reviews[1] = [item]
            result, code = self.publisher(api).run()
            self.assertEqual((result["state"], code), ("error", 2))
            self.assertNotIn("success", [p["state"] for p in api.posts])

    def test_nullable_deleted_review_author_cannot_approve_or_block_bot(self):
        api = GitHub()
        deleted = {**review(99, "CHANGES_REQUESTED"), "user": None}
        api.reviews[1] = [deleted, review()]
        self.assertEqual(self.publisher(api).run()[0]["state"], "success")
        api.reviews[1] = [deleted]
        self.assertEqual(self.publisher(api).run()[0]["state"], "pending")

    def test_malformed_readback_cannot_leave_reported_success(self):
        for fault in ("creator", "other_id", "id"):
            api = GitHub()
            request = api.request
            def corrupt(path, body=None):
                data = request(path, body)
                if "/statuses?" in path and body is None and api.posts and api.posts[-1]["state"] == "success":
                    if fault == "creator":
                        data[0]["creator"] = "invalid"
                    elif fault == "other_id":
                        data[0]["id"] += 100
                    else:
                        data[0]["id"] = 0
                return data
            api.request = corrupt
            result, code = self.publisher(api).run()
            self.assertEqual((result["state"], code), ("error", 2))
            self.assertEqual(api.statuses[0]["state"], "error")

    def test_http_happy_path_posts_only_persona_status_to_exact_head(self):
        fixture = GitHub()
        headers = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, data, status):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_GET(self):
                self.respond(fixture.request(self.path), 200)
            def do_POST(self):
                headers.append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.respond(fixture.request(self.path, body), 201)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            api = persona.API("fixture-token", base="http://127.0.0.1:" + str(server.server_port))
            result, code = self.publisher(api).run()
            self.assertEqual((result["state"], code), ("success", 0))
            self.assertEqual([p["state"] for p in fixture.posts], ["pending", "success"])
            self.assertEqual(headers, ["Bearer fixture-token", "Bearer fixture-token"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_strict_boolean_and_hint_validation_precede_api_use(self):
        for dry in ("", "TRUE", "False", "1", "invalid"):
            with self.assertRaises(persona.Unverified):
                persona.Target.parse(REPO, "1", HEAD, str(PUBLISHER), dry, "")
        with self.assertRaises(persona.Unverified):
            persona.Target.parse(REPO, "1", HEAD, str(PUBLISHER), "false", "APPROVED")
        for hint in ("", "running", "failed"):
            self.assertEqual(persona.Target.parse(REPO, "1", HEAD, str(PUBLISHER), "true", hint).dry_run, True)

    def test_cli_hints_never_decide_verdict_and_logs_contain_no_payload(self):
        for hint in ("running", "failed"):
            api = GitHub()
            with patch.dict(os.environ, {"PERSONA_TOKEN": "fixture-scoped-token", "GITHUB_OUTPUT": ""}), \
                 patch.object(persona, "API", return_value=api), contextlib.redirect_stdout(io.StringIO()) as output:
                code = persona.main(["--repository", REPO, "--pr", "1", "--head", HEAD,
                                     "--publisher-bot-id", str(PUBLISHER), "--hint", hint])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "success")
            self.assertNotIn("fixture-scoped-token", output.getvalue())
            self.assertNotIn("fixture-private-review-body", output.getvalue())

    def test_invalid_dry_run_never_constructs_api_or_writes_output(self):
        with patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), \
             patch.object(persona, "API", side_effect=AssertionError("API must not be called")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = persona.main(["--dry-run", "TRUE"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["reason"], "invalid_setup")

    def test_cli_rejects_dynamic_context_before_api_use(self):
        with patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), \
             patch.object(persona, "API", side_effect=AssertionError("API must not be called")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = persona.main(["--context", "gatekeeper / all-checks-passed"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["reason"], "invalid_setup")

    def test_all_creators_count_toward_cap_and_error_does_not_churn(self):
        api = GitHub()
        api.statuses = [{"id": i + 1, "context": persona.CONTEXT.upper(), "state": "pending",
                         "description": "old", "creator": {"id": 42, "type": "Bot"}} for i in range(950)]
        result, code = self.publisher(api).run()
        self.assertEqual((result["state"], code), ("error", 2))
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["description"], persona.CAP_DESCRIPTION)
        self.publisher(api).run()
        self.assertEqual(len(api.posts), 1)

    def test_other_context_page_overflow_is_error_not_zero_and_is_deduplicated(self):
        api = GitHub()
        api.statuses = [{"id": i + 1, "context": "other", "state": "success"} for i in range(200)]
        with patch.object(persona, "HISTORY_PAGES", 2):
            publisher = self.publisher(api)
            result, code = publisher.run()
            self.assertEqual((result["state"], code), ("error", 2))
            self.assertLessEqual(publisher.status_reads, 2)
            self.assertEqual(len(api.posts), 1)
            self.publisher(api).run()
            self.assertEqual(len(api.posts), 1)

    def test_serialized_refresh_orders_re_read_state(self):
        for order in ("old-then-new", "new-then-old"):
            api = GitHub()
            if order == "old-then-new":
                self.publisher(api).run()
            api.reviews[1] = [review(2, "CHANGES_REQUESTED")]
            self.publisher(api).run()
            if order == "new-then-old":
                self.publisher(api).run()  # delayed old intent, not old data
            self.assertEqual(api.statuses[0]["state"], "failure")

    def test_real_http_transport_and_permission_error_hide_response_body(self):
        fixture = GitHub()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = fixture.request(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_POST(self):
                self.assert_token = self.headers.get("Authorization")
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"fixture-secret-response-body")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            api = persona.API("fixture-token", base="http://127.0.0.1:" + str(server.server_port))
            result, code = self.publisher(api).run()
            self.assertEqual(code, 2)
            self.assertNotIn("fixture-token", json.dumps(result))
            self.assertNotIn("fixture-secret-response-body", json.dumps(result))
            self.assertFalse(result["status_verified"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_single_file_isolated_entrypoint_ignores_workspace_shadowing(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "json.py").write_text('raise RuntimeError("workspace json imported")\n')
            result = subprocess.run([sys.executable, "-I", str(SOURCE), "--help"], cwd=directory,
                                    env={**os.environ, "PYTHONPATH": directory}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("workspace json imported", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
