# Changelog

All notable changes to agentlog. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies.

## [0.4.1] — 2026-05-20

Launch-readiness release. Code is stable from 0.4.0; new content + integration examples.

### Added
- `examples/langchain_example.py` — five-line OpenTelemetry path showing LangChain → agentlog.
- `examples/llamaindex_example.py` — same for LlamaIndex.
- `examples/benchmark.py` — overhead-per-trace measurements; prints a Markdown table you can paste.
- `docs/hero.svg` — inline-renderable hero image for the README (no external host).
- `LAUNCH.md` — internal launch playbook with Show HN draft, Twitter thread, outreach templates.
- Pinned `good first issue` templates for `wrap_bedrock`, `wrap_gemini`, streaming-chunk capture, and tree-edit-distance clustering.
- "Overhead" section in the README with honest numbers and the sampling/skip escape hatches.

## [0.4.0] — 2026-05-20

### Performance
- **Thread-local connection pool** — 200 concurrent async traces now finish in ~9s instead of ~55s (6× speedup). Previously each storage helper opened a fresh SQLite connection with PRAGMA setup; now the connection is reused within a thread.

### Added
- **`signature_for_spans(mode='set')`** — reorder-insensitive cluster signature. `A→B` and `B→A` collapse to one cluster. Dashboard `/clusters` page now has a mode toggle.
- **Pagination on `/api/clusters`** — `limit` + `offset` parameters; "Load more" button on the page.
- **Auto-cost** — `finish_span` now estimates and stores `cost_usd` automatically for any span with a known LLM model + token counts. `finish_trace` rolls up the per-span costs into the trace's `cost_usd`. No more manual `agentlog backfill-cost`.
- **`@trace(sample=0.1)` + `@trace(skip=True)`** — production-grade sampling. `skip=True` returns the original function unwrapped (zero overhead). `sample` accepts a float in (0, 1] or reads `$AGENTLOG_SAMPLE_RATE`. Sampling is bypassed inside an active trace so child spans are always recorded.
- **`agentlog.flush()` + `atexit` hook** — orphan `running` traces from crashes/kills are cleaned up automatically on process exit.
- **`agentlog cleanup --stale-after 5m`** — explicit CLI for the same.
- **`agentlog vacuum --older-than 30d [--dry-run]`** — retention policy. `ON DELETE CASCADE` removes spans, tags, metrics; `VACUUM` reclaims disk space.
- **Per-tag failure-prefix mining** — `failure_summary(group_by_tag='agent')` returns a separate longest-common-prefix per tag value. When you have multiple agents in the same DB, the global prefix is empty but the per-tag ones are diagnostic. The `/clusters` page shows them as labelled chip rows.
- **Streaming-aware attrs** — when you call `messages.create(stream=True)` or `chat.completions.create(stream=True)`, the span's attrs include `streaming: true` so you can filter or cluster on it. (Full chunk-by-chunk capture is v0.5.)
- **Versioned JSON exports** — `agentlog export` emits a header line with `agentlog_version` and `export_format_version`. `agentlog import` refuses streams from a newer format than it supports.

### Fixed
- **OTel ingestion**: child-span errors now propagate to trace-level status. Previously OTel-ingested traces where a child errored but the root ended OK were silently classified as successful and excluded from the failure clusters.

### Tests
- 94 tests (was 75). New suite `tests/test_v04_features.py` covers connection pooling, auto-cost, set-mode clustering, sampling, per-tag prefix, streaming attrs, flush + cleanup, vacuum + duration parsing, versioned export.
- 88% line coverage maintained. Pyright clean. Ruff clean.

## [0.3.1] — 2026-05-20

### Added
- `agentlog demo` command — zero-config trial: imports 60 bundled traces and launches the dashboard. No API key needed.
- 60-trace `demo-traces.jsonl` bundled in the wheel (3 agent topologies, real failure clustering).
- Empty-state guidance on the dashboard when there are no traces: `agentlog demo`, instrument-your-code snippet, and OpenTelemetry one-liner.
- `py.typed` marker so type checkers respect agentlog's type hints.
- GitHub Actions CI on push and PR — Python 3.11/3.12/3.13 × ubuntu/macOS/windows.
- Issue and PR templates.
- `CHANGELOG.md`, `SECURITY.md`.

### Documentation
- README hero ASCII cluster view + badges + comparison table + FAQ.
- Install-from-git instructions for the pre-PyPI window.

## [0.3.0] — 2026-05-20

### Added
- **OpenTelemetry exporter** (`agentlog.otel.AgentlogSpanExporter`) — any OTel-instrumented app pipes spans into agentlog with one line. Maps `gen_ai.*` and `llm.*` attribute conventions.
- **Cost tracking** — pricing table for Anthropic, OpenAI, Gemini with date-suffix stripping. `$AGENTLOG_PRICING_JSON` override. Cached per span + rolled up per trace.
- **Replay** — `agentlog replay <id> --entry mod:fn` re-runs a trace with captured args. New trace tagged `replay_of=<original>`.
- **FTS5 search** across span name + input + output + error_message. Supports phrases, OR, NEAR. `/search` page.
- **`agentlog.metric(name, value)`** — numeric metrics per trace, time-series sparkline charts on `/metrics`.
- **JSONL export/import** — `agentlog export [--all]` and `agentlog import`.
- **Self-contained HTML snapshots** — `agentlog snapshot <id>` produces a single shareable file with no external assets.
- Schema migration v3: `trace_metrics` table, `cost_usd` columns, `spans_fts` virtual table with sync triggers.
- `ARCHITECTURE.md` (design choices and trade-offs), `CONTRIBUTING.md` (how to add an SDK wrapper).

### Changed
- README leads with the differentiator + cost + OTel + replay.

## [0.2.0] — 2026-05-20

### Added
- **Trace clustering by structural signature** — RLE-collapsed sequence of `(name, status)` pairs. `/clusters` page.
- **Longest common failure prefix** mining across failed traces.
- `agentlog.tag(key, value)` and `@trace(tags={...})` for filterable tags.
- `wrap_openai` for sync + async OpenAI clients.
- Filter UI on the recent-traces page (status, tag, name search) with live polling.
- Per-field payload truncation (`AGENTLOG_MAX_PAYLOAD_BYTES`, default 32 KB).
- Real schema migration runner (`schema_meta.version` + ordered DDL list). v1 → v2 adds `trace_tags`, `traces.signature`.

## [0.1.0] — 2026-05-20

### Added
- Initial release.
- `@agentlog.trace` decorator (sync + async), `agentlog.span(...)`, `agentlog.tool_call(...)`.
- `wrap_anthropic` for sync + async Anthropic clients (works with `AnthropicBedrock` and `AnthropicVertex` via the shared `.messages.create` interface).
- SQLite storage at `~/.agentlog/traces.db`, `AGENTLOG_DB` override, WAL mode.
- FastAPI dashboard on port 7777: recent traces, trace timeline, failure-cluster visualization (per-span error rate + step-of-failure histogram + force-directed call graph).
- 18 tests.
