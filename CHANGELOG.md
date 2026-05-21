# Changelog

All notable changes to clustertrace. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies.

> **Renamed from `agentlog` to `clustertrace` in v0.5.0** — PyPI's name-similarity check rejected `agentlog` as too close to the existing `agentlogger` package. The new name lands the differentiator (clustering of traces) more directly anyway.

## [0.5.1] — 2026-05-21

### Fixed (rigorous red-team pass found four real bugs)

1. **NaN / Infinity in trace I/O crashed `/api/trace/<id>` with a 500.** Python's `json.dumps` happily emitted non-standard `NaN` / `Infinity` literals which then crashed the dashboard's strict response encoder. `_dumps` now uses `allow_nan=False` and walks the payload to sanitize non-finite floats to `null` on the way in.
2. **Multi-threaded `storage.connect()` raced on `PRAGMA journal_mode=WAL`** with "database is locked." After the v0.4 connection pool change, each new thread's connection tried to set WAL while the other threads' pooled connections held shared locks. PRAGMA WAL needs an exclusive lock. Fix: set WAL once during `_ensure_initialized` under the global init lock; subsequent connections inherit WAL from the file header.
3. **Span names containing `|` or `:` corrupted cluster signature decoding.** The signature format `name:status|name:status|...` lost structure when names contained the separators — `|` produced 1-element pattern tuples; `:` folded the rest of the name into the status field. Both characters are now percent-encoded in signatures (`%7C`, `%3A`) with a matching decoder. `%` itself is escaped as `%25` for full round-trip safety.
4. **Opening a DB created by a newer clustertrace silently accepted it**, then the next write blew up with a cryptic SQLite error. Now: `_ensure_initialized` raises a clear `RuntimeError` naming the version mismatch and pointing the user at the fix.

### Added
- `tests/test_red_team.py` — 8 regression tests covering each bug above. Total suite: 95 → 105 tests.

### Not fixed (intentionally)
- `limit=0` on `/api/traces` returns 1 trace, not 0. The clamp `max(1, min(limit, 500))` is the intended UX (asking for "no results" via a parameter is unusual). Documented as a quirk.
- `CLI snapshot --out <path>` has no validation. By design — the user is choosing where to write on their own machine.
- Tag key/value have no length cap. The user is the only writer; no DoS surface.

## [0.5.0] — 2026-05-20

### Breaking
- **Package renamed `agentlog` → `clustertrace`.** Update your imports: `from agentlog import ...` → `from clustertrace import ...`. The decorator (`@clustertrace.trace`), wrappers (`clustertrace.wrap_anthropic`, `clustertrace.wrap_openai`), exporter (`ClustertraceSpanExporter`), and CLI (`clustertrace dashboard`, `clustertrace demo`) all use the new name.
- **Environment variables renamed**: `AGENTLOG_DB` → `CLUSTERTRACE_DB`, `AGENTLOG_MAX_PAYLOAD_BYTES` → `CLUSTERTRACE_MAX_PAYLOAD_BYTES`, `AGENTLOG_PRICING_JSON` → `CLUSTERTRACE_PRICING_JSON`, `AGENTLOG_SAMPLE_RATE` → `CLUSTERTRACE_SAMPLE_RATE`.
- **Default DB path** moved from `~/.agentlog/traces.db` to `~/.clustertrace/traces.db`. If you have existing traces, set `CLUSTERTRACE_DB=~/.agentlog/traces.db` or `mv ~/.agentlog ~/.clustertrace`.
- **Export format header key** changed from `_agentlog_header` to `_clustertrace_header`. The bundled demo dataset was re-exported under the new key. Old export files will fail to import — re-export from the old version if you need them.

### Why the rename
PyPI's ultranormalization rejected `agentlog` as too similar to the existing `agentlogger` (0.1.2) package. Rather than fight the similarity check, we picked `clustertrace` — a name that lands the actual differentiator (structural clustering of traces) more clearly. PyPI confirmed `clustertrace` is free.

### Same
Everything functional from v0.4.2 is unchanged — same `@trace` decorator API, same dashboard pages, same clustering algorithm, same OpenTelemetry exporter, same wrappers, same 95-test suite (still 95/95 passing under the new name).

## [0.4.2] — 2026-05-20

### Fixed
- **Windows cp1252 crash on `clustertrace demo`** — the CLI used Unicode arrows (`→`) and bullets (`·`) in its output, which crashed on a fresh Windows install where Python's stdout defaults to cp1252. Found in a first-5-minutes UX test. CLI output is now ASCII-only; regression test enforces this.

### Added
- **`publish.yml` workflow** — pushes a tag, builds the wheel + sdist, publishes to PyPI via OIDC trusted publisher (no API token stored). One-time setup at https://pypi.org/manage/account/publishing/ then `git push --tags` releases.
- **`release.yml` workflow** — same trigger creates a GitHub Release with notes auto-extracted from the matching `CHANGELOG.md` section.
- **`OUTREACH.md`** — researched, named-target list: 6 awesome-lists with categories, 9 AI infra bloggers/outlets with specific pitch angles, 9 Slack/Discord communities with channel names, named individuals worth engaging.
- **README hero restructured** to match the patterns of higher-star competitors (Langfuse 27k, Phoenix 9.8k, Helicone 5.7k): centered banner image, tagline, badges row including PyPI; install command before features.

### Improved
- `LAUNCH.md` updated with researched data: 605-post Show HN survival study (1% survive 7 days on front page), 23,000-post timing analysis confirming weekday US morning slots, plus the contrarian Sunday-late-evening option. Specific times now grounded, not generic.

## [0.4.1] — 2026-05-20

Launch-readiness release. Code is stable from 0.4.0; new content + integration examples.

