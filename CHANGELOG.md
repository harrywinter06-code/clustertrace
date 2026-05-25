# Changelog

All notable changes to clustertrace. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semver applies.

> **Renamed from `agentlog` to `clustertrace` in v0.5.0** — PyPI's name-similarity check rejected `agentlog` as too close to the existing `agentlogger` package. The new name lands the differentiator (clustering of traces) more directly anyway.

## [0.10.0] — 2026-05-25

### Added — Claude Code integration

The dashboard's existing OTLP/JSON receiver (`POST /v1/traces`, shipped in 0.9.1) now has a one-command setup helper for Claude Code's OpenTelemetry exporter:

```bash
clustertrace claude-code               # prints the 5 env vars to paste into your shell
clustertrace claude-code --content     # also include the optional content-gating vars
clustertrace dashboard                 # receive
```

Auto-detects PowerShell on Windows, bash elsewhere. The shell can be overridden with `--shell {bash,powershell}`.

Every `claude_code.interaction` span becomes a trace; child `claude_code.llm_request` and `claude_code.tool` spans land as nested function calls with model, input/output tokens, cache hits, stop reason, tool name, and duration. README has a new "Use with Claude Code" section that links the same flow.

## [0.9.1] — 2026-05-23

### Changed — dashboard UX

The dashboard now lands on the failure-pattern view, not the flat trace list.
This is the actual pitch of the project (group failing runs by execution
pattern), and the prior landing page contradicted it by showing a generic
trace table on page load.

- Route swap: `/` now serves the patterns view; the flat trace list moved to `/traces`.
- New hero block on `/` explaining what clustertrace is in plain language.
- Section headers re-worded for non-power-users: "Execution clusters" → "Failure patterns", "Top failing nodes" → "Functions involved in failures most often", "Common failure prefix" → "Where failures share a path", "Clusters by frequency" → "Patterns by frequency".
- Pattern-matching mode picker (`ordered` / `set` / `tree_edit`) collapsed into an "Advanced" `<details>` disclosure so the default view stays clean.
- Header stats: `spans` relabeled to `function calls`.
- Nav trimmed to three primary items (`Patterns · Traces · Search`); `Drift · Metrics · Failure graph` demoted to a dimmed secondary group.

## [0.9.0] — 2026-05-22

### Added — Phase 4: IDE-native

Two surfaces that bring clustertrace into the editor:

#### `clustertrace mcp` — Model Context Protocol server

Exposes clustertrace's data model as tools to any MCP-capable AI editor — Claude Code, Cursor, Continue. "Show me a failing trace of this pattern" or "diff this trace against a successful one" is now a single AI assistant command.

Six read-only tools:

- `list_clusters(limit, mode, threshold)` — distinct execution patterns with counts + failure rate
- `get_trace(trace_id)` — full record (trace + spans + tags)
- `search(query, limit)` — FTS5 search over span name + I/O + error messages
- `failure_summary(group_by_tag)` — aggregate failure-pattern view
- `recent_failed(limit)` — N most recent traces with status=error
- `compare_traces(a_trace_id, b_trace_id)` — Wagner-Fischer edit script between two traces (most useful with `cluster_drift` output)

`clustertrace mcp install --target {claude-code|cursor|continue}` merges into the editor's MCP config with a timestamped backup. Without `--target`, prints the JSON snippet to paste manually. Optional install: `pip install "clustertrace[mcp]"`. v0.9 ships stdio transport only; HTTP arrives when there's a concrete client that needs it. Read-only by design — mutation tools (annotate/assert) land in v1.0 after we see how the read-only surface gets used.

#### `clustertrace inspect <trace_id>` — terminal TUI

Renders one trace as a `rich`-formatted header + ASCII Gantt + nested span tree. Fully offline (pure SQLite reads). Good for SSH'd-in debugging where you can't pop open a browser.

- `clustertrace inspect <id>` — by id
- `--latest` — most recent trace
- `--failed` — most recent failed trace
- `--expand <span_id>` (repeatable) — dump that span's input/output/attrs
- `--no-color` and `--width` for piping and snapshot tests

Status icons: `✓` ok / `✗` error / `◌` running. Error rows surface `error_type: message` under the node. Renderer's column math is bounded — output never exceeds the requested width (verified at 80 and 200 cols).

### Dependencies
- `rich>=13` is now a hard dep (needed for `inspect`, small and useful)
- `mcp>=1.0` is an optional extra (`clustertrace[mcp]`)

