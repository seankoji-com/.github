#!/usr/bin/env python3
"""Aggregator gate: one check run that blocks on ANY red check.

Each repo once listed its own required checks, a hand-maintained subset of what
actually runs. That design lets an unlisted failing check be ignored. This
posts the single check run, `gatekeeper / all-checks-passed`, that the org
`PR gatekeeper` ruleset requires everywhere.

The decision lives in `evaluate()`, which is pure: it takes the three API
payloads and returns the check-run fields to post. All I/O is in `main()`, so
the tests need no network.

Three payloads are read, and each is load-bearing:

  * `/commits/{sha}/check-runs?filter=latest` — the checks themselves. Also
    de-duplicated here by `(app.id, name)`: without that, "Re-run failed jobs"
    leaves the stale `failure` sitting beside the new `success` and the gate is
    pinned red forever.
  * `/commits/{sha}/status` — commit statuses (Vercel, Codecov and friends, if
    they ever appear). GitHub reports `state: "pending"` on an EMPTY response,
    so `total_count == 0` means "nothing", never "pending".
  * `/commits/{sha}/check-suites` — quiescence only, and only for suites with
    `latest_check_runs_count > 0`. Some app suites (`vercel`, `claude`) sit at
    `queued` with zero runs permanently; requiring every suite to be completed
    would block every PR in the org forever.

`conclusion` is None unless `status == "completed"`. `in_progress` is a check
run *status*; supplying any conclusion forces `status: completed`, so
`conclusion: "in_progress"` is a 422 on every still-running PR.

Usage:
    pr-gatekeeper.py --repo owner/name --sha <head sha> [--dry-run]

Reads the token from $GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

# The check-run name, verbatim. The org ruleset matches this string exactly;
# any variation here is an unsatisfiable required context on 33 repos.
GATE_CHECK_NAME = "gatekeeper / all-checks-passed"
PERSONA_USER_ID = 283599686  # bot-grumpy-engineer[bot]
PERSONA_CHECK_NAME = "persona / grumpy-engineer"

# Allowlist, deliberately. A denylist of failure values would let a conclusion
# GitHub adds tomorrow pass silently; anything unrecognised must block.
PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral", "stale"})

# Commit-status states that pass / are still running. Everything else blocks.
PASSING_STATUS_STATES = frozenset({"success"})
PENDING_STATUS_STATES = frozenset({"pending"})


def _latest_key(run: dict) -> tuple:
    """Ordering used to pick the surviving run of a duplicated (app, name).

    `filter=latest` is meant to do this server-side, but it is pinned rather
    than relied on: the re-run case is the one that pins the gate red.
    """
    return (
        run.get("started_at") or "",
        run.get("completed_at") or "",
        run.get("id") or 0,
    )


def dedupe_check_runs(check_runs: list[dict]) -> list[dict]:
    """Drop the gate's own run, then keep one run per (app.id, name)."""
    latest: dict[tuple, dict] = {}
    for run in check_runs:
        name = run.get("name") or ""
        if name == GATE_CHECK_NAME:
            continue
        app = run.get("app") or {}
        key = (app.get("id"), (run.get("check_suite") or {}).get("id"), name)
        current = latest.get(key)
        if current is None or _latest_key(run) >= _latest_key(current):
            latest[key] = run
    return sorted(latest.values(), key=lambda r: (r.get("name") or "", r.get("id") or 0))



