# Verify review-event delivery

Use a disposable pull request in a repository with
`PERSONA_REVIEW_REQUIRED=true`. The policy is documented in
[Required persona review](../README.md#required-persona-review).

1. Record the pull request's current head SHA and GitHub's test-merge SHA.
2. Check `gatekeeper / all-checks-passed` on both commits before Grumpy Engineer
   submits a review. With other checks passing, both copies should be
   `in_progress` while the review is missing.
3. Address any findings. After Grumpy submits `APPROVED` for the current head,
   confirm both gate copies become successful once the other checks pass.
4. Dismiss that approval on the disposable PR. Confirm the `pull_request_review`
   signal run completes and its `workflow_run` follow-up returns both gate
   copies to `in_progress`. Do not merge the verification PR.
5. Close the disposable PR after recording the run links and check results.

The [caller](../.github/workflows/call-reusable-pr-gatekeeper.yml) defines the
signal and follow-up events. The [evaluator](../scripts/pr-gatekeeper.py) reads
Grumpy's submitted review and publishes the gate results. If a signal or
follow-up fails, inspect its job logs before changing enforcement settings.
