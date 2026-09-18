# Agent readiness — the org standard for cheap agentic change

**Goal:** the token cost of an agentic change is proportional to the complexity of the
change. Not to the size of the repo, the size of the always-loaded guide, or how many
stale paths an agent has to trip over first.

**Enforced by:** `scripts/agent_readiness.py` — stdlib, no network, runs on any checkout.
Per PR as `Agent Readiness` (a ratchet: only what the PR made worse), weekly as the
`Agent readiness fleet report` issue (the absolute picture). Every repo starts advisory
and opts into gating by itself.

```bash
python3 scripts/agent_readiness.py --root ~/repos/zooma          # one repo
python3 scripts/agent_readiness.py --fleet ~/repos --json         # everything under a dir
python3 scripts/agent_readiness.py --root . --baseline base.json  # what changed vs a prior --json run
```

## The standard

### Always-loaded context: one canonical guide, under budget

| File | Role |
| --- | --- |
| `AGENTS.md` | The canonical, harness-neutral guide. What the repo is, the gates, the rules that are not the agent's to relax, where things live. Every harness (Claude Code, Codex, OpenCode, Gemini) reads it. |
| `CLAUDE.md` | `@AGENTS.md` on the first line, then Claude-only deltas (permissions, hooks, MCP quirks). Never a second copy of the rules. |
| `docs/**` | Reference material: runbooks, schemas, API notes, decision records. Linked from the guide **by path**, read only when the task needs it. |
| `apps/<x>/AGENTS.md` | Monorepo packages with real code carry their own guide, so a session in one package does not load every package's rules. |

Budget: **4,000 tokens** for the guide plus everything it `@imports` (measured as
bytes ÷ 4, an estimate; the same estimate on both sides of a ratchet, which is what
matters). Above that, the fix is to move sections into `docs/` and leave a one-line
pointer, not to compress the prose. Above double the budget the finding is high.

Why: the guide is paid for on every turn of every session, before any work starts.
A 7k-token guide costs more per session than most of the changes made in it.

### Files: no monsters, no blobs

- Source files stay under **800 lines** (tests 1,500). Every read of a 4,000-line file
  costs more than the change touching it, and the agent reads it more than once.
- Data belongs out of the tree: large JSON/CSV/markdown exports pollute every grep.
  If it must stay, list it under `ignore` in `.agent-readiness.json`.
- Lockfiles and generated files carry `linguist-generated -diff` in `.gitattributes`
  so they collapse in diffs and reviews.
- Session spines (`GOAL.md`), backups, and editor droppings are never committed.

### Docs and commands: no dead paths

Every markdown link and every `` `path/to/file.ext` `` in a doc is a claim that the
file exists. A `.claude/` command, skill, or agent that names a missing path is worse
than a stale doc: an agent will act on it. Fix the path, or delete the reference.

Changelogs and anything under an `archive/`, `legacy/`, or `deprecated/` directory
are exempt: they describe what was.

### What is *not* in the standard

Test coverage and code style are fitness judgments, not token-cost signals. The
checker reports `tests-missing` at low severity for the weekly picture and never gates
on it. Human and automated code review own quality.

## The checks

| Check | Severity | Ratchet behaviour in a PR |
| --- | --- | --- |
| `context-split` | high | new split fails (when enforced) |
| `context-duplicate` | medium | new duplicate fails |
| `context-budget` | medium, high at 2× | a guide **newly** over budget, one that **grew >10%** while over, or one crossing into high fails |
| `stale-commands` | high | a `.claude/` command/skill/agent **linking** to a new dead path fails |
| `doc-drift` | medium | a **newly** dead markdown link target fails (one finding per target, however many docs link to it) |
| `config-invalid` | high | `.agent-readiness.json` unreadable: defaults apply and `enforce` is treated as off, so this never gates — it is loud in the weekly report instead |
| `monster-files` | low | a file **newly** over the limit, or one already over that **grew >10%**, is promoted to medium and fails; a pure rename is followed (`git diff -M`) and does not count |
| `path-drift`, `tracked-blobs`, `gitattributes`, `tracked-scratch`, `context-missing`, `tests-missing`, `nested-guides`, `waiver-expired` | low | weekly report only; never gate |

`path-drift` is the backtick form (`` `src/x.sh` `` in prose). It stays low because such
mentions often describe another repo or a file the reader is told to create; a markdown link
is a promise of navigability and is held to the higher bar.

Existing debt never fails a PR. Adding to it does — once the repo opts in. If the ratchet
has no merge-base to compare against (a transient git failure), the run reports absolute
findings as a warning and exits green: no ratchet, no gate.

## Per-repo config: `.agent-readiness.json`

```json
{
  "enforce": true,
  "context_budget_tokens": 4000,
  "max_code_lines": 800,
  "ignore": ["docs-site/**", "content/batches/**"],
  "waivers": [
    {"check": "monster-files", "subject": "api/src/data.ts", "until": "2026-12-31",
     "reason": "split tracked in #123"}
  ]
}
```

- `enforce` — off by default. Turn it on once the weekly report shows the repo has
  nothing you disagree with; turn it off **in the same PR** if the checker is wrong.
  That is the escape hatch: no hub round-trip, no redistribution.
- `waivers` — dated. Expired waivers surface in the weekly report as `waiver-expired`.
- `always` — scan even when the repo has fewer than 50 source files (small repos are
  skipped by default; a 12-file zsh plugin does not need an AGENTS.md).
- Every other key in `DEFAULT_CONFIG` at the top of the script can be overridden.

## Rollout

1. Hub PR merges → `panel/call-reusable-agent-readiness.yml` is distributed to every
   non-archived repo by the existing caller distributor (advisory: green unless enforced).
2. First weekly report lands → the fleet baseline, one issue in this repo.
3. Remediation PRs clear the high/medium findings per repo (context splits, dead
   paths). Content trimming of over-budget guides is proposed in the PR body and
   applied by a human: an agent cannot tell which sentence stops the next agent
   breaking prod.
4. A repo flips `"enforce": true` when its report is clean or waived.

## Blind spots the checker does not cover (yet)

Measured while building this, worth knowing, not automated:

- **User-level context** dwarfs any repo guide: the global `~/.claude/CLAUDE.md`
  (~1.5k tokens), 500+ permission rules, 9 hook events, 12 plugins, and every MCP
  connector's tool schema are loaded into *every* session in *every* repo. Deferred
  tool loading helps; a connector that is enabled but unused for a repo still costs
  its name. Review the connector list per project, not once globally.
- **Hooks that inject output** (`SessionStart`, `UserPromptSubmit`) add tokens per turn.
  The weekly report inventories hook counts per repo; it does not measure their output.
- **CI feedback latency** is a token cost: every red loop is a full re-read. This check is
  ~10s of stdlib Python so it adds little to the self-hosted pool, but the pool's queue
  depth on a busy day is itself worth watching.
- **Tool-result size**: long test output, verbose logs, `git diff` of a lockfile. The
  `gitattributes` check covers the last one; the rest is per-repo hygiene (quiet test
  reporters, `--quiet` flags in the guide's command list).
