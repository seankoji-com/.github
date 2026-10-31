# Issue labels

Labels use a prefix so their meaning stays readable in a list. Type, priority,
severity and complexity answer different questions and do not replace one
another.

## Severity

Severity describes impact. `severity:SEV1`–`severity:SEV3` use the incident
scale for review findings and bugs as well as active incidents.

| Label | Impact | Default release treatment |
| --- | --- | --- |
| `severity:SEV1` | Essential functionality unavailable, or comparably severe impact such as serious security exposure or irreversible data loss | Blocks release. An exception needs the owner's explicit approval. |
| `severity:SEV2` | Degraded functionality with a workable alternative or workaround | The maintainer evaluates the release requirements and the workaround. |
| `severity:SEV3` | Minor defect with low or no user impact | Normally allows release. |

Severity describes the defect, not the fix. A deferred issue keeps its severity.
An issue form answer is an initial report; triage confirms the level and applies
the label.

## Priority

Priority describes when work is scheduled.

| Label | Response |
| --- | --- |
| `priority:P0` | Immediate response required |
| `priority:P1` | High priority |
| `priority:P2` | Normal priority |
| `priority:P3` | Low priority |

Priority and severity are independent. Do not map `SEV1` to `P1` or copy one
label from the other. A low-priority defect can still block a release, and a
high-priority task can have minor user impact.

## Other labels

- `type:*` records the kind of issue: bug, feature, chore, docs, incident or
  security.
- `complexity:*` is an effort estimate used for planning and model routing.
- `blocked`, `good first issue`, `help wanted` and `wontfix` carry their usual
  meaning.
- Area labels such as `area:*` are optional and repository-specific.

The organization label set is maintained by the private label-sync workflow.
Repositories keep any custom labels they already have.
