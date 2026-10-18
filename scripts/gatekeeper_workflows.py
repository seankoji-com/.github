"""Workflow evidence collection and gate report formatting.

Split out of `pr-gatekeeper.py` so the aggregator's decision logic and this
supporting logic each stay under the size budget. The two HTTP seams it needs
(`get`, `batch_fn`) are injected by the aggregator, which keeps ownership of
retry, authentication and monkeypatching; nothing here reads the network on its
own or imports the aggregator back.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
import re
import time
import urllib.parse

# The gate's own caller workflow. Its run must never be inventoried or required.
GATE_WORKFLOW_PATH = ".github/workflows/call-reusable-pr-gatekeeper.yml"
EVALUATED_MARKER = "<!-- gatekeeper-evaluated -->"


class WorkflowInventoryDrift(ValueError):
    """A paginated run inventory changed or was incomplete during the read."""


def collect_workflow_checks(get, batch_fn, repo: str, sha: str, token: str,
                            inventory: dict | None = None) -> tuple[list[dict], set[int]]:
    for attempt in range(3):
        try:
            return _collect_workflow_checks(get, batch_fn, repo, sha, token, inventory)
        except WorkflowInventoryDrift:
            if attempt == 2:
                raise
            time.sleep(1)


def _collect_workflow_checks(get, batch_fn, repo: str, sha: str, token: str,
                             inventory: dict | None = None) -> tuple[list[dict], set[int]]:
    """Include registered workflows before they create their first check run."""
    latest = {}
    known_suites = set()
    seen_runs = set()
    expected_count = None
    page = 1
    while True:
        payload = get(f"/repos/{repo}/actions/runs?head_sha={urllib.parse.quote(sha, safe='')}&per_page=100&page={page}", token)
        batch = batch_fn(payload, "workflow_runs")
        count = payload.get("total_count")
        if not isinstance(count, int) or count < 0 or count > 1000:
            raise ValueError("invalid or truncated workflow run inventory")
        if expected_count is not None and count != expected_count:
            raise WorkflowInventoryDrift("workflow run inventory changed during pagination; retry")
        expected_count = count
        for run in batch:
            if not run.get("id"):
                raise ValueError("missing workflow run identity")
            if run["id"] in seen_runs:
                raise WorkflowInventoryDrift("duplicate workflow run identity; retry")
            seen_runs.add(run["id"])
            if not run.get("head_sha"):
                raise ValueError("workflow run missing head SHA")
            if run["head_sha"] != sha:
                continue
            path = run.get("path")
            if not isinstance(path, str) or not path or not run.get("workflow_id") or not run.get("id"):
                raise ValueError("workflow run missing identity")
            if (str(run["id"]) == os.environ.get("GITHUB_RUN_ID")
                    or path.split("@", 1)[0] == GATE_WORKFLOW_PATH):
                continue
            if isinstance(run.get("check_suite_id"), int):
                known_suites.add(run["check_suite_id"])
            key = (run["workflow_id"], run.get("event"), run.get("head_branch"))
            order = (run["id"], run.get("run_attempt") or 1)
            if key not in latest or order > latest[key][0]:
                latest[key] = (order, run)
        if len(batch) < 100:
            if len(seen_runs) != expected_count:
                raise WorkflowInventoryDrift("incomplete workflow run inventory; retry")
            break
        page += 1
    selected_suites = {run.get("check_suite_id") for _, run in latest.values()}
    if inventory is not None:
        inventory[sha] = [run for _, run in latest.values()]
    def label(value):
        return " ".join(str(value).split())[:100]

    checks = [{"id": run["id"],
             "name": (f"Workflow {label(run['path'])} ({label(key[1])}, {label(key[2])}); run {run['id']}. "
                      "Resolve pending execution/approval or rerun unsuccessful runs"),
             "html_url": f"https://github.com/{repo}/actions/runs/{run['id']}",
             "status": run.get("status"), "conclusion": run.get("conclusion")}
            for key, (_, run) in latest.items()]
    return checks, known_suites - selected_suites


def _check_runs(get, batch_fn, repo, ref, token):
    runs: list = []
    page = 1
    while True:
        payload = get(
            f"/repos/{repo}/commits/{ref}/check-runs"
            f"?filter=latest&per_page=100&page={page}",
            token,
        )
        batch = batch_fn(payload, "check_runs")
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return runs


def _statuses(get, batch_fn, repo, ref, token):
    statuses = None
    page = 1
    while True:
        payload = get(f"/repos/{repo}/commits/{ref}/status?per_page=100&page={page}", token)
        batch = batch_fn(payload, "statuses")
        if not isinstance(payload.get("total_count"), int) or payload["total_count"] < 0:
            raise ValueError("invalid status count")
        if statuses is None:
            statuses = dict(payload, statuses=[])
        statuses["statuses"].extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return statuses


def _suites(get, batch_fn, repo, ref, token):
    suites: list = []
    page = 1
    while True:
        payload = get(
            f"/repos/{repo}/commits/{ref}/check-suites?per_page=100&page={page}", token
        )
        batch = batch_fn(payload, "check_suites")
        suites.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return suites


def collect_for_ref(get, batch_fn, repo: str, ref: str, token: str, *,
                    workflow_sha: str | None = None,
                    inventory: dict | None = None) -> tuple[list, dict, list]:
    """Fetch the three payloads for a commit SHA or URL-encoded branch ref."""
    # Each inventory retains its own pagination, retries and validation. Reads
    # share no cache and all four must succeed before any verdict is published.
    # Workflow inventory stays LAST: a rerun registered during check reads must
    # still hold the gate pending even before it has any check runs or suites.
    # Keep reports/publications serialized: concurrent heads can share refs.
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks_task = pool.submit(_check_runs, get, batch_fn, repo, ref, token)
        status_task = pool.submit(_statuses, get, batch_fn, repo, ref, token)
        suites_task = pool.submit(_suites, get, batch_fn, repo, ref, token)
        runs, statuses, suites = checks_task.result(), status_task.result(), suites_task.result()
    workflow_checks, superseded_suites = collect_workflow_checks(
        get, batch_fn, repo, workflow_sha or ref, token, inventory)
    # A new workflow run on the same SHA can replace a canceled older suite.
    # Keep suites from distinct workflows, even when their job names match.
    runs = [run for run in runs if (run.get("check_suite") or {}).get("id") not in superseded_suites]
    suites = [suite for suite in suites if suite.get("id") not in superseded_suites]
    runs.extend(workflow_checks)
    return runs, statuses, suites


def open_pr_ref(get, repo: str, sha: str, token: str) -> str | None:
    """Return the URL-encoded branch ref for an open PR at `sha`, if any."""
    page = 1
    while True:
        prs = get(f"/repos/{repo}/pulls?state=open&per_page=100&page={page}", token)
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


def parse_required_workflows(value: str) -> list[dict]:
    """Caller-owned JSON, never workflow discovery or shell source.

    Each entry is {"path": ".github/workflows/ci.yml", "event": "pull_request",
    "ref": "head"}. ref binds Actions REST head_sha, not a job's checkout:
    PR runs commonly report the source head even when checkout tests the merge.
    Use merge only for runs actually indexed by the current merge SHA.
    """
    if len(value) > 16384:
        raise ValueError("required_workflows exceeds input limit")
    entries = json.loads(value or "[]")
    if not isinstance(entries, list) or len(entries) > 50:
        raise ValueError("required_workflows must be a JSON array of at most 50 entries")
    seen = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"path", "event", "ref"}
                or not all(isinstance(v, str) for v in entry.values())
                or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", entry["path"])
                or entry["path"] == GATE_WORKFLOW_PATH
                or entry["event"] not in {"pull_request", "push", "workflow_dispatch"}
                or entry["ref"] not in {"head", "merge"}):
            raise ValueError("invalid required_workflows entry: expected path, event, and head/merge ref")
        key = (entry["path"], entry["event"], entry["ref"])
        if key in seen:
            raise ValueError("duplicate required_workflows entry")
        seen.add(key)
    return entries


def _required_run_matches(run: dict, entry: dict, pr: dict, sha: str, prs: list[dict]) -> bool:
    """Bind evidence to this PR/ref, including fork runs with no PR array."""
    head = pr["head"]
    if (run.get("head_sha") != sha or run.get("event") != entry["event"]
            or run.get("head_branch") != head.get("ref")):
        return False
    repository = run.get("head_repository") or {}
    if not head.get("repo", {}).get("id") or repository.get("id") != head["repo"]["id"]:
        return False
    linked = run.get("pull_requests")
    if not isinstance(linked, list):
        return False
    if linked:
        return any(p.get("number") == pr.get("number")
                   and (p.get("head") or {}).get("sha") == head["sha"] for p in linked)
    # Empty fork associations must identify only one current PR. A shared
    # branch/head targeting different bases cannot borrow another PR's run.
    return sum(p["head"].get("ref") == head.get("ref")
               and (p["head"].get("repo") or {}).get("id") == repository["id"]
               and (p["head"]["sha"] if entry["ref"] == "head" else p.get("merge_commit_sha")) == sha
               for p in prs) == 1


def _attempt_jobs(get, batch_fn, repo: str, run: dict, token: str) -> list[dict]:
    """Read only this attempt, with complete pagination and identity checks."""
    jobs, seen, expected, page = [], set(), None, 1
    while True:
        payload = get(f"/repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
                       f"?per_page=100&page={page}", token)
        batch = batch_fn(payload, "jobs")
        count = payload.get("total_count")
        if (type(count) is not int or count < 0
                or (expected is not None and count != expected)):
            raise ValueError("invalid or changed required workflow jobs inventory")
        expected = count
        for job in batch:
            if (not job.get("id") or job["id"] in seen or job.get("run_id") != run["id"]
                    or job.get("run_attempt") != run["run_attempt"]
                    or job.get("head_sha") != run["head_sha"]):
                raise ValueError("required workflow job identity/ref/attempt mismatch")
            seen.add(job["id"])
            jobs.append(job)
        if len(batch) < 100:
            if len(jobs) != expected:
                raise ValueError("incomplete required workflow jobs inventory")
            return jobs
        page += 1


def required_workflow_checks(get, batch_fn, repo: str, entries: list[dict], prs: list[dict], inventory: dict,
                              token: str, metadata: dict) -> list[dict]:
    """Require every declared workflow on every PR sharing the published head."""
    checks, verified = [], {}
    for entry in entries:
        path = entry["path"]
        if path not in metadata:
            metadata[path] = get(f"/repos/{repo}/actions/workflows/{urllib.parse.quote(path.rsplit('/', 1)[1], safe='')}", token)
        workflow = metadata[path]
        if (type(workflow.get("id")) is not int or workflow["id"] <= 0
                or workflow.get("path") != path or workflow.get("state") != "active"):
            raise ValueError("required workflow metadata does not identify an active matching path")
        for pr in prs or [{}]:
            name = f"Required workflow `{path}` ({entry['event']}, {entry['ref']}, PR {pr.get('number', '?')})"
            sha = ((pr.get("head") or {}).get("sha") if entry["ref"] == "head"
                   else pr.get("merge_commit_sha"))
            candidates = [r for r in inventory.get(sha, [])
                          if r.get("workflow_id") == workflow["id"]
                          and r.get("path", "").split("@", 1)[0] == path
                          and _required_run_matches(r, entry, pr, sha, prs)] if sha else []
            if not candidates:
                checks.append({"name": name + ": missing current-ref run", "status": "queued"})
                continue
            selected = max(candidates, key=lambda r: (r["id"], r.get("run_attempt") or 0))
            key = (selected["id"], selected.get("run_attempt"))
            if key not in verified:
                current = get(f"/repos/{repo}/actions/runs/{selected['id']}", token)
                if (any(current.get(k) != selected.get(k) for k in
                        ("id", "workflow_id", "path", "head_sha", "event", "head_branch"))
                        or type(current.get("run_attempt")) is not int or current["run_attempt"] < 1
                        or current["run_attempt"] < (selected.get("run_attempt") or 1)):
                    raise ValueError("required workflow run identity/ref/attempt changed")
                jobs = _attempt_jobs(get, batch_fn, repo, current, token) if current.get("status") == "completed" and current.get("conclusion") == "success" else []
                # A rerun begun while jobs were read must not inherit success.
                if jobs:
                    after = get(f"/repos/{repo}/actions/runs/{selected['id']}", token)
                    if after != current:
                        raise ValueError("required workflow run changed while reading jobs; retry")
                verified[key] = current, jobs
            current, jobs = verified[key]
            if not _required_run_matches(current, entry, pr, sha, prs):
                raise ValueError("required workflow PR association changed")
            status, conclusion = current.get("status"), current.get("conclusion")
            if status == "completed" and conclusion == "success":
                if not jobs:
                    status, conclusion = "queued", None
                    name += ": empty latest attempt"
                elif any(j.get("status") != "completed" for j in jobs):
                    status, conclusion = "queued", None
                    name += ": latest-attempt jobs pending"
                elif any(j.get("conclusion") not in {"success", "skipped"} for j in jobs):
                    conclusion = "failure"
                    name += ": latest-attempt jobs unsuccessful"
                elif not any(j.get("conclusion") == "success" for j in jobs):
                    status, conclusion = "queued", None
                    name += ": no successful jobs in latest attempt"
            elif status == "completed" and conclusion != "success":
                name += f": {conclusion or 'no conclusion'}"
                conclusion = "failure"
            checks.append({"name": name, "status": status, "conclusion": conclusion})
    return checks


def safe_text(value):
    """Render event data as text, never Markdown/HTML or hidden state markers."""
    value = " ".join(str(value).split())[:1000]
    return html.escape(value).replace("[", "&#91;").replace("]", "&#93;").replace("`", "&#96;")


def evidence_label(item, name):
    label = safe_text(name)
    url = item.get("html_url") or item.get("target_url") or ""
    if not isinstance(url, str):
        return label
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return label
    # External app target URLs are untrusted too. GitHub is the only link host;
    # no credentials, ports, relative URLs or executable schemes.
    if (parsed.scheme == "https" and parsed.netloc == "github.com" and parsed.path.startswith("/")
            and len(url) <= 2048 and not any(ord(char) < 32 for char in url)):
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
    return label


def finalize_report(status, conclusion, title, summary, *, refs, refs_complete,
                    awaiting_ci, elapsed, result):
    """Format timing and bounded recovery metadata before publishing a verdict.

    Destination markers survive detail truncation. If the markers themselves
    exceed GitHub's output budget, return a failure without claiming a complete
    destination inventory.
    """
    # A delayed seed must still recognize an empty reconciliation as a seed,
    # rather than re-evaluating it through the legacy empty-inventory path.
    if awaiting_ci and status == "in_progress":
        title = "Gate seeded, awaiting CI"
        footer = "\n\n"
    else:
        footer = "\n\n" + EVALUATED_MARKER
    footer += "\n<!-- gatekeeper-refs:" + json.dumps(sorted(refs)) + " -->"
    if refs_complete:
        footer += "\n<!-- gatekeeper-refs-complete -->"
    summary += (f"\n\nEvaluation time: {elapsed:.2f}s. "
                "This gate aggregates CI and configured review evidence; it does not rerun tests. "
                "Pending work is refreshed by subsequent workflow/review events.")
    budget = 60000 - len(footer.encode("utf-8"))
    if budget < 0:
        status, conclusion, title, result = "completed", "failure", "Too many gate destinations", 2
        summary = "Gate destination metadata exceeds GitHub's output limit; operator review required."
        # A delayed seed must still re-evaluate this failure. Do not claim the
        # omitted destination inventory is complete.
        summary += "\n\n" + EVALUATED_MARKER
    else:
        if len(summary.encode("utf-8")) > budget:
            notice = "\n\nAdditional details omitted; inspect the repository Actions runs."
            summary = summary.encode("utf-8")[:max(0, budget - len(notice))].decode("utf-8", errors="ignore") + notice
            summary = summary.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        summary += footer
    return status, conclusion, title, summary, result


def write_job_summary(refs, status, conclusion, title, summary):
    """Summary output is advisory; reporting failure cannot skip gate writes."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        if os.path.exists(path) and os.path.getsize(path) > 850000:
            return
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"### {safe_text(title)}\n\n")
            output.write(f"{safe_text(status)} / {safe_text(conclusion or 'pending')}\n\n")
            output.write("Destinations: " + ", ".join(safe_text(ref) for ref in sorted(refs)) + "\n\n")
            output.write(summary + "\n\n")
    except OSError:
        print("::warning::Could not write gatekeeper job summary")
