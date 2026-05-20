# agentlog

Zero-config, local-first instrumentation for LLM agents. Decorator-based traces, SQLite storage, dashboard with a visualization of where agents fail.

Built because debugging multi-step agents needs visual trace tooling that doesn't require a SaaS account.

- **Zero-config** — `@agentlog.trace` and you're done. No signup, no API keys, no env vars required to start.
- **Local-first** — SQLite on your machine at `~/.agentlog/traces.db`. No phone-home, no telemetry, no version checks.
- **Visual failure clusters** — one good visualization that shows where, across many runs, your agent tends to fail.

## Install

```bash
pip install agentlog              # core library + dashboard
pip install "agentlog[anthropic]" # adds wrap_anthropic helper
```

Python 3.11+.

## Quickstart

```python
import agentlog

@agentlog.trace
def my_agent_step(user_input):
    ...

my_agent_step("hello")           # writes a trace to ~/.agentlog/traces.db
```

Then:

```bash
agentlog dashboard               # open http://127.0.0.1:7777
```

## What you can log

```python
import agentlog

# 1. Decorate any sync or async function
@agentlog.trace
async def plan(query): ...

# 2. Group work inside a function
with agentlog.span("retrieval", k=5):
    ...

# 3. Log tool calls explicitly
agentlog.tool_call("web_search", args={"q": query}, result=hits)
agentlog.tool_call("web_search", args={"q": query}, error=TimeoutError("…"))

# 4. Wrap an Anthropic client (explicit — no global monkey-patch)
from anthropic import Anthropic
client = agentlog.wrap_anthropic(Anthropic())
client.messages.create(model="claude-haiku-4-5-20251001", ...)
```

Async functions and concurrent `asyncio.gather` calls produce separate traces correctly — span nesting tracks the parent via `contextvars`.

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `AGENTLOG_DB` | `~/.agentlog/traces.db` | SQLite file path |

That's the entire configuration surface.

## Dashboard

`agentlog dashboard` launches a small FastAPI app on port 7777.

- **Recent** — the last 50 traces with status, duration, and error preview.
- **Trace detail** — timeline of spans with expandable input/output and full exception text.
- **Failure clusters** — per-span error-rate bars, a histogram of *which step* in a trace failed, and a force-directed call graph where nodes are colored by error rate and edges represent observed transitions.

## Works

- Sync and async `@trace` decoration with full input/output/exception capture.
- Nested spans via `agentlog.span(...)` — async-safe via `contextvars`.
- Explicit `wrap_anthropic` for `messages.create` (sync and async clients).
- Local SQLite storage with schema migrations and a per-process init guard.
- Dashboard endpoints: recent traces, trace detail, aggregate failure graph.

## Known limitations (v0.1)

- Only Anthropic SDK has a built-in wrapper. OpenAI/Gemini will land in v0.2.
- The dashboard reads the active DB but does not poll for live updates — refresh manually.
- No tagging, search, or filter UI yet — query the SQLite file directly if you need filters.
- Streaming responses from `messages.create` are logged on completion only, not chunk-by-chunk.
- No retention policy. The DB grows until you delete it; `~/.agentlog/traces.db` is safe to remove between sessions.

## Why I built this

I was debugging a multi-step agent that fanned out across tools, and the conversation logs alone weren't enough — I needed to see, across dozens of runs, *which step* the agent tended to break on. SaaS tools require a signup and a network round-trip per call; existing OSS options either monkey-patch every HTTP client in your process or impose a heavy schema. agentlog is the smallest thing that gave me the visualization I needed.

## License

MIT.
