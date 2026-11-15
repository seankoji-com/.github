#!/usr/bin/env python3
"""One serialized persona writer. The trusted caller owns the per-head mutex."""

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

CONTEXT = "persona / grumpy-engineer"
REVIEWER_ID = 283599686
POLICY = "persona-status-v1"
PAGE_SIZE = 100
HISTORY_PAGES = 50  # At most 5,000 total statuses, including other contexts.
PR_PAGES = 20
REVIEW_PAGES = 20
CONTEXT_GUARD = 950  # Leaves room for pending, terminal and error before 1,000.
ERROR_DESCRIPTION = "persona-v1 error: could not verify current reviews"
CAP_DESCRIPTION = "persona-v1 error: status history budget exhausted"
SHA = re.compile(r"[0-9a-f]{40}")


class Unverified(Exception):
    """A fixed diagnostic code; never include response bodies or credentials."""


class Stale(Exception):
    pass


def positive(value):
    return type(value) is int and value > 0


def boolean(value):
    if value not in ("true", "false"):
        raise Unverified("invalid_boolean")
    return value == "true"


@dataclass(frozen=True)
class Target:
    repository: str
    pr: int
    head: str
    publisher: int
    dry_run: bool = False

    @classmethod
    def parse(cls, repository, pr, head, publisher, dry_run, hint):
        # Parse dry_run before constructing a publisher: invalid defaults never
        # accidentally enable writes. Hints are validated, but never evaluated.
        dry = boolean(dry_run)
        if (not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or not re.fullmatch(r"[1-9][0-9]{0,11}", pr or "")
                or not SHA.fullmatch(head or "")
                or not re.fullmatch(r"[1-9][0-9]{0,19}", publisher or "")
                or hint not in ("", "running", "failed")):
            raise Unverified("invalid_input")
        return cls(repository, int(pr), head, int(publisher), dry)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unverified("api_redirect")


