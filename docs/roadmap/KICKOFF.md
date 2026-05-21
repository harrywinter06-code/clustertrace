# Kickoff — one-session orchestration

This is the operational doc for executing the roadmap from a single Claude Code session. It tells the orchestrator agent exactly which subagents to spawn, in what order, with what isolation, and how to verify each phase landed.

## The dependency graph (one more time, in detail)

```
                day 0  ────────────────────────────────────────────── day 30+
                  │                                                     │
   ┌──────────────┼──────────────┐                                       │
   │              │              │                                       │
 [Phase 1]      [Phase 2]      [Phase 5]   ← three parallel agents, day 0
 cluster        importers      TS SDK         (Group 1)
 depth          (independent)  (independent)
   │              │              │
   │              │              │
   ▼              │              │
 [Phase 3]        │              │
 eval loop ←──────────────────── ┼──   needs Phase 1 done
 (depends         │              │
  on Phase 1)     │              │
   │              │              │
   ▼              │              │
 [Phase 4]        │              │
 IDE-native ←──── ┼─────────────── ┼─   needs Phase 1 done
 (MCP +           │              │     (can run parallel with Phase 3)
  inspect)        │              │
   │              │              │
   ▼              ▼              ▼
   └──── all merged to main, v1.0.0 tagged ────┘
                                                  │
                                                  ▼
                                          [Phase 6 — continuous]
                                          production case study
                                          (human work, weeks 1-12)
```

## Group 1 — kick off in parallel (day 0)

Three agents, each in its own worktree so they cannot collide. Spawn all three in a single message:

```python
# Run these three Agent tool calls in a SINGLE message so they execute in parallel.

Agent(
    description="Phase 1: cluster depth",
    subagent_type="general-purpose",
    isolation="worktree",
    run_in_background=True,
    prompt="""
Read docs/roadmap/phase-1-cluster-depth.md in full first.

Implement every checkbox under "What ships (binary, all must pass)".
Follow the operational rules (commit every 30-60 min, STATUS.md append after each).
Bump version to 0.6.0 on first commit, tag v0.6.0 on the last.

The brief is self-contained — do not ask questions, decide where it says "decide
and commit." If you hit something that says "stop and write BLOCKED.md", do that
and exit.

When done: open a PR against main with title "Phase 1: cluster depth (v0.6.0)".
""",
)

Agent(
    description="Phase 2: competitor ingest",
    subagent_type="general-purpose",
    isolation="worktree",
    run_in_background=True,
    prompt="""
Read docs/roadmap/phase-2-competitor-ingest.md in full first.

All work is in NEW files under src/clustertrace/importers/ — no edits to
existing files except pyproject.toml (extras), src/clustertrace/cli.py (new
import command), and the test directory. No conflict with phase 1.

Implement every checkbox under "What ships". Bump version to 0.7.0 on first
commit, tag v0.7.0 on last.

When done: open a PR titled "Phase 2: competitor ingest (v0.7.0)".
""",
)

Agent(
    description="Phase 5: TypeScript SDK",
    subagent_type="general-purpose",
    isolation="worktree",
    run_in_background=True,
    prompt="""
Read docs/roadmap/phase-5-typescript-sdk.md in full first.

All work is in a NEW directory clustertrace-ts/ at the repo root. The Python
side adds ONE endpoint (POST /v1/traces) to src/clustertrace/dashboard/app.py
plus a test for it. No other Python changes.

Use bun + tsup + vitest. Confirm `npm view clustertrace` shows the name is
available (or owned by us) before starting the SDK work — if taken, write
BLOCKED.md and exit.

When done: open a PR titled "Phase 5: TypeScript SDK (ts-v0.1.0 + py /v1/traces endpoint)".
""",
)
```

## Group 2 — kick off after Phase 1's notification arrives

You'll get a notification when Phase 1's background agent finishes. At that moment, merge Phase 1's PR into main, then spawn:

```python
# After Phase 1 is merged. Two more agents in parallel.

Agent(
    description="Phase 3: eval loop",
    subagent_type="general-purpose",
    isolation="worktree",
    run_in_background=True,
    prompt="""
Read docs/roadmap/phase-3-eval-loop.md in full first.

This depends on Phase 1's cluster API (mode='tree_edit' etc.). Confirm Phase 1
is on main before starting.

Implement every checkbox. Bump to 0.8.0, tag v0.8.0, PR titled
"Phase 3: eval loop (v0.8.0)".
""",
)

Agent(
    description="Phase 4: IDE-native (MCP + inspect)",
    subagent_type="general-purpose",
    isolation="worktree",
    run_in_background=True,
    prompt="""
Read docs/roadmap/phase-4-ide-native.md in full first.

This depends on Phase 1's cluster API. Confirm Phase 1 is on main first.

Implement every checkbox. Bump to 0.9.0, tag v0.9.0, PR titled
"Phase 4: IDE-native (v0.9.0)".
""",
)
```