def evaluate(
    check_runs: list[dict],
    statuses: dict | None,
    suites: list[dict],
) -> tuple[str, str | None, str, str]:
    """Return (status, conclusion, title, summary) for the gate check run.

    Pure. `conclusion` is None whenever `status != "completed"`.
    """
    failing: list[str] = []
    pending: list[str] = []
    gate_suite_ids = {
        suite.get("id")
        for run in check_runs
        if run.get("name") == GATE_CHECK_NAME
        for suite in [run.get("check_suite")]
        if isinstance(suite, dict) and suite.get("id") is not None
    }

    for run in dedupe_check_runs(check_runs):
        name = run.get("name") or "(unnamed check)"
        if run.get("status") != "completed":
            pending.append(f"{name} ({run.get('status') or 'unknown'})")
            continue
        conclusion = run.get("conclusion")
        if conclusion not in PASSING_CONCLUSIONS:
            failing.append(f"{name} ({conclusion or 'no conclusion'})")

    # Commit statuses. An empty response reports state "pending" — read the
    # count, not the state.
    statuses = statuses or {}
    if int(statuses.get("total_count") or 0) > 0:
        for status in statuses.get("statuses") or []:
            context = status.get("context") or "(unnamed status)"
            state = status.get("state")
            if state in PASSING_STATUS_STATES:
                continue
            if state in PENDING_STATUS_STATES:
                pending.append(f"{context} (pending)")
            else:
                failing.append(f"{context} ({state or 'no state'})")

    # Quiescence: a suite that has produced no check runs is not a suite that
    # is going to. Only suites with runs outstanding hold the gate open. The
    # gate's own Actions suite is excluded: otherwise it waits for itself.
    for suite in suites or []:
        if suite.get("id") in gate_suite_ids:
            continue
        if int(suite.get("latest_check_runs_count") or 0) <= 0:
            continue
        if suite.get("status") != "completed":
            app = (suite.get("app") or {}).get("slug") or "unknown app"
            pending.append(f"check suite {suite.get('id')} ({app}) not finished")

    if failing:
        title = f"{len(failing)} check{'s' if len(failing) != 1 else ''} failing"
        summary = "These checks are not passing:\n\n" + "\n".join(
            f"- {item}" for item in failing
        )
        if pending:
            summary += "\n\nStill running:\n\n" + "\n".join(
                f"- {item}" for item in pending
            )
        return "completed", "failure", title, summary

    if pending:
        title = f"Waiting on {len(pending)} check{'s' if len(pending) != 1 else ''}"
        summary = "Still running:\n\n" + "\n".join(f"- {item}" for item in pending)
        return "in_progress", None, title, summary

    return (
        "completed",
        "success",
        "All checks passed",
        "Every check run and commit status on this commit passed, was skipped, "
        "or was neutral/stale.",
    )


def _request_json(req: urllib.request.Request) -> object:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or exc.code == 429 or (
                exc.code == 403 and (exc.headers.get("Retry-After") or exc.headers.get("X-RateLimit-Remaining") == "0")
            )
            exc.close()
            if not retryable or attempt == 2:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("request retry exhausted")


def _get(path: str, token: str) -> object:
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "seankoji-pr-gatekeeper",
        },
    )
    try:
        return _request_json(req)
    except urllib.error.HTTPError as exc:
        exc.request_path = path
        raise


def _post(path: str, token: str, body: dict) -> object:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "seankoji-pr-gatekeeper",
        },
    )
    return _request_json(req)


def _batch(payload: object, key: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ValueError(f"invalid {key} response")
    batch = payload[key]
    if not all(isinstance(item, dict) for item in batch):
        raise ValueError(f"invalid {key} item")
    return batch


def _collect_for_ref(repo: str, ref: str, token: str) -> tuple[list, dict, list]:
    """Fetch the three payloads for a commit SHA or URL-encoded branch ref."""
    runs: list = []
    page = 1
    while True:
        payload = _get(
            f"/repos/{repo}/commits/{ref}/check-runs"
            f"?filter=latest&per_page=100&page={page}",
            token,
        )
        batch = _batch(payload, "check_runs")
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    statuses = None
    page = 1
    while True:
        payload = _get(f"/repos/{repo}/commits/{ref}/status?per_page=100&page={page}", token)
        batch = _batch(payload, "statuses")
        if not isinstance(payload.get("total_count"), int) or payload["total_count"] < 0:
            raise ValueError("invalid status count")
        if statuses is None:
            statuses = dict(payload, statuses=[])
        statuses["statuses"].extend(batch)
        if len(batch) < 100:
            break
        page += 1

    suites: list = []
    page = 1
    while True:
        payload = _get(
            f"/repos/{repo}/commits/{ref}/check-suites?per_page=100&page={page}", token
        )
        batch = _batch(payload, "check_suites")
        suites.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return runs, statuses, suites


def _open_pr_ref(repo: str, sha: str, token: str) -> str | None:
    """Return the URL-encoded branch ref for an open PR at `sha`, if any."""
    page = 1
    while True:
        prs = _get(f"/repos/{repo}/pulls?state=open&per_page=100&page={page}", token)
        if not isinstance(prs, list):
            raise ValueError("invalid open PR response")
        for pr in prs:
            head = pr.get("head") if isinstance(pr, dict) else None
            if not isinstance(head, dict):
                raise ValueError("invalid open PR head")
            if head.get("sha") == sha and isinstance(head.get("ref"), str):
                return urllib.parse.quote(head["ref"], safe="")
        if len(prs) < 100:
            return None
        page += 1


def collect(repo: str, sha: str, token: str) -> tuple[list, dict, list]:
    """Fetch the three payloads, falling back to the matching open PR ref.

    GitHub occasionally rejects a head SHA on the checks endpoints while it
    accepts its branch name. The check run remains attached to the exact SHA.
    """
    try:
        return _collect_for_ref(repo, sha, token)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 422):
            raise
        missing_ref_error = exc
    ref = _open_pr_ref(repo, sha, token)
    if ref is None:
        raise missing_ref_error
    return _collect_for_ref(repo, ref, token)


