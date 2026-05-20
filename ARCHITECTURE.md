# ARCHITECTURE

This document explains the design choices in agentlog. It's intentionally short — the codebase is small enough that you can read it end-to-end in an hour. Read this first.

## Storage

One SQLite file, WAL mode, schema versioned by a `schema_meta.version` row and a list of migration scripts in `storage._MIGRATIONS`. Adding a column or table = append a new script and bump `_SCHEMA_VERSION`. Migrations are applied in order, exactly once, by `_ensure_initialized` on first connect.

There are five tables:

| Table | Purpose |
|---|---|
| `traces` | one row per top-level trace (status, duration, signature, cost) |
| `spans` | every span in every trace, parent-linked |
| `trace_tags` | key/value tags filterable in the dashboard |
| `trace_metrics` | numeric metrics from `agentlog.metric()` for time-series charts |
| `spans_fts` | FTS5 virtual table mirroring span name + I/O + error_message |

The FTS5 table is kept in sync via three triggers (insert/update/delete on `spans`). When migration v3 runs on an existing DB, the table is backfilled inline.

## Tracing

`@agentlog.trace` opens a span when the function is called and closes it on return or exception. Parent linkage uses `contextvars` — a `ContextVar` for `trace_id` and one for `span_id`. This is what makes async work without manual plumbing: `contextvars` propagate across `await` boundaries and `asyncio.gather`, so a span opened in a parent task is visible to spans opened in child tasks.

The decorator distinguishes sync vs. async functions via `inspect.iscoroutinefunction(fn)` and dispatches to different code paths (`_sync_call_span` and `_AsyncCallSpan`) so that we never accidentally `await` a non-coroutine or block on a coroutine.

## Signatures and clustering

The differentiator. A trace's *signature* is the ordered sequence of `(normalized_name, status)` pairs from its non-root spans, with consecutive duplicates collapsed (RLE). Two traces with the same signature took the same execution path. Clustering by signature reveals the distinct ways an agent runs.

Three design choices worth justifying:

**Why exact-string equality, not fuzzy?** Trade-offs in v0.3:

| Approach | Pros | Cons |
|---|---|---|
| Exact ordered + RLE | Cheap, deterministic, easy to explain | Reorderings split clusters |
| Set-of-edges | Reorder-insensitive | Loses order information that's often diagnostic |
| Tree-edit distance | Captures intuitive similarity | O(n²) per pair, expensive at scale |
| Learned embeddings | Captures semantic similarity | Adds ML dep, opaque, harder to debug |

For v0.3 we go with the simple approach. Reorder-insensitive clustering is a real gap and is on the v0.4 list. Fuzzy clustering on top of the exact signatures is straightforward to layer in later.

**Why normalize LLM-call names by provider, not by model?** Because changing models is the most common thing an agent dev does, and we don't want every prompt-tuning iteration to fragment your clusters into N×(number of models) groups. The model is preserved in span attributes; clustering uses the provider prefix only.

**Why collapse consecutive duplicates (RLE)?** Agents loop. "Fetched 3 papers" and "fetched 5 papers" are the same execution shape. RLE collapse keeps the cluster count manageable.

The cost: RLE means "loop ran 3 times" and "loop ran 30 times" look identical. There's no flag to disable it yet; if you need to distinguish, the per-span data is still in the spans table.

## OpenTelemetry ingestion

`agentlog.otel.AgentlogSpanExporter` implements the OTel exporter protocol (`export`, `shutdown`, `force_flush`) and writes received spans directly into the SQLite store. The mapping:

- OTel `trace_id` (16 bytes → 32 hex chars) → `traces.id`
- OTel `span_id` (8 bytes → 16 hex chars) → `spans.id`
- OTel `attributes` → `spans.attrs_json`
- OTel `gen_ai.*` / `llm.*` attributes → mapped onto agentlog's conventions so the cost module works
- OTel `events` with `name=exception` → `spans.error_type` / `error_message`
- OTel parent context → `spans.parent_id`

Why ingestion, not export? The clustering page only works if all your traces live in one store. We let users keep their existing OTel instrumentation and route it to us.

## Cost

`agentlog.cost.PRICING` is a dict keyed by model id mapping to `(input_per_million, output_per_million)` in USD. `estimate_span_cost(attrs)` looks up the model (exact match → date-suffix strip → longest-prefix match) and multiplies by the captured input/output token counts.

`cost.backfill()` walks every `kind='llm_call'` span, computes its cost, writes it to `spans.cost_usd`, then rolls up per trace into `traces.cost_usd`. The dashboard surfaces these in the recent-traces list, the trace detail page, the snapshot HTML, and the header.

Users can override or extend the pricing table at runtime via `$AGENTLOG_PRICING_JSON='{"some-model": [2.0, 10.0]}'`.

## Replay

`agentlog replay <trace_id> --entry module:function` imports the named entrypoint, reads the captured `args`/`kwargs` from the trace's root span input, and re-invokes the function. The new trace is tagged `replay_of=<original_id>` so the dashboard can pair them.

Limitations are intentional:

- The entrypoint module must be importable in the current Python env (you can't replay a trace from a different codebase without putting that code on the path).
- Non-JSON-serializable args (file handles, sockets) don't round-trip.
- Truncated root inputs (the `__truncated` marker) refuse to replay because the result wouldn't match the original.

Replay-with-modified-prompt is the natural extension — same machinery, but with a stage that lets you edit the captured kwargs before the re-invocation. v0.4 target.

## Wrappers

`wrap_anthropic` and `wrap_openai` are explicit: you pass an instance, you get a wrapped instance back. No global monkey-patching, no import-time side effects. The wrapper detects sync vs. async by class name (`AsyncAnthropic`, `AsyncOpenAI`) and exposes `.messages.create` (Anthropic) or `.chat.completions.create` (OpenAI). Both providers' Bedrock and Vertex clients (`AnthropicBedrock`, `AnthropicVertex`) work through `wrap_anthropic` for free because they expose the same `.messages.create` interface.

## Dashboard

FastAPI + vanilla HTML + small inline JS. No build step, no framework. Static files served from `agentlog/dashboard/static/`. Templates are Jinja2. The frontend uses `fetch()` for JSON endpoints and renders dynamically.

| Page | Purpose |
|---|---|
| `/` | recent traces with status/tag/name-search filters and live polling |
| `/clusters` | execution patterns, failure rates, common failure prefix |
| `/search` | FTS5 search over span I/O and errors |
| `/metrics` | per-metric aggregates and rolling-mean sparklines |
| `/failures` | per-span error-rate bars + force-directed call graph |
| `/trace/<id>` | Gantt timeline + expandable I/O + tags + metrics |

## What's intentionally absent

- **No auth.** Local-first means one user.
- **No retention policy.** Delete `~/.agentlog/traces.db` when you want to start fresh; export with `agentlog export --all` first if you want a backup.
- **No alerting.** Alerts belong with a production observability stack, not a debug tool.
- **No streaming chunk capture.** We log on completion. Streaming sequences add complexity that doesn't justify itself for debugging.