## Group 3 — continuous, human only

Phase 6 (production case study) cannot be spawned as a subagent. It requires a person sending real emails to real people. **Start day 1**, not after the others are done. Use:

- The target list in `_internal/OUTREACH.md`
- The cold-outreach templates in `_internal/LAUNCH.md`
- The brief in `docs/roadmap/phase-6-production-case-study.md`

Calendar block 30 minutes per day. Track in `_internal/users.csv`. Daily summary appended to `STATUS.md`.

## Merge strategy

Each agent works in its own worktree on its own branch:

- Phase 1 → branch `phase-1-cluster-depth`
- Phase 2 → branch `phase-2-importers`
- Phase 3 → branch `phase-3-eval`
- Phase 4 → branch `phase-4-mcp`
- Phase 5 → branch `phase-5-ts-sdk`

Order of merge into `main`:

1. **Phase 1 first** — Phase 3 and Phase 4 will rebase on top
2. **Phase 2 second** — independent; safe any time
3. **Phase 5 second** — independent; safe any time
4. **Phase 3 third** — after Phase 1 lands
5. **Phase 4 fourth** — after Phase 1 lands

If two agents' work conflicts on the same line (rare — only `pyproject.toml`, `__init__.py`, `cli.py`, `CHANGELOG.md`, `STATUS.md` are touched by multiple phases), you (the orchestrator human) resolve. The brief tells each agent to bump version + append CHANGELOG — those need a small manual rebase.

## Verification — when is each phase truly done?

Each phase's brief has a "Definition of done" section with concrete commands. Before merging a phase's PR, run them locally. If they pass on `main`:

| Phase | Verify |
|---|---|
| 1 | `clustertrace demo` shows `/clusters?mode=tree_edit`, `/drift`, and `clustertrace repro <hash>` emits a runnable pytest file |
| 2 | `echo '<fixture>' \| clustertrace import --from langfuse` works; same for phoenix/langsmith/otel |
| 3 | `clustertrace judge`, `clustertrace annotate`, `clustertrace assert`, `clustertrace check` all work end-to-end |
| 4 | `clustertrace mcp install --target claude-code` configures, and Claude Code can list_clusters; `clustertrace inspect --latest` renders |
| 5 | `cd clustertrace-ts && bun test && bun run build` green; example emits traces visible in clustertrace dashboard |
| 6 | `docs/case-studies/<n>.md` exists, named (or consented-anonymized), 500-1500 words, no marketing |

After all six pass: tag **v1.0.0** with a release note covering everything.

## Commands the orchestrator runs

```bash
# Day 0 — spawn Group 1
# (in your Claude Code session, paste the three Agent() calls above as a single message)

# Day 0 — start outreach work in parallel
$EDITOR _internal/OUTREACH.md       # pick 10 targets
$EDITOR _internal/LAUNCH.md         # customize the email templates
# Calendar-block 30 min/day "user outreach"

# Each Group 1 agent finishes ~ day 1-2; merge in order:
gh pr review phase-2-importers --approve && gh pr merge phase-2-importers --squash
gh pr review phase-5-ts-sdk --approve && gh pr merge phase-5-ts-sdk --squash
gh pr review phase-1-cluster-depth --approve && gh pr merge phase-1-cluster-depth --squash

# Then spawn Group 2 (paste those two Agent() calls into the session)

# Group 2 finishes ~ day 4-5; merge:
gh pr review phase-3-eval --approve && gh pr merge phase-3-eval --squash
gh pr review phase-4-mcp --approve && gh pr merge phase-4-mcp --squash

# Tag v1.0.0 after the case study (Phase 6) lands
git tag -a v1.0.0 -m "v1.0.0 — clustering depth, importers, eval loop, MCP, TS SDK, production case study"
git push --tags
```

## Token budget

Each phase brief is ~250-400 lines. A subagent reading + executing should consume roughly 80-200k tokens of context. Total for all five spawnable phases: ~600k-1M tokens. Run with `--model sonnet` or `opus` depending on phase complexity; phases 1 (algorithmic) and 4 (MCP integration) benefit from `opus`.

## What this kickoff doc does NOT include

- A literal shell script that spawns the agents. The Agent tool is a Claude Code primitive, not a CLI. Paste the Python-style snippets above into your session.
- Continuous-integration auto-merge. Each PR is reviewed by you (the orchestrator), not by another bot.
- Rollback procedure. If a phase breaks main, `git revert` the merge commit and re-spawn the agent with the bug context in the prompt.

## The single instruction

If you're a human reading this and want to execute: **open a new Claude Code session, paste Group 1's three Agent() calls in a single message**. Then wait for notifications.

If you're an AI agent reading this because the orchestrator spawned you: read the specific phase brief named in your prompt. Don't read the other phases unless you need to coordinate. Trust the brief.
