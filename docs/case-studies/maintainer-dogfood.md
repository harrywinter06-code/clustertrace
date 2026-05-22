# Case study: maintainer dogfood — a 40% → 15% failure rate, found in five minutes

> **Honesty disclosure.** This is a *maintainer dogfood self-study*, **not** a third-party customer testimonial. The "user" here is the maintainer; the "team" is one person. Phase 6 of the roadmap calls for a real customer case study, which is still open. This study exists to (a) verify the cluster page actually does what it claims on a workload the author did not write to flatter the tool, and (b) provide a reproducible record any reviewer can re-run and check.
>
> Every number cited here comes from `examples/case_study_research_agent.py` + `examples/case_study_analyze.py` in this repo. Re-run those scripts and you will get the same numbers (the RNG is seeded). If a number in this document does not match the script output, the document is wrong.

## The problem

A small research-assistant agent — five steps: query rewrite, web search, rerank, LLM synthesis, claim verification — written before any debugging, with realistic failure modes baked in. Ran it 200 times over a fixed 40-question set with a fixed seed. **80 of 200 runs failed — a 40.0% failure rate.** Not a number you ship.

The agent's code is in [`examples/case_study_research_agent.py`](../../examples/case_study_research_agent.py). Each step is wrapped in `clustertrace.span`; the agent is decorated with `@clustertrace.trace`. That is the entire instrumentation surface — no manual logging, no extra config.

## The setup

```bash
python examples/case_study_research_agent.py --runs 200 --version v1 --reset
```

This populates `.case-study-db/v1.db` (a SQLite file). Total instrumentation overhead for 200 runs: 18.0 seconds end-to-end, including 200 simulated tool calls. That is ~90 ms per traced run including the deliberate `time.sleep` in each step; the clustertrace overhead is below the time-sleep noise floor — `examples/benchmark.py` measures the per-`@trace` cost at ~35 µs on Windows.

## The diagnosis

Without clustertrace, "40% failure rate" gives you 80 failed traces to read. Pick one — was it the right one to fix? You don't know. You go read all 80.

With clustertrace, `clustertrace stats` (or `python examples/case_study_analyze.py --version v1`) returns one screen:

```
traces_total    = 200
traces_failed   = 80
overall_rate    = 40.0%
n_clusters      = 5

--- top 5 clusters (by count) ---
#1  count=120  errors=  0  rate=0%   query_rewrite:ok -> web_search:ok -> rerank:ok -> synthesize:ok -> verify_claim:ok
#2  count= 50  errors= 50  rate=100% query_rewrite:ok -> web_search:ok -> rerank:error
#3  count= 13  errors= 13  rate=100% query_rewrite:ok -> web_search:error
#4  count= 11  errors= 11  rate=100% query_rewrite:ok -> web_search:ok -> rerank:ok -> synthesize:error
#5  count=  6  errors=  6  rate=100% query_rewrite:ok -> web_search:ok -> rerank:ok -> synthesize:ok -> verify_claim:error
```

Cluster #2 is **50 of the 80 failures — 62.5% of all failures, concentrated in one pattern.** The pattern says exactly which step breaks (`rerank:error`) and what was true at the point it broke (`web_search:ok` — the search worked, the rerank failed). Without the structural grouping you would never see this — the 50 failing traces all have *different* questions and different search results, so a flat list view would scatter them.

