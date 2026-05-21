# clustertrace roadmap

Open roadmap for becoming the **unambiguous best-in-class diagnostic layer** for LLM agent failures. We're not trying to be Langfuse-comprehensive — we're trying to be the tool you reach for after Langfuse shows you 1,000 trace rows and you still don't know what's broken.

## Strategic position

| Tool | What they own |
|---|---|
| Langfuse / Phoenix / LangSmith | Production observability platform |
| Helicone | LLM gateway + caching |
| Braintrust | IDE-native eval |
| Datadog LLM Obs | Enterprise unified APM |
| **clustertrace** | **Failure-pattern diagnostics. The cluster page. Drift detection. Causal failure analysis.** |

**The discipline**: we do not build prompt management, LLM gateways, multi-tenant SaaS, or "comprehensive eval." Those battles are already lost. We *integrate* with the tools that won them.

## Dependency graph

```
                            ┌──────────────┐
                            │  Phase 1     │
                            │  Clustering  │ ← Foundation: everything else depends on this
                            │  depth       │
                            └──────┬───────┘
                                   │
                ┌──────────────────┼──────────────────┐
                │                  │                  │
                ▼                  ▼                  ▼
        ┌─────────────┐   ┌──────────────┐   ┌─────────────┐
        │  Phase 3    │   │  Phase 4     │   │  Phase 6    │
        │  Eval loop  │   │  IDE-native  │   │  Production │
        │             │   │  (MCP)       │   │  case study │
        └─────────────┘   └──────────────┘   └─────────────┘

  Independent (no dependency on Phase 1):
        ┌─────────────┐   ┌──────────────────┐
        │  Phase 2    │   │  Phase 5         │
        │  Competitor │   │  TypeScript SDK  │
        │  ingest     │   │                  │
        └─────────────┘   └──────────────────┘
```

## Phases

| # | Phase | Effort | Dep | Status |
|---|---|---|---|---|
| 1 | [Cluster depth](phase-1-cluster-depth.md) — tree-edit-distance, drift detection, auto-repro | 1–2 wk | — | not started |
| 2 | [Competitor ingest](phase-2-competitor-ingest.md) — import Langfuse / Phoenix / LangSmith / OTel exports | 1 wk | — | not started |
| 3 | [Eval loop](phase-3-eval-loop.md) — judge on cluster reps, annotations, pass/fail per cluster | 1–2 wk | 1 | not started |
| 4 | [IDE-native (MCP)](phase-4-ide-native.md) — MCP server + terminal UI | 1 wk | 1 | not started |
| 5 | [TypeScript SDK](phase-5-typescript-sdk.md) — `clustertrace` npm package | 2–3 wk | — | not started |
| 6 | [Production case study](phase-6-production-case-study.md) — one real user, one written story | 4–12 wk | 1 | not started |

## Parallelism plan

There are three independent tracks. Run them in parallel where possible.

```
Time →

Track A  [─── Phase 1 ───][─── Phase 3 ───]              ← clustering work
Track A                  [─── Phase 4 ───]              ← also depends on Phase 1
Track B  [── Phase 2 ──]                                ← ingest, independent
Track C  [──── Phase 5 ────]                            ← TS SDK, independent
Track D  [────────── Phase 6 (continuous) ──────────]   ← human work, no agent
```

**Group 1 (kick off in parallel, day 0)**: Phase 1 + Phase 2 + Phase 5
**Group 2 (kick off once Phase 1 is done)**: Phase 3 + Phase 4
**Group 3 (continuous from day 1, human only)**: Phase 6

## Kickoff from a single Claude Code session

Each phase brief is self-contained — a future agent can read just that one file and have everything needed to start. Recommended kickoff sequence in **one session**:

```python
# Spawn Group 1 in parallel — three agents working independently
Agent(description="Phase 1: cluster depth",
      subagent_type="general-purpose",
      isolation="worktree",
      prompt="Implement docs/roadmap/phase-1-cluster-depth.md per the brief. \
              Read the brief in full first. Use the existing test patterns in tests/. \
              Open a PR when done.",
      run_in_background=True)

Agent(description="Phase 2: competitor ingest",
      subagent_type="general-purpose",
      isolation="worktree",
      prompt="Implement docs/roadmap/phase-2-competitor-ingest.md per the brief. \
              All work is in new files under src/clustertrace/importers/. \
              No conflict with phase 1.",
      run_in_background=True)

Agent(description="Phase 5: TypeScript SDK",
      subagent_type="general-purpose",
      isolation="worktree",
      prompt="Implement docs/roadmap/phase-5-typescript-sdk.md per the brief. \
              All work is under a NEW directory clustertrace-ts/ at the repo root.",
      run_in_background=True)
```

After all three Group 1 agents finish (notifications arrive automatically), spawn Group 2:

```python
Agent(description="Phase 3: eval loop", ...)
Agent(description="Phase 4: IDE-native MCP", ...)
```

**Why isolation="worktree"**: each agent works on its own copy of the repo on its own branch. Their changes don't collide while they're running. When each finishes, you (or the orchestrator) merges its branch into `main`.

**Why background**: their stdout doesn't clog the foreground; you get a notification when each one completes; you can spawn Group 2 the moment Phase 1's notification arrives without waiting on the others.

## Acceptance criteria — when is a phase "done"?

Each brief includes a binary checklist under "What ships." A phase is done when:

1. All "must ship" checkboxes pass
2. New tests are written and `pytest -q` is green
3. `ruff check .` and `pyright src/clustertrace` are clean
4. CHANGELOG entry exists for the phase's version
5. README updated if the user-facing pitch changed
6. A PR is open against `main` (or merged if you're the only reviewer)

When all six phases are done: tag **v1.0.0**.

## What success looks like

| Outcome | Means | Likelihood with these phases shipped |
|---|---|---|
| 300–800 stars in week 4 | Show HN landed + Phase 1+2 shipped | likely |
| 1500–3000 stars by month 3 | Phase 1+2+3+6 shipped + one comparison post landed | plausible |
| 3000–8000 stars by month 6 | All six phases shipped + one production case study + sustained content | the goal |
| The standard diagnostic layer that AI infra people reach for | All six + ecosystem adoption (LangChain integration / Phoenix mention / etc.) | the ambition |

## What we do NOT build

The discipline of refusing these is part of the moat:

- ❌ Prompt management (Langfuse + LangSmith own this)
- ❌ LLM gateway / caching (Helicone owns this)
- ❌ Multi-tenant SaaS (Langfuse Cloud + LangSmith own this)
- ❌ Comprehensive eval platform (Phoenix + Braintrust own this)
- ❌ Auto-instrumentor for every framework (let the framework's OTel instrumentor do it; we ingest)

If a feature request lands and it matches one of these, the answer is "use [tool X] + clustertrace, they integrate."

## The single ruthless question

For every feature you consider building over the next 90 days:

> **What can clustertrace do that Langfuse + 30 lines of glue cannot?**

The features in this roadmap have a concrete defensible answer. Anything that doesn't, we don't build.
