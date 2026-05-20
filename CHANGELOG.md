# Changelog

All notable changes to agentlog. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies.

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