The cluster page surprised me on one specific point: I assumed at write-time that the LLM synthesis step (`synthesize:error`, cluster #4) would dominate, because it had the most code paths to fail in. It did not. It produced 11 failures — about 14% of the total. The cluster page exposed an assumption gap I would have spent an hour chasing the wrong direction otherwise.

`compare_traces` (MCP tool added in v0.9.0) confirmed all 50 traces in cluster #2 are structurally identical up to `rerank:error`. Reading any *one* of them would have been enough.

## The fix

Cluster #2's pattern (`web_search:ok -> rerank:error`) localizes the bug to the rerank step. The relevant code in v1:

```python
def tool_rerank_v1(results, question, ...):
    rng.shuffle(results)
    reranked = results[:3]
    if len(reranked) != 3:                     # <-- the bug
        raise RerankFailed(f"expected exactly 3, got {len(reranked)}")
    return reranked
```

`tool_web_search` returns `[result1, result2]` (only 2 items) when the rewritten query has ≤3 keywords. The rerank step's `len(reranked) != 3` check then fires unconditionally. The fix (`tool_rerank_v2`) accepts any non-empty list:

```python
def tool_rerank_v2(results, question, ...):
    rng.shuffle(results)
    reranked = results[:3]
    if not reranked:                           # <-- accept thin results
        raise RerankFailed("no candidates at all to rerank")
    return reranked
```

Four lines of code. Two lines of logic.

## The after

```bash
python examples/case_study_research_agent.py --runs 200 --version v2 --reset
python examples/case_study_analyze.py --version v2
```

```
traces_total    = 200
traces_failed   = 30
overall_rate    = 15.0%
n_clusters      = 4
```

| Metric              | v1 (broken)     | v2 (fixed)      | Delta                |
| ------------------- | --------------- | --------------- | -------------------- |
| Total runs          | 200             | 200             |                      |
| Failed runs         | 80              | 30              | **−50 absolute**     |
| Failure rate        | 40.0%           | 15.0%           | **−25 pp / −62.5%**  |
| # distinct clusters | 5               | 4               | rerank cluster gone  |
| Largest cluster     | 120 ok (60.0%)  | 170 ok (85.0%)  | +25 pp on the OK path |
| Runtime (200 runs)  | 18.03 s         | 17.85 s         | within noise         |

The remaining 30 failures sort into three known clusters, each ≤13 traces:

- `web_search:error` (13) — simulated 5% network timeout. Real fix: retry-with-backoff.
- `synthesize:error` (11) — simulated LLM-over-budget on long inputs. Real fix: input truncation.
- `verify_claim:error` (6) — verifier flags low-confidence factual claims. Real fix: confidence threshold + human-in-the-loop for anchored questions.

Each is a one-paragraph diagnosis and a one-pull-request fix. Without the cluster page, the same 30 failures would be 30 traces to read.

## What the cluster page actually saved

- **Wrong-direction time avoided** — I would have started on the LLM synthesis path because that is what I half-remembered as fragile. Cluster #2 said `rerank` instead. Saved: ~30 minutes I would have spent reading synthesis traces.
- **Trace-reading volume avoided** — without clustering, "find the dominant failure" means reading at least the first 10 failures (a 12.5% sample), about 5 minutes of careful scanning. With clustering, the top cluster says "50 of 80 here, look at one of them." Saved: ~4 minutes per diagnosis cycle.
- **Verifiable claim** — every number above is reproducible from `examples/case_study_research_agent.py` + `examples/case_study_analyze.py` with the default seed. Re-run them to confirm. Output JSON lives at `.case-study-db/v1-stats.json` and `.case-study-db/v2-stats.json` after a run.

## Direct quote

> This is the maintainer talking; not a customer. The dogfood test confirms the cluster page does what the README claims: across 200 traces of a deliberately-broken agent, one cluster carried 62.5% of failures, and the screen surfacing it took five seconds to read. A four-line fix dropped the failure rate from 40% to 15%. The remaining 30 failures sort into three small clusters, each one diagnostic. **This is what I built clustertrace for, and the only thing I changed was looking at the cluster page first.**

> — clustertrace maintainer

## Reproducing

```bash
# Fresh venv
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Workload — populates two SQLite DBs under .case-study-db/
python examples/case_study_research_agent.py --runs 200 --version v1 --reset
python examples/case_study_research_agent.py --runs 200 --version v2 --reset

# Diagnostics — writes JSON snapshots cited in this document
python examples/case_study_analyze.py --version v1 --top-clusters 10
python examples/case_study_analyze.py --version v2 --top-clusters 10

# Optional: open the dashboard against either DB
CLUSTERTRACE_DB=.case-study-db/v1.db clustertrace dashboard
```

## What this study does NOT prove

- It does not prove clustertrace is useful in production. The workload is synthetic.
- It does not prove a real engineering team would see the same speedup. The fix here is obvious-in-hindsight; real bugs aren't.
- It does not substitute for the Phase 6 customer case study. That work — finding a real named team, running clustertrace on their actual traffic, getting their numbers — is still open.

This study proves one thing only: **on a workload with deliberately-injected realistic failure modes, the cluster page surfaces the dominant pattern in one screen, and the fix is localized.** The architectural claim — "structural clustering beats a flat trace list" — is verifiable. That is the minimum I needed before asking a real customer to try it.
