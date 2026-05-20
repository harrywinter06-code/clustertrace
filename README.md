# agentlog

Local-first instrumentation for LLM agents. Decorator-based traces, SQLite storage, and a dashboard that **clusters your traces by execution pattern** so you can see which patterns fail and what the failing ones have in common.

The differentiator: most tracing tools dump traces into a list and let you go find the failures yourself. agentlog groups every trace by its structural signature and surfaces the longest path-prefix shared by every failed run — usually the actual root cause.

- **Zero-config** — `@agentlog.trace` and you're done. No signup, no API keys, no env vars required.
- **Local-first** — SQLite at `~/.agentlog/traces.db`. No phone-home, no telemetry, no version checks.
- **Clustering, not just listing** — distinct execution patterns surface automatically; failing clusters and common failure prefixes are computed for you.

## Install

```bash
pip install agentlog                    # core + dashboard
pip install "agentlog[anthropic]"       # adds wrap_anthropic
pip install "agentlog[openai]"          # adds wrap_openai
```

Python 3.11+.

## Quickstart

```python
import agentlog

@agentlog.trace(tags={"agent": "research"})
def my_agent(query):
    ...

my_agent("hello")
```

```bash
agentlog dashboard          # http://127.0.0.1:7777
```

## Full API

```python
import agentlog

# 1. Decorator — sync OR async, captures inputs, outputs, exceptions, duration
@agentlog.trace
async def plan(query): ...

# 2. Nested span context manager (parent linkage via contextvars, async-safe)
with agentlog.span("retrieval", k=5):
    ...

# 3. Explicit tool-call logging
agentlog.tool_call("web_search", args={"q": query}, result=hits)
agentlog.tool_call("web_search", args={"q": query}, error=TimeoutError("…"))

# 4. Tag the current trace (filterable in the dashboard)
agentlog.tag("user_tier", "pro")
agentlog.tag("model", "haiku-4.5")

# 5. Wrap LLM clients (explicit — no global monkey-patch)
from anthropic import Anthropic
from openai import OpenAI
ac = agentlog.wrap_anthropic(Anthropic())
oc = agentlog.wrap_openai(OpenAI())
```

Concurrent `asyncio.gather` calls produce separate traces; nesting tracks the parent automatically via `contextvars`.

## What the dashboard shows

| Page | What |
|---|---|
| **Recent** | filterable trace list (status, tag, name search) with live polling |
| **Clusters** | each distinct execution pattern as one row, with count, failure rate, sample trace link, and the longest path-prefix shared by every failed trace |
| **Failure graph** | per-span error-rate bars, a histogram of where in a trace the failure happened, and a force-directed call graph |
| **Trace detail** | Gantt timeline of spans, expandable input/output, tags, full exception text |

## How clustering works

A trace's *structural signature* is the ordered sequence of `(span_name, status)` pairs across its non-root spans, with consecutive duplicates collapsed (so "fetch × 5" is the same pattern as "fetch × 3"). Two traces with the same signature took the same execution path; clustering by signature reveals the distinct ways your agent runs.

LLM-call spans normalize by provider, not by model id — so a model swap doesn't fragment your clusters.

For the failed traces in your DB, the dashboard computes the longest common `(name, status)` prefix shared by all of them. That prefix is usually a clearer signal than any single stack trace: it tells you the path the agent took *before* it started failing.

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `AGENTLOG_DB` | `~/.agentlog/traces.db` | SQLite file path |
| `AGENTLOG_MAX_PAYLOAD_BYTES` | `32768` | Per-field cap on serialized span I/O |

That's the entire configuration surface.

## Works (v0.2)

- Sync and async `@trace` decoration with full input/output/exception capture
- `agentlog.span(...)` nested spans, `agentlog.tool_call(...)`, `agentlog.tag(...)`
- `wrap_anthropic` and `wrap_openai` for sync and async clients (explicit, not global monkey-patch)
- SQLite storage with WAL mode, schema migrations, and on-demand signature backfill for existing traces
- Per-field payload truncation so a 10 MB tool output doesn't blow up your DB
- Dashboard: filterable trace list with live polling, trace detail timeline, **clusters page**, failure graph, JSON endpoints

## Known limitations (v0.2)

- Only Anthropic and OpenAI have built-in wrappers. Gemini/Bedrock/Ollama land later.
- Streaming responses are logged on completion only, not chunk-by-chunk.
- No replay or run-comparison view yet — you can see which clusters fail, but you can't yet diff a failing trace against a similar succeeding one.
- No retention policy. The DB grows until you delete it; `~/.agentlog/traces.db` is safe to remove between sessions.
- Clustering signatures collapse consecutive duplicates but don't fuzzy-match across reorderings.

## Why I built this

I was debugging a 5-step research agent that fanned out across tools. Every run looked unique in the logs, so I couldn't tell whether each new failure was a one-off or part of a pattern. Existing tracers either need a SaaS account or treat every trace as an independent log line. I wanted the tool to look at all my traces, find the recurring shapes, and tell me which shapes break. That's what the clusters page does.

### Concrete example: 246 runs of three agents

The demo data in `examples/` runs three small agents (`research`, `rag`, `tool_use`) ~80 times each — 246 traces, 47 failures (19%). Across them, agentlog finds **29 distinct execution patterns**. The top two clusters explain **87% of all failures**:

| Pattern | Count | Failure rate |
|---|---|---|
| `retrieve:ok → rerank:error` | 29 | 100% |
| `plan:ok → anthropic.messages.create:ok → web_search:error` | 12 | 100% |

The first one is the RAG agent: retrieval always succeeds, the reranker rejects the docs as off-topic. The fix isn't the reranker — it's the retrieval step returning bad docs for a meaningful fraction of queries. The second is the tool-use agent always tripping over a web_search rate limit after the planner runs. Both diagnoses are one glance, not 47 stack traces.

## License

MIT.
