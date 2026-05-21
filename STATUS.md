# STATUS

Append-only log so progress is visible at a glance.

- 2026-05-20 — phase: skeleton + tests green (18/18). next: generate demo traces, smoke dashboard, README pass.
- 2026-05-20 — phase: demo data generated (60 runs, 58 ok / 2 trace-fail, 131/1441 spans errored). verify_claim 29% error, fetch_paper 18% error — the failure-cluster viz tells a clear story. next: ruff clean, v0.1.0 tag.
- 2026-05-20 — phase: ruff clean, dashboard fail-step counter fixed to ignore @trace root span, tagged v0.1.0.
- 2026-05-20 — v0.2 work: schema migration runner, clustertrace.tag + @trace(tags=...), payload truncation, structural-signature clustering with RLE collapse + failure-prefix mining, /clusters page, wrap_openai sync+async, filter UI + live polling. 46/46 tests passing.
- 2026-05-20 — v0.2 demo: 3 agents (research, rag, tool_use) × 80 runs = 246 traces, 29 distinct clusters, 47 failures (19%). Top 2 clusters explain 87% of failures — quoted as the README insight.
- 2026-05-20 — tagged v0.2.0.
- 2026-05-20 — v0.3 work: schema v3 (FTS5 + trace_metrics + cost columns), cost module with pricing table, OpenTelemetry SpanExporter, clustertrace.metric() API + sparkline charts, FTS5 search, JSONL export/import, self-contained HTML snapshot, replay CLI. ARCHITECTURE.md + CONTRIBUTING.md added.
- 2026-05-20 — v0.3 tests: 74/74 passing (added cost, metric, search, otel, export/snapshot/replay).
- 2026-05-20 — tagged v0.3.0.
- 2026-05-20 — v0.3.1: adoption blockers fixed. Bundled 60-trace demo dataset; `clustertrace demo` command for zero-config trial; empty-state guidance on dashboard; GitHub Actions CI (3 OSes × 3 Python versions); CHANGELOG, SECURITY, issue/PR templates; py.typed marker; README rewrite with badges, ASCII cluster viz, comparison table to Langfuse/Phoenix/LangSmith, FAQ. Tagged v0.3.1.
- 2026-05-20 — v0.4: every weakness from the critical analysis fixed. Connection pool (200 traces 55s→9s, 6× faster). Auto-cost on finalize. Reorder-insensitive `mode='set'` clustering + dashboard toggle. `@trace(sample, skip)` + `CLUSTERTRACE_SAMPLE_RATE`. `clustertrace.flush()` + atexit cleanup. `clustertrace cleanup` and `clustertrace vacuum` CLIs. Per-tag failure-prefix mining. Streaming-aware attrs. Versioned JSONL exports. Clusters pagination. 94 tests passing, 88% coverage, pyright clean, ruff clean. Tagged v0.4.0.
- 2026-05-20 — v0.4.1: launch infrastructure. LangChain + LlamaIndex examples, benchmark suite (35µs Python overhead per @trace, 5ms SQLite write on Linux / ~30ms on Windows NTFS), docs/hero.svg embedded in README, LAUNCH.md with Show HN draft, 4 pinned good-first-issues for contribution opportunities. Tagged v0.4.1.
- 2026-05-20 — v0.4.2: launch-readiness — PyPI OIDC trusted-publisher workflow (publish.yml) and GitHub Release auto-creation (release.yml) so `git push --tags` does everything. README hero restructured to match high-star OSS patterns (Langfuse/Phoenix/Helicone). OUTREACH.md with researched, named targets. First-5-min UX test caught a real Windows cp1252 crash on `clustertrace demo` — fixed + regression test (cli output is cp1252-safe). 95 tests passing. Tagged v0.4.2.
- 2026-05-20 — v0.5.0: **renamed `agentlog` → `clustertrace`** because PyPI's similarity check rejected `agentlog` (too close to existing `agentlogger`). 551 substitutions across 64 files via deliberate three-case-sensitive replacement (AGENTLOG/Agentlog/agentlog). Env vars, class names, CLI, default DB path, export header key all updated. PyPI confirms `clustertrace` is free. 95/95 tests still pass. ruff + pyright clean. Tagged v0.5.0. **Published to PyPI** via OIDC workflow.
- 2026-05-21 — v0.5.1: rigorous red-team pass found 4 real bugs and fixed all: (1) NaN/Inf in trace I/O crashed the dashboard 500; (2) multi-threaded `storage.connect()` raced on PRAGMA WAL with "database is locked"; (3) `|`/`:` in span names corrupted cluster signature decoding; (4) future-version DB silently accepted then writes crashed. Each has a regression test. 105/105 tests, ruff + pyright clean. Tagged v0.5.1.
- 2026-05-21 — v0.6.0 phase 1 (a): tree-edit-distance clustering. `signature_for_spans(..., mode='tree_edit')` keeps the full token sequence (no RLE); `list_clusters(mode='tree_edit')` runs Wagner-Fischer with an early-exit max_distance bound and assigns traces to the closest canonical within `max(2, 0.1 × median len)` threshold; `set_tree_edit_threshold(N)` overrides. Dashboard `/clusters` toggle gains a third option; `/api/clusters?mode=tree_edit&threshold=N` works. 14 new tests, 1000-trace perf budget verified (~well under 5s). 119/119 passing.
- 2026-05-21 — v0.6.0 phase 1 (b): drift detection. New `clustertrace.drift.compute_drift(window_s, compare_s)` reads two adjacent time windows, computes per-signature failure-rate change, filters `current_n < 3` and zero-deltas, sorts by `abs(delta)` desc. New endpoint `GET /api/cluster-drift?window=24h&compare=24h` (parses durations via `maintenance.parse_duration`); 400 on bad input. New `/drift` dashboard page with before/after rates and red ↑ / green ↓ arrows; navbar entry between Clusters and Search. 11 new tests including demo-data verification. 130/130 passing.
- 2026-05-21 — v0.6.0 phase 1 (c): auto-repro CLI. `clustertrace repro <sig_hash_or_trace_id> --entry mod:fn` emits a pytest file to stdout (or `--out path`); `--mode positive|negative` controls whether the test asserts no-raise or asserts the original error type fires. Sig_hash lookup picks the most recent failing trace as the seed. JSON-safe args are inlined as Python literals; truncated inputs become a TODO comment. 13 new tests including a generated-source compile check and demo-data run. 143/143 passing, ruff + pyright clean.







