# Standalone persona review status

`actions/persona-status` is an additive CLI/composite for a trusted controller.
It publishes the commit status `persona / grumpy-engineer` on one exact head SHA.
It never creates a review, reads review text as a verdict, publishes CI, or writes
to another head or a test-merge SHA. Existing gatekeeper and native CI behavior
is unchanged until a separately verified repository migration.

GitHub's [required-reviewer schema](https://docs.github.com/en/rest/orgs/rules)
supports reviewing teams, rather than directly requiring this bot or App.
The publisher therefore reads actual reviews from bot user ID `283599686`, with
type `Bot`, on the supplied commit. Every current open PR sharing the head must
have a latest submitted approval. Requested changes fail; missing, dismissed,
comment-only or stale approval stays pending. Drafts and forks have no exemption.

## Trusted same-job envelope

Use a fresh, target-scoped installation token from a dedicated publisher App
with pull requests read, commit statuses write and metadata read. Keep its key
only at the trusted controller boundary. Do not broaden another App or expose
the token through job outputs, encoding, command arguments or PR-controlled code.
The same isolated job mints the token and calls this immutable action:

```yaml
jobs:
  publish-persona:
    # Dispatch inputs must already come from fresh trusted GitHub metadata.
    concurrency:
      group: persona-status-v1-${{ inputs.target_repository }}-${{ inputs.head_sha }}
      cancel-in-progress: false
    # Select trusted isolated execution using the controller's private policy.
    # Mint the fresh scoped token in this SAME job as step `mint`.
    steps:
      - uses: seankoji-com/.github/actions/persona-status@<approved-40-hex-commit>
        with:
          target_repository: ${{ inputs.target_repository }}
          persona_pr: ${{ inputs.persona_pr }}
          head_sha: ${{ inputs.head_sha }}
          publisher_bot_id: ${{ inputs.publisher_bot_id }}
          token: ${{ steps.mint.outputs.token }}
```

All publisher routes must share that group in ONE controller repository. The
composite itself cannot create a job mutex. Keep a running writer; pending
intents may coalesce because each survivor reads the current state. Do not add a
repository-wide FIFO. Workers, recovery and broker routes send refresh intents,
not separate status writes. The broker must resolve the canonical head before
admission. A delayed completion for an old head must never target its successor.

Required inputs are `target_repository`, `persona_pr`, `head_sha`,
`publisher_bot_id` and `token`. Optional `persona_state` accepts empty, `running`
or `failed` as diagnostics only. `dry_run` accepts exactly `true` or `false` and
defaults to `false`; true makes no API writes, including pending/error paths.
The colocated CLI accepts the same values through `PERSONA_*` environment names
or the non-secret flags shown by `python3 -I actions/persona-status/publish.py --help`.
Distribute both `publish.py` and `action.yml`: their content digest versions the
policy certificate. There are no sibling Python imports or downloaded packages.

The independent `publisher_bot_id` validates returned status creators. It is not
installation-token introspection or proof of the App's integration ID. The
deployment must verify the actual App-bound required rule; there is no
`GITHUB_TOKEN` fallback. The action masks its token before invoking the CLI.
Direct CLI callers must mask the token at their trusted mint boundary as well.
No credentials, raw reviews, request headers or API response bodies enter logs.

## Results, bounds and invalidation

GitHub uses the [latest status for each context](https://docs.github.com/en/rest/commits/statuses).
The action reads current PRs and reviews before any certificate reuse. Its fixed
digest covers policy/action bytes, head, current PR membership and selected
review IDs/states. Duplicate intents with matching current state need no writes.
A new success first publishes pending to verify its returned publisher identity,
then re-reads reviews. Every final write is read back and checked against another
current snapshot. Changed snapshots retry at most three passes. This is not an
atomic review-read-and-status transaction.

History accounting includes every creator's case-insensitive same-context
statuses. At most 50 status GETs and 5,000 total returned records are inspected
across history/readback/error reconciliation, under a 120-second API deadline.
A full last history page is conservatively treated as overflow. Malformed,
truncated or unreadable history is an error, never zero. At 950 context records
the publisher refuses success, retaining reserve for pending, terminal and error
before GitHub's 1,000-record limit. Stable error is deduplicated; an operator must
resolve an exhausted head, usually through a fresh reviewed commit.

API errors after safe target binding attempt an error status in the same writer
lane. If initial binding or publication is unreadable, old-success revocation is
unverified. A superseded target writes nothing to a successor. A closed target
can refresh its exact original head only if complete current metadata identifies
other open PRs on that same head; its own review is excluded. The publisher also
fails closed when an open PR's merge SHA collides with another open PR's head,
and still writes only its supplied head.

The action output `result` is safe JSON with head, state, certificate, status ID,
changed/bound/evaluated/stale/dry-run flags, reason and status verification. Pending
or failure is a completed evaluation, not an approval. Errors return nonzero;
an unverified status must not be treated as cleared. Controllers may use these
outputs for exact terminal acknowledgements, including after idempotent refreshes.

## Activation and rollback gates

Source publication, unit tests and dry-runs are not hosted acceptance. Before
any live cutover, verify all of these in the repository's actual configuration:

- A dedicated App token can post this status, and the required persona rule
  binds the correct integration ID. Test wrong-writer rejection and permission
  failures. An Actions check with the same required name also creates a second
  requirement, so keep all internal job names distinct.
- GitHub selects the intended head status with native CI, legacy merge-ref
  publication, foreign merge statuses and base movement. Exercise a merge SHA
  that is another PR's head. Head-only status is a source choice that still
  requires this live proof.
- Durable opened/head-change/submitted/edited/dismissed/recovery delivery and
  queue capacity meet an explicit agreed latency bound. A new unreviewed PR on
  an already-approved head, or a dismissal while queued, can inherit old success
  until the writer executes. Per-PR actual-review merge preflight remains
  mandatory; latest-context semantics and App binding do not close that window.
- Native CI and persona remain separately required before removing legacy
  enforcement. Preserve both protections through rollback and retain an
  immutable, verified publisher revision.

Legacy coexistence needs special handling: the old evaluator includes commit
statuses but has no status-change trigger. The private controller must request a
legacy gate refresh after persona evaluation when the actual production publisher
route is confirmed legacy, including idempotent or error outcomes when bound.
Use the separate existing CI dispatcher and exact head. Native mode must never
call that legacy CI publisher. Both/neither/unreadable route evidence is a
configuration error, not permission to fall back. This public action dispatches
no CI workflow. Test the rollback configuration as well as forward cutover.