def persona_verdict(reviews: list[dict], sha: str) -> tuple[str, str | None]:
    """Only a submitted verdict from Grumpy on this exact head can satisfy us."""
    matching = [r for r in reviews
                if (r.get("user") or {}).get("id") == PERSONA_USER_ID
                and (r.get("user") or {}).get("type") == "Bot"
                and r.get("commit_id") == sha and r.get("submitted_at")
                and r.get("state") != "PENDING"]
    latest = max(matching, key=lambda r: (r["submitted_at"], r["id"]), default={})
    if latest.get("state") == "APPROVED":
        return "completed", "success"
    if latest.get("state") == "CHANGES_REQUESTED":
        return "completed", "failure"
    return "in_progress", None


def persona_marker(number: int, verdict: tuple[str, str | None]) -> str:
    return f"<!-- grumpy-review:{number}:{verdict[0]}:{verdict[1] or 'waiting'} -->"


def _list_all(path: str, token: str) -> list[dict]:
    items, page = [], 1
    separator = "&" if "?" in path else "?"
    while True:
        batch = _get(f"{path}{separator}per_page=100&page={page}", token)
        if not isinstance(batch, list) or not all(isinstance(i, dict) for i in batch):
            raise ValueError("invalid paginated list response")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def matching_prs(prs: list[dict], sha: str) -> list[dict]:
    """A shared head gate must enforce every PR that can use that gate."""
    heads = {pr["head"]["sha"] for pr in prs
             if sha in (pr["head"]["sha"], pr.get("merge_commit_sha"))}
    return [pr for pr in prs if pr["head"]["sha"] in heads]


def collect_persona_checks(repo: str, sha: str, token: str, prs: list[dict] | None = None) -> list[dict]:
    """Central Actions runs are invisible on target commits; read actual reviews.

    No draft, fork, author or skip-review exemption. Every open PR sharing the
    commit must have its own review. Non-PR workflow commits keep normal gating.
    API errors propagate to report(), which posts a blocking diagnostic.
    """
    checks = []
    if prs is None:
        prs = _list_all(f"/repos/{repo}/pulls?state=open", token)
    for pr in matching_prs(prs, sha):
        reviews = _list_all(f"/repos/{repo}/pulls/{pr['number']}/reviews", token)
        status, conclusion = persona_verdict(reviews, pr["head"]["sha"])
        checks.append({"name": f"{PERSONA_CHECK_NAME} (PR #{pr['number']})",
                       "status": status, "conclusion": conclusion,
                       "marker": persona_marker(pr['number'], (status, conclusion)),
                       "head_sha": pr["head"]["sha"], "merge_sha": pr.get("merge_commit_sha")})
    return checks


def resolve_review_heads(repo: str, run_id: int, token: str) -> list[str]:
    """Resolve fork review signals using trusted GitHub run and PR metadata."""
    run = _get(f"/repos/{repo}/actions/runs/{run_id}", token)
    if run.get("event") != "pull_request_review":
        raise ValueError("not a review event workflow run")
    linked = {p["number"] for p in run.get("pull_requests", [])}
    heads = set()
    for pr in _list_all(f"/repos/{repo}/pulls?state=open", token):
        head = pr["head"]
        same_branch = ((run.get("head_repository") or {}).get("id") is not None
                       and (head.get("repo") or {}).get("id") == run["head_repository"]["id"]
                       and head.get("ref") == run.get("head_branch"))
        same_commit = bool(run.get("head_sha")) and run["head_sha"] in (head["sha"], pr.get("merge_commit_sha"))
        if pr["number"] in linked or same_branch or same_commit:
            heads.add(head["sha"])
    if not heads:
        raise ValueError("could not associate review event with an open PR")
    return sorted(heads)


def previous_publication_refs(repo: str, sha: str, token: str) -> set[str]:
    """Recover destinations from our last gate if PR metadata is unavailable."""
    payload = _get(f"/repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100", token)
    gates = [c for c in _batch(payload, "check_runs")
             if c.get("name") == GATE_CHECK_NAME and (c.get("app") or {}).get("id") == 15368]
    latest = max(gates, key=lambda c: c["id"], default={})
    summary = (latest.get("output") or {}).get("summary", "")
    match = re.search(r"<!-- gatekeeper-refs:(\[.*?\]) -->", summary)
    if not match:
        return set()
    refs = json.loads(match[1])
    if not isinstance(refs, list) or not all(isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs):
        raise ValueError("invalid previous gate destinations")
    return set(refs)