### Testing
- 32 new MCP tests (schema coverage, dispatch, every tool's shape, config-file merge + backup + idempotency, real-runtime server construction)
- 15 new inspect tests (resolver paths, width invariants, --expand JSON dump, CLI surface)
- Total suite 174 → 221 passing.

## [0.8.0] — 2026-05-22

### Added — Phase 3: eval loop

Three surfaces that turn the cluster view into something you can write rules against, not just stare at.

1. **LLM-as-judge on cluster representatives.** New `clustertrace.judge` module: `JudgeVerdict` dataclass, `no_exceptions_evaluator` (free, default), `llm_judge_evaluator(rubric, model=...)` (Anthropic SDK, defers API-key check to call time). `evaluate_cluster(sig_hash, evaluator, n_samples=3)` and `evaluate_all_clusters(...)` with deterministic sampling per-sig_hash so reports are reproducible. CLI: `clustertrace judge [--rubric <text>] [--samples 3] [--max-cost-usd 0.50] [--model claude-haiku-4-5-20251001]` emits a markdown report. Cost-cap enforced *before* any LLM calls; verified on demo data ($0.0001 cap aborts cleanly). Dashboard `/api/clusters` attaches `latest_judgment`.
2. **Cluster annotations.** `clustertrace.annotate_cluster(sig_hash, status=..., note=..., tag=...)` with statuses `expected-failure` / `wontfix` / `priority` / `acceptable`; 4096-char note cap; append-deduped tags. Storage is keyed on `sig_hash` so annotations survive `clustertrace vacuum`. CLI: `clustertrace annotate <sig_hash> --status ... --note ... --tag ...` (`--status clear` removes). `/clusters` cards show an annotation badge with an inline editor (POST `/api/cluster-annotations`). `/api/failure-summary` drops `expected-failure` clusters from the headline count with a `+N expected, hidden` footnote.
3. **Cluster pass/fail assertions + `clustertrace check` CLI.** Four rule kinds: `success_rate_above`, `avg_latency_below`, `avg_cost_below`, `no_new_traces`. `over_last` window in `traces` (count) or `seconds` (time). `clustertrace assert <sig_hash> --success-rate-above 0.9 --over-last 100` persists one rule per invocation; `clustertrace check` evaluates all of them and exits 0/1. `--format json` emits a CI-consumable payload (`{"passed": bool, "n_assertions": N, "n_failed": N, "results": [...]}`). By default, assertions on `expected-failure` clusters don't tank the exit code; `--include-expected-failures` overrides.

### Schema
Schema v4 migration adds `cluster_judgments`, `cluster_annotations`, `cluster_assertions`. Idempotent, runs once per DB.

### Testing
- 50 new tests across judge / annotations / assertions / check CLI. Total suite 174 → 224 passing.

## [0.7.1] — 2026-05-21

### Fixed (rigorous red-team pass found three real bugs)

1. **Race condition in `/api/clusters?mode=tree_edit&threshold=N`.** The dashboard handler mutated `cluster._tree_edit_threshold_override` (a module-level global) before each call and reset it in `finally`. Two concurrent FastAPI requests could race: A's threshold leaks into B's response, B's finally then clobbers A's setting before A reads. Fix routes `threshold` through `list_clusters(..., threshold=N)` directly; the global remains for non-API callers (notebooks/scripts) but the request path no longer touches shared state.

2. **Code injection in `clustertrace repro --mode negative`.** The generated pytest source inlined the DB-stored `error_type` raw into `pytest.raises({err_name})` and the docstring header. An attacker who could write to the trace DB — directly, via the new `/v1/traces` OTLP endpoint, or via a malformed importer — could land arbitrary Python in the generated file, which executes when the user runs the generated test. Fix validates `error_type` against `^[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*$` (falls back to `Exception` on non-match) and replaces the docstring header with `#` comment lines so a smuggled `"""` cannot break out of the string literal. AST + tokenize-based regression test asserts the dangerous payload never lands in executable code.

3. **Unbounded body on `POST /v1/traces`.** The OTLP/JSON endpoint called `request.json()` with no size cap. A multi-GB body OOMs the dashboard process (and the OTLP endpoint is the *one* surface explicitly designed to accept remote agent traffic, including from browsers via the new TS SDK). Fix reads the body in two phases: an upfront `Content-Length` check rejects oversize before buffering, and a post-read length check catches chunked clients that omitted/lied about the header. Both return HTTP 413. Cap defaults to 16 MiB; override via `CLUSTERTRACE_OTLP_MAX_BYTES`.

Each has a regression test that fails on the unpatched code. 174/174 tests passing, ruff + pyright clean.

## [0.7.0] — 2026-05-21

### Added — Phase 2: competitor ingest

`clustertrace import --from <source>` accepts span/trace exports from the four common competitor formats. All importers are stdlib-only, idempotent, and prefix IDs with the source name so cross-tool UUID collisions are impossible.

- **langfuse** — Langfuse JSON/JSONL exports. `observation.type=GENERATION` → `llm_call`, `=SPAN` → `function`. Usage block normalized onto `model`/`input_tokens`/`output_tokens`. Trace tags + metadata preserved as tags; `observation.level=ERROR` propagates to trace status.
- **phoenix** — Arize Phoenix / OpenInference span exports (JSON envelope, JSONL, or bare span list). OpenInference `span.kind` maps to `llm_call`/`tool_call`/`function`; falls back to `llm.*`/`tool.*` attr sniffing. `llm.model_name`/`gen_ai.request.model` → `attrs[model]`. Surfaces `session.id`/`user.id` as trace tags. Two-pass finalize so child errors that arrive after the root flag the trace failed.
- **langsmith** — LangSmith run exports (JSON envelope, JSONL, or single run). `run_type=llm`/`tool`/`chain`/`agent` mapping; model extracted from `extra.invocation_params`, `extra.metadata.ls_model_name`, or `serialized.name`. Top-level + nested `outputs.llm_output.token_usage` token counts. Orphaned child runs become their own self-rooted trace rather than being dropped.
- **otel (OTLP/JSON)** — canonical OTLP/JSON envelope (`resourceSpans[].scopeSpans[].spans[]`) plus `{"spans":[...]}` and bare-span-dict shapes; both camelCase and snake_case keys. Flattens OTLP attribute encoding. Reuses the same `gen_ai.*`/`llm.*`/`tool.*` mapping table as the native `ClustertraceSpanExporter` so dashboard treatment matches. OTLP/protobuf reserved for the optional extra `clustertrace[otel-import]`.

### CLI

`clustertrace import --from <langfuse|phoenix|langsmith|otel|native> [--file PATH]` (stdin default). Native JSONL passthrough preserved as the default for existing `clustertrace export | clustertrace import` flows.

### Testing
- 15 new tests; total suite 105 → 120 passing.

## [0.6.0] — 2026-05-21

### Added — Phase 1: cluster depth

Three additions that turn the cluster view from "interesting list" into "where you go when something broke after your last deploy."

1. **Tree-edit-distance clustering** — `mode='tree_edit'` groups traces whose `(name, status)` token sequences differ by ≤ `max(2, 0.1 × median length)` Wagner-Fischer edit operations. One extra retry or a single reordering no longer splits a cluster. Threshold is configurable via `clustertrace.cluster.set_tree_edit_threshold(N)` or the new `threshold=` query param on `/api/clusters`. Dashboard `/clusters` toggle gains a third option. Wagner-Fischer uses a `max_distance` early-exit bound so the inner loop short-circuits as soon as a candidate canonical is provably too far; 1,000 traces cluster in well under the 5s wall budget.
2. **Drift detection** — new `GET /api/cluster-drift?window=24h&compare=24h` returns each cluster's failure-rate change between two adjacent time windows. Sorted by `abs(delta)` desc; clusters with `<3` traces in the current window are filtered out as noise. New `/drift` dashboard page with before/after rates and direction arrows. Reuses the existing `signature` column — no schema migration.
3. **Auto-repro CLI** — `clustertrace repro <sig_hash_or_trace_id> --entry mod:fn [--mode positive|negative] [--out path]` emits a pytest file that imports the entrypoint, inlines the captured args/kwargs as Python literals, and asserts either no-raise (positive) or the original error type (negative). For a sig_hash, the most recent failing trace seeds. Truncated root inputs are flagged with a TODO comment instead of fabricated args.

### Testing
- 38 new tests across `tests/test_tree_edit.py`, `tests/test_drift.py`, `tests/test_repro.py`. Total suite: 105 → 143.

### No schema migration
Drift and tree-edit operate on the existing `traces.signature` column. Repro needs no schema.

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
