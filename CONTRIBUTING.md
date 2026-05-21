# Contributing

clustertrace is small on purpose. Before adding a feature, read [ARCHITECTURE.md](ARCHITECTURE.md) — most additions touch the same five files and the same five tables.

## Setup

```bash
git clone https://github.com/harrywinter06/clustertrace
cd clustertrace
uv venv
uv pip install -e ".[anthropic,openai,dev]"
```

Run tests: `pytest -q`. Run lint: `ruff check .`. Both should be green before you open a PR.

## Where things live

```
src/clustertrace/
  __init__.py        public API surface — keep additions explicit
  trace.py           @trace, span, tag, metric, tool_call (uses _ctx)
  _ctx.py            shared contextvars (trace_id, span_id, new_id)
  storage.py         SQLite + schema migrations
  cluster.py         signatures, clusters, failure-prefix mining
  cost.py            pricing table + per-span/trace cost
  anthropic.py       explicit wrapper for Anthropic client
  openai.py          explicit wrapper for OpenAI client
  otel.py            OpenTelemetry SpanExporter
  snapshot.py        single-file shareable HTML
  export.py          JSONL export/import
  replay.py          re-run a stored trace
  cli.py             click commands
  dashboard/
    app.py           FastAPI routes + JSON endpoints
    templates/       Jinja2 pages (no React, no build step)
    static/style.css
```

Tests in `tests/` mirror module names: `test_storage.py`, `test_cost.py`, etc.

## Adding a new LLM provider wrapper

This is the most common contribution. Read [`src/clustertrace/anthropic.py`](src/clustertrace/anthropic.py) first — it's ~180 lines and the pattern below mirrors it.

1. **Create `src/clustertrace/<provider>.py`.** Define `_WrappedClient` (sync + async detected by class name), wrap the `.create()` method on whatever object hangs off the client (e.g. `client.chat.completions` for OpenAI, `client.messages` for Anthropic).
2. **Open a span before calling the underlying SDK.** Use `current_trace_id` and `current_span_id` from `clustertrace._ctx`. If `current_trace_id` is None, open a root trace.
3. **Map the response to a `dict` summary** — at minimum `model`, `usage.input_tokens`, `usage.output_tokens`, `stop_reason`, and a truncated `content` preview. Store on `spans.output_json` and `spans.attrs_json`.
4. **Add `wrap_<provider>` to `clustertrace/__init__.py`** with a lazy import (so the package doesn't pull in the provider's SDK as a hard dep).
5. **Add an optional install extra in `pyproject.toml`:** `"<provider>" = ["the-sdk>=X.Y"]`.
6. **Add a price entry in `cost.PRICING`** for each model id.
7. **Write a `tests/test_wrap_<provider>.py`** using a `Fake<Provider>` class so tests run without API calls. Mirror `tests/test_wrap_openai.py`.

A new wrapper PR should be ~250 lines of code + ~150 lines of tests.

## Schema changes

Don't edit existing migrations — append a new one to `storage._MIGRATIONS` and bump `_SCHEMA_VERSION`. Migrations must be idempotent (use `IF NOT EXISTS`, `ALTER TABLE ADD COLUMN` is safe because SQLite errors are caught by the runner).

When you add a column that needs backfill (like `cost_usd`), put the backfill logic in the appropriate module's `backfill()` function and wire it into the dashboard endpoint (`api_clusters` already does this for signatures).

## Pull requests

Small PRs preferred. One logical change per PR. A good PR includes:

- A line in `STATUS.md` describing what changed
- Tests covering the new behavior + at least one failure mode
- README updates if the public API or pitch changes
- A clean `ruff check .` and a green `pytest -q`

If your change adds an optional dependency, note it in both `pyproject.toml` (extras) and the README install section.

## What I'd love to see in a PR

These are real gaps that would meaningfully help users:

1. **Reorder-insensitive clustering** as an optional signature mode (set-of-edges or tree-edit-distance)
2. **`wrap_bedrock` / `wrap_gemini`** native wrappers (Anthropic's Bedrock/Vertex clients already work via `wrap_anthropic`)
3. **Streaming response capture** for `messages.create(stream=True)` and `chat.completions.create(stream=True)`
4. **`@trace(skip=True)`** and **`@trace(sample=0.1)`** for production deployments
5. **Replay with prompt diff** — modify captured kwargs before re-invocation
6. **`clustertrace tail`** — CLI command that streams new traces to the terminal
7. **Auto-instrumentation hooks** for common frameworks (LangChain, LlamaIndex, DSPy)

Open an issue first if you're going to spend more than a couple of hours on something.

## Tone

Honesty over hype. The README should never claim a feature that isn't shipped, a benchmark that isn't reproducible, or a comparison that isn't fair. If you ship something half-built, label it accordingly in "Known limitations."