def report(repo: str, sha: str, token: str, dry_run: bool = False, error: str | None = None) -> int:
    publish_refs = {sha}
    result = 0
    try:
        if error:
            raise ValueError(error)
        persona_checks = []
        # Ship disabled, validate the fleet, then enable the org Actions variable.
        # An absent review must hold the existing required gate pending.
        if os.environ.get("PERSONA_REVIEW_REQUIRED") == "true":
            prs = matching_prs(_list_all(f"/repos/{repo}/pulls?state=open", token), sha)
            # Resolve both destinations before reading reviews, so an API
            # failure also replaces any earlier green merge gate.
            publish_refs.update(ref for pr in prs
                                for ref in (pr["head"]["sha"], pr.get("merge_commit_sha")) if ref)
            persona_checks = collect_persona_checks(repo, sha, token, prs)
            # GitHub may prefer checks on its synthetic merge commit. Keep
            # both refs current so a dismissed review cannot leave one green.
            publish_refs.update(c[key] for c in persona_checks
                                for key in ("head_sha", "merge_sha") if c.get(key))
        runs, statuses, suites = collect(repo, sha, token)
        runs = runs + persona_checks
        extra_summaries = []
        for ref in sorted(publish_refs - {sha}):
            other_status, other_conclusion, _, other_summary = evaluate(*collect(repo, ref, token))
            runs = runs + [{"name": f"Checks on {ref}", "status": other_status,
                            "conclusion": other_conclusion}]
            extra_summaries.append(other_summary)
        status, conclusion, title, summary = evaluate(runs, statuses, suites)
        if extra_summaries:
            summary += "\n\n" + "\n\n".join(extra_summaries)
        if persona_checks:
            summary += "\n\nGrumpy Engineer review of the current PR head is required.\n"
            summary += "\n".join(c["marker"] for c in persona_checks)
    except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        # Recover independently of the failed PR-list/review-run request.
        # Every published verdict records its destinations for this purpose.
        result = 2
        try:
            publish_refs.update(previous_publication_refs(repo, sha, token))
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError, KeyError):
            print("::error::could not recover all previous gate destinations; retry required", file=sys.stderr)
        # The job lives on main, so put read/shape failures on the PR too.
        status, conclusion = "completed", "failure"
        title = "Could not read check state"
        summary = f"Gate evaluation failed: {type(exc).__name__}. See the gatekeeper job log and retry."
        path = getattr(exc, "request_path", "unknown endpoint")
        print(f"::error::{title} at {path}: {exc}", file=sys.stderr)
    summary += "\n\n<!-- gatekeeper-refs:" + json.dumps(sorted(publish_refs)) + " -->"
    print(f"{status} / {conclusion or '-'}: {title}")
    if dry_run:
        return 0
    body = {"name": GATE_CHECK_NAME, "head_sha": sha, "status": status,
            "output": {"title": title, "summary": summary}}
    if status == "completed":
        body["conclusion"] = conclusion
    for ref in sorted(publish_refs):
        try:
            _post(f"/repos/{repo}/check-runs", token, dict(body, head_sha=ref))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"::error::could not post the gate check run for {ref}: {exc}", file=sys.stderr)
            result = 2
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sha", help="head commit SHA")
    target.add_argument("--reconcile-open", action="store_true", help="refresh every open PR in this repository")
    parser.add_argument("--review-run", type=int, help="resolve the current PR head from a review signal run")
    parser.add_argument("--dry-run", action="store_true", help="evaluate without posting")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("::error::GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    if args.review_run:
        if not args.sha:
            parser.error("--review-run requires --sha as its blocking fallback")
        try:
            heads = resolve_review_heads(args.repo, args.review_run, token)
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            return report(args.repo, args.sha, token, args.dry_run, error=str(exc))
        return max(report(args.repo, head, token, args.dry_run) for head in heads)
    if not args.reconcile_open:
        return report(args.repo, args.sha, token, args.dry_run)
    page, result = 1, 0
    try:
        while True:
            prs = _get(f"/repos/{args.repo}/pulls?state=open&per_page=100&page={page}", token)
            if not isinstance(prs, list):
                raise ValueError("invalid open PR response")
            for pr in prs:
                result = max(result, report(args.repo, pr["head"]["sha"], token, args.dry_run))
            if len(prs) < 100:
                return result
            page += 1
    except (urllib.error.URLError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"::error::could not reconcile open PRs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