class API:
    def __init__(self, token, *, base="https://api.github.com", budget=120):
        if not token:
            raise Unverified("missing_token")
        self._token = token
        self.base = base.rstrip("/")
        self.end = time.monotonic() + budget
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, body=None):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise Unverified("deadline")
        req = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "POST",
            headers={"Authorization": "Bearer " + self._token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with self.opener.open(req, timeout=min(10, remaining)) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise Unverified("api_payload_limit")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise Unverified("api_http_" + str(code)) from None
        except (OSError, ValueError, RecursionError):
            raise Unverified("api_unreadable") from None


def selected_review(reviews, head):
    choices = []
    for review in reviews:
        actor = review.get("user")
        if not isinstance(actor, dict):
            raise Unverified("invalid_review_actor")
        if actor.get("id") != REVIEWER_ID or actor.get("type") != "Bot":
            continue
        if review.get("commit_id") != head or review.get("state") == "PENDING":
            continue
        if not positive(review.get("id")) or not review.get("submitted_at"):
            raise Unverified("invalid_review_identity")
        try:
            stamp = datetime.fromisoformat(review["submitted_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
            order = stamp.timestamp(), review["id"]
        except (ValueError, TypeError, AttributeError):
            raise Unverified("invalid_review_time") from None
        state = review.get("state")
        if state not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
            raise Unverified("invalid_review_state")
        choices.append((order, review["id"], state))
    if not choices:
        return 0, "MISSING"
    _, ident, state = max(choices)
    return ident, state


def policy_digest():
    script = Path(__file__).read_bytes()
    manifest = Path(__file__).with_name("action.yml")
    return hashlib.sha256(script + b"\0" + manifest.read_bytes()).hexdigest()


class Publisher:
    def __init__(self, target, api, *, policy_hash=None):
        self.t = target
        self.api = api
        self.root = "/repos/" + target.repository
        self.policy_hash = policy_hash or policy_digest()
        self.bound = False
        self.latest = None
        self.count = 0
        self.changed = False
        self.status_reads = 0

    def status_request(self, path):
        # One shared budget across history, readback and error reconciliation.
        if self.status_reads >= HISTORY_PAGES:
            raise Unverified("status_history_budget")
        self.status_reads += 1
        return self.api.request(path)

    def pages(self, path, limit):
        seen = set()
        for page in range(1, limit + 1):
            data = self.api.request(path + ("&" if "?" in path else "?")
                                    + "per_page=100&page=" + str(page))
            if not isinstance(data, list) or len(data) > PAGE_SIZE:
                raise Unverified("invalid_page")
            for item in data:
                if not isinstance(item, dict) or not positive(item.get("id")) or item["id"] in seen:
                    raise Unverified("invalid_page_identity")
                seen.add(item["id"])
                yield item
            if len(data) < PAGE_SIZE:
                return
        raise Unverified("pagination_limit")

    def check_pr(self, pr):
        if (not isinstance(pr, dict) or not positive(pr.get("number"))
                or ((pr.get("base") or {}).get("repo") or {}).get("full_name") != self.t.repository
                or not isinstance(pr.get("head"), dict)
                or not isinstance(pr["head"].get("sha"), str)
                or not SHA.fullmatch(pr["head"]["sha"])
                or "merge_commit_sha" not in pr
                or (pr["merge_commit_sha"] is not None
                    and (not isinstance(pr["merge_commit_sha"], str) or not SHA.fullmatch(pr["merge_commit_sha"])))
                or pr.get("state") not in ("open", "closed")):
            raise Unverified("invalid_pr_identity")
        return pr

    def snapshot(self):
        target = self.check_pr(self.api.request(self.root + "/pulls/" + str(self.t.pr)))
        if target["number"] != self.t.pr:
            raise Unverified("invalid_pr_identity")
        if target["head"]["sha"] != self.t.head:
            raise Stale()
        self.bound = True
        current = []
        for item in self.pages(self.root + "/pulls?state=open", PR_PAGES):
            pr = self.check_pr(item)
            if pr["state"] != "open":
                raise Unverified("pr_roster_drift")
            current.append(pr)
        heads = {p["head"]["sha"] for p in current}
        if any((p["merge_commit_sha"] == self.t.head and p["head"]["sha"] != self.t.head)
               or (p["head"]["sha"] == self.t.head and p["merge_commit_sha"] in heads
                   and p["merge_commit_sha"] != self.t.head) for p in current):
            raise Unverified("head_merge_collision")
        roster = [p for p in current if p["head"]["sha"] == self.t.head]
        if not roster:
            if target["state"] == "closed":
                raise Stale()
            raise Unverified("pr_roster_drift")
        if target["state"] == "open" and not any(p["number"] == self.t.pr for p in roster):
            raise Unverified("pr_roster_drift")
        entries = []
        for pr in sorted(roster, key=lambda p: p["number"]):
            reviews = list(self.pages(self.root + "/pulls/" + str(pr["number"]) + "/reviews", REVIEW_PAGES))
            ident, state = selected_review(reviews, self.t.head)
            entries.append((pr["number"], self.t.head, ident, state))
        states = {entry[3] for entry in entries}
        state = ("failure" if "CHANGES_REQUESTED" in states else
                 "success" if states == {"APPROVED"} else "pending")
        payload = {"policy": POLICY, "action": self.policy_hash, "head": self.t.head, "prs": entries}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return state, digest

    def trusted(self, status):
        actor = (status or {}).get("creator") or {}
        return (isinstance(actor, dict) and positive(actor.get("id"))
                and actor.get("id") == self.t.publisher and actor.get("type") == "Bot")

    def history(self):
        self.latest, self.count = None, 0
        seen = set()
        for page in range(1, HISTORY_PAGES + 1):
            data = self.status_request(self.root + "/commits/" + self.t.head
                                       + "/statuses?per_page=100&page=" + str(page))
            if not isinstance(data, list) or len(data) > PAGE_SIZE:
                raise Unverified("invalid_status_page")
            for status in data:
                if (not isinstance(status, dict) or not positive(status.get("id"))
                        or status["id"] in seen or not isinstance(status.get("context"), str)):
                    raise Unverified("invalid_status_identity")
                seen.add(status["id"])
                if status["context"].casefold() != CONTEXT.casefold():
                    continue
                if status.get("state") not in ("pending", "success", "failure", "error"):
                    raise Unverified("invalid_status_state")
                self.count += 1  # Includes EVERY creator, not only our App.
                if self.latest is None or status["id"] > self.latest["id"]:
                    self.latest = status
            if len(data) < PAGE_SIZE:
                return self.latest
        raise Unverified("status_history_budget")

    def readback(self):
        # Combined statuses contain the latest record per context. They do not
        # replace the full, all-creator history count used for the cap guard.
        seen = set()
        for page in range(1, HISTORY_PAGES + 1):
            data = self.status_request(self.root + "/commits/" + self.t.head
                                       + "/status?per_page=100&page=" + str(page))
            if (not isinstance(data, dict) or data.get("sha") != self.t.head
                    or not isinstance(data.get("statuses"), list)
                    or len(data["statuses"]) > PAGE_SIZE):
                raise Unverified("invalid_status_readback")
            for status in data["statuses"]:
                if (not isinstance(status, dict) or not positive(status.get("id"))
                        or not isinstance(status.get("context"), str)
                        or status.get("state") not in ("pending", "success", "failure", "error")):
                    raise Unverified("invalid_status_readback")
                context = status["context"].casefold()
                if context in seen:
                    raise Unverified("invalid_status_readback")
                seen.add(context)
                if context == CONTEXT.casefold():
                    self.latest = status
                    return
            if len(data["statuses"]) < PAGE_SIZE:
                self.latest = None
                return

    def matches(self, state, description):
        return (self.trusted(self.latest) and self.latest.get("state") == state
                and self.latest.get("description") == description)

    def post(self, state, description):
        if self.t.dry_run:
            return
        if self.count >= CONTEXT_GUARD and state != "error":
            raise Unverified("status_cap")
        body = {"context": CONTEXT, "state": state, "description": description,
                "target_url": "https://github.com/" + self.t.repository + "/pull/" + str(self.t.pr)}
        status = self.api.request(self.root + "/statuses/" + self.t.head, body)
        self.changed = True
        self.count += 1
        if (not isinstance(status, dict) or not positive(status.get("id"))
                or status.get("context", "").casefold() != CONTEXT.casefold()
                or status.get("state") != state or status.get("description") != description
                or not self.trusted(status)):
            raise Unverified("publisher_identity_or_write_mismatch")
        self.latest = status

    def outcome(self, state="", certificate="", *, evaluated=False, stale=False, reason="", verified=False):
        return {"head_sha": self.t.head, "state": state, "certificate": certificate,
                "status_id": (self.latest or {}).get("id", ""), "changed": self.changed,
                "bound": self.bound, "evaluated": evaluated, "stale": stale,
                "dry_run": self.t.dry_run, "reason": reason, "status_verified": verified}

    def failure(self, reason):
        description = CAP_DESCRIPTION if reason in ("status_cap", "status_history_budget") else ERROR_DESCRIPTION
        verified = False
        # Never guess a successor or claim revocation when initial binding failed.
        if self.bound and not self.t.dry_run:
            try:
                self.readback()
            except Unverified:
                pass  # Cached newest error may still deduplicate an overflow.
            try:
                if not self.matches("error", description):
                    self.post("error", description)
                verified = self.matches("error", description)
            except Unverified:
                pass
        return self.outcome("error", reason=reason, verified=verified), 2

    def run(self):
        try:
            for _ in range(3):
                first = self.snapshot()
                if first != self.snapshot():
                    continue
                state, digest = first
                description = "persona-v1 " + state + " " + digest
                self.history()
                if first != self.snapshot():
                    continue
                if self.count >= CONTEXT_GUARD:
                    raise Unverified("status_cap")
                if not self.t.dry_run and self.matches(state, description):
                    return self.outcome(state, digest, evaluated=True, verified=True), 0
                if self.t.dry_run:
                    return self.outcome(state, digest, evaluated=True), 0
                # A pending probe authenticates the returned creator BEFORE any
                # new success. No redundant pending before an already-blocking state.
                if state == "success":
                    self.post("pending", "persona-v1 verifying " + digest)
                if first != self.snapshot():
                    continue
                if self.status_reads >= HISTORY_PAGES:
                    raise Unverified("status_history_budget")
                self.post(state, description)
                written_id = self.latest["id"]
                self.readback()
                if not self.matches(state, description) or self.latest["id"] != written_id:
                    raise Unverified("status_readback_mismatch")
                if first != self.snapshot():
                    self.post("pending", "persona-v1 reviews changed")
                    continue
                return self.outcome(state, digest, evaluated=True, verified=True), 0
            raise Unverified("snapshot_drift")
        except Stale:
            # If an intent moves during evaluation, invalidate only its old,
            # already-bound head. Never publish to the successor head.
            if self.changed and self.bound:
                return self.failure("head_changed")
            return self.outcome(stale=True, reason="stale_intent"), 0
        except (Unverified, TypeError, KeyError, AttributeError, ValueError, RecursionError) as error:
            return self.failure(str(error) if isinstance(error, Unverified) else "invalid_payload")


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise Unverified("invalid_arguments")


def main(argv=None):
    parser = Arguments(description=__doc__)
    for flag, env in (("repository", "TARGET_REPOSITORY"), ("pr", "PR"), ("head", "HEAD_SHA"),
                      ("publisher-bot-id", "PUBLISHER_BOT_ID"), ("dry-run", "DRY_RUN"), ("hint", "HINT")):
        parser.add_argument("--" + flag, default=os.environ.get("PERSONA_" + env, "false" if flag == "dry-run" else ""))
    try:
        args = parser.parse_args(argv)
        target = Target.parse(args.repository, args.pr, args.head, args.publisher_bot_id, args.dry_run, args.hint)
        result, code = Publisher(target, API(os.environ.get("PERSONA_TOKEN", ""))).run()
    except (Unverified, OSError, ValueError):
        result, code = {"state": "error", "bound": False, "evaluated": False,
                        "status_verified": False, "reason": "invalid_setup"}, 2
    output = json.dumps(result, sort_keys=True, separators=(",", ":"))
    print(output)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("result=" + output + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