### Added
- `examples/langchain_example.py` — five-line OpenTelemetry path showing LangChain → clustertrace.
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
- **Auto-cost** — `finish_span` now estimates and stores `cost_usd` automatically for any span with a known LLM model + token counts. `finish_trace` rolls up the per-span costs into the trace's `cost_usd`. No more manual `clustertrace backfill-cost`.
- **`@trace(sample=0.1)` + `@trace(skip=True)`** — production-grade sampling. `skip=True` returns the original function unwrapped (zero overhead). `sample` accepts a float in (0, 1] or reads `$CLUSTERTRACE_SAMPLE_RATE`. Sampling is bypassed inside an active trace so child spans are always recorded.
- **`clustertrace.flush()` + `atexit` hook** — orphan `running` traces from crashes/kills are cleaned up automatically on process exit.
- **`clustertrace cleanup --stale-after 5m`** — explicit CLI for the same.
- **`clustertrace vacuum --older-than 30d [--dry-run]`** — retention policy. `ON DELETE CASCADE` removes spans, tags, metrics; `VACUUM` reclaims disk space.
- **Per-tag failure-prefix mining** — `failure_summary(group_by_tag='agent')` returns a separate longest-common-prefix per tag value. When you have multiple agents in the same DB, the global prefix is empty but the per-tag ones are diagnostic. The `/clusters` page shows them as labelled chip rows.
- **Streaming-aware attrs** — when you call `messages.create(stream=True)` or `chat.completions.create(stream=True)`, the span's attrs include `streaming: true` so you can filter or cluster on it. (Full chunk-by-chunk capture is v0.5.)
- **Versioned JSON exports** — `clustertrace export` emits a header line with `clustertrace_version` and `export_format_version`. `clustertrace import` refuses streams from a newer format than it supports.

### Fixed
- **OTel ingestion**: child-span errors now propagate to trace-level status. Previously OTel-ingested traces where a child errored but the root ended OK were silently classified as successful and excluded from the failure clusters.

### Tests
- 94 tests (was 75). New suite `tests/test_v04_features.py` covers connection pooling, auto-cost, set-mode clustering, sampling, per-tag prefix, streaming attrs, flush + cleanup, vacuum + duration parsing, versioned export.
- 88% line coverage maintained. Pyright clean. Ruff clean.

## [0.3.1] — 2026-05-20

### Added
- `clustertrace demo` command — zero-config trial: imports 60 bundled traces and launches the dashboard. No API key needed.
- 60-trace `demo-traces.jsonl` bundled in the wheel (3 agent topologies, real failure clustering).
- Empty-state guidance on the dashboard when there are no traces: `clustertrace demo`, instrument-your-code snippet, and OpenTelemetry one-liner.
- `py.typed` marker so type checkers respect clustertrace's type hints.
- GitHub Actions CI on push and PR — Python 3.11/3.12/3.13 × ubuntu/macOS/windows.
- Issue and PR templates.
- `CHANGELOG.md`, `SECURITY.md`.

### Documentation
- README hero ASCII cluster view + badges + comparison table + FAQ.
- Install-from-git instructions for the pre-PyPI window.

## [0.3.0] — 2026-05-20

### Added
- **OpenTelemetry exporter** (`clustertrace.otel.ClustertraceSpanExporter`) — any OTel-instrumented app pipes spans into clustertrace with one line. Maps `gen_ai.*` and `llm.*` attribute conventions.
- **Cost tracking** — pricing table for Anthropic, OpenAI, Gemini with date-suffix stripping. `$CLUSTERTRACE_PRICING_JSON` override. Cached per span + rolled up per trace.
- **Replay** — `clustertrace replay <id> --entry mod:fn` re-runs a trace with captured args. New trace tagged `replay_of=<original>`.
- **FTS5 search** across span name + input + output + error_message. Supports phrases, OR, NEAR. `/search` page.
- **`clustertrace.metric(name, value)`** — numeric metrics per trace, time-series sparkline charts on `/metrics`.
- **JSONL export/import** — `clustertrace export [--all]` and `clustertrace import`.
- **Self-contained HTML snapshots** — `clustertrace snapshot <id>` produces a single shareable file with no external assets.
- Schema migration v3: `trace_metrics` table, `cost_usd` columns, `spans_fts` virtual table with sync triggers.
- `ARCHITECTURE.md` (design choices and trade-offs), `CONTRIBUTING.md` (how to add an SDK wrapper).

### Changed
- README leads with the differentiator + cost + OTel + replay.

## [0.2.0] — 2026-05-20

### Added
- **Trace clustering by structural signature** — RLE-collapsed sequence of `(name, status)` pairs. `/clusters` page.
- **Longest common failure prefix** mining across failed traces.
- `clustertrace.tag(key, value)` and `@trace(tags={...})` for filterable tags.
- `wrap_openai` for sync + async OpenAI clients.
- Filter UI on the recent-traces page (status, tag, name search) with live polling.
- Per-field payload truncation (`CLUSTERTRACE_MAX_PAYLOAD_BYTES`, default 32 KB).
- Real schema migration runner (`schema_meta.version` + ordered DDL list). v1 → v2 adds `trace_tags`, `traces.signature`.

## [0.1.0] — 2026-05-20

### Added
- Initial release.
- `@clustertrace.trace` decorator (sync + async), `clustertrace.span(...)`, `clustertrace.tool_call(...)`.
- `wrap_anthropic` for sync + async Anthropic clients (works with `AnthropicBedrock` and `AnthropicVertex` via the shared `.messages.create` interface).
- SQLite storage at `~/.clustertrace/traces.db`, `CLUSTERTRACE_DB` override, WAL mode.
- FastAPI dashboard on port 7777: recent traces, trace timeline, failure-cluster visualization (per-span error rate + step-of-failure histogram + force-directed call graph).
- 18 tests.
