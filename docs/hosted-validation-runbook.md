# Hosted Validation Runbook: Gatekeeper Review Event & Stuck Check Verification

This runbook documents the step-by-step verification procedure for validating the PR Gatekeeper fix on hosted GitHub Actions infrastructure during authorized hosted acceptance testing, alongside the historical hosted incident evidence from PR #54.

## Context & Problem Statement

Prior to this fix, when persona bots (such as `bot-grumpy-engineer`) submitted an approval on a PR, the review signal would fire, but the gatekeeper run skipped execution, leaving the required check `gatekeeper / all-checks-passed` in the `in_progress` state indefinitely until manually rerun.

The fix introduces:
1. **Direct Admission for Same-Repository PRs**: Direct `pull_request_review` events on same-repo PRs evaluate and publish check-runs immediately with `checks: write` runner permissions.
2. **Privileged `workflow_run` Route for Fork PRs**: Fork PR reviews run with read-only tokens (`checks: read`). They trigger the unprivileged signal workflow `call-reusable-review-event.yml`, whose completion automatically triggers `call-reusable-pr-gatekeeper.yml` on `main` via `workflow_run` with privileged `checks: write` tokens.
3. **Explicit 403 Diagnostics**: If an unprivileged token attempts publication, `scripts/pr-gatekeeper.py` outputs a clear diagnostic indicating fork review token restrictions.

---

## Historical Incident Trace & Root Cause Evidence (PR #54)

The bug was directly observed and recorded on hosted GitHub Actions in PR #54 (`test(gatekeeper): cover remaining refresh edge cases from #49`):

- **Event & Workflow Run**: Run [39085362103](https://github.com/seankoji-com/.github/actions/runs/39085362103)
  - Triggered via: `pull_request_review` at `2026-10-18T01:08:09Z` upon persona bot review submission.
  - Job `review-event / signal`: `SUCCESS` (ID 117550641169).
  - Job `gatekeeper`: `SKIPPED` (ID 117550642307) because `jobs.gatekeeper.if` did not admit `pull_request_review`.
- **Downstream Blockage**:
  - Because the workflow is named `PR Gatekeeper`, GitHub Actions prohibited `workflow_run` from triggering another run of `PR Gatekeeper`.
  - No automated follow-up run was created at `01:08:09Z`.
  - The required check `gatekeeper / all-checks-passed` was left stuck `in_progress` indefinitely.
- **Resolution via Manual Rerun**:
  - At `2026-10-18T01:37:49Z`, manual rerun of run [39027991584](https://github.com/seankoji-com/.github/actions/runs/39027991584) finally published `gatekeeper / all-checks-passed: SUCCESS` (Job ID 117556095148).

### Architectural Solution
- **Same-Repo PRs**: Admitted directly in `call-reusable-pr-gatekeeper.yml` under `github.event.pull_request.head.repo.full_name == github.repository`. Bypasses `workflow_run` entirely, avoiding both runner delay and self-trigger prohibition.
- **Fork PRs**: Emitted by caller workflow `call-reusable-review-event.yml` (`PR Review Event`). Cross-workflow triggers (`PR Review Event` -> `PR Gatekeeper`) are fully permitted by GitHub Actions, allowing the privileged runner on `main` to publish check runs.

---

## Hosted Verification Procedure

### Test Case 1: Same-Repository PR Review Approval

1. **Create a Test PR from a local branch**:
   ```bash
   git checkout -b test/verify-review-approval
   echo "test verification" > test-trigger.txt
   git add test-trigger.txt
   git commit -m "test: trigger gatekeeper review validation"
   git push origin test/verify-review-approval
   gh pr create --title "Test Gatekeeper Review Approval" --body "Testing gatekeeper review completion"
   ```

2. **Trigger Persona Bot Review**:
   Request a review from `bot-grumpy-engineer` or submit an approval from the persona bot account:
   ```bash
   gh pr review <PR_NUMBER> --approve -b "LGTM: persona approval"
   ```

3. **Verify Immediate Direct Evaluation**:
   - Check the triggered workflow runs:
     ```bash
     gh run list --event pull_request_review
     ```
   - Watch the latest run:
     ```bash
     gh run watch <RUN_ID>
     ```
   - Verify that the `gatekeeper` job was admitted (`jobs.gatekeeper.result == 'success'`).
   - Confirm check-run status on the PR:
     ```bash
     gh pr checks <PR_NUMBER>
     ```
   - **Expected Outcome**: `gatekeeper / all-checks-passed` is `completed: success` without manual rerun.

4. **Cleanup**:
   ```bash
   gh pr close <PR_NUMBER> --delete-branch
   ```

---

### Test Case 2: Fork PR Review Event (Privileged Follow-up Route)

1. **Submit a Fork PR**:
   From a fork clone or branch:
   ```bash
   git checkout -b test/verify-fork-review-approval
   echo "fork test verification" > test-trigger.txt
   git add test-trigger.txt
   git commit -m "test: trigger gatekeeper fork review validation"
   git push origin test/verify-fork-review-approval
   gh pr create --head <fork-owner>:test/verify-fork-review-approval --title "Test Fork Gatekeeper Review Approval" --body "Testing fork gatekeeper review completion"
   ```

2. **Submit Review on the Fork PR**:
   - Have `bot-grumpy-engineer` submit an approval on the fork PR:
     ```bash
     gh pr review <FORK_PR_NUMBER> --approve -b "LGTM: persona approval on fork"
     ```

3. **Verify Signal Workflow Run**:
   - Confirm `PR Review Event` (`call-reusable-review-event.yml`) triggers on `pull_request_review`:
     ```bash
     gh run list --workflow "PR Review Event"
     ```
   - Verify it finishes cleanly (`status: completed, conclusion: success`).

4. **Verify Automatic Downstream Gatekeeper Run**:
   - Confirm `PR Gatekeeper` (`call-reusable-pr-gatekeeper.yml`) triggers automatically via `workflow_run`:
     ```bash
     gh run list --workflow "PR Gatekeeper" --event workflow_run
     ```
   - Check the run logs to verify it resolved the review event via `--review-run <SIGNAL_RUN_ID>`.
   - Confirm check-run status:
     ```bash
     gh pr checks <FORK_PR_NUMBER>
     ```
   - **Expected Outcome**: `gatekeeper / all-checks-passed` check run publishes `completed: success` without manual rerun.

5. **Cleanup**:
   ```bash
   gh pr close <FORK_PR_NUMBER> --delete-branch
   ```

---

## Rollback & Safety

If unexpected behavior occurs:
- The gatekeeper job admission condition can be inspected in `.github/workflows/call-reusable-pr-gatekeeper.yml`.
- Standard manual rerun (`gh run rerun <RUN_ID>`) continues to operate as an emergency fallback.
