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
- 2026-05-20 — v0.5.0: **renamed `agentlog` → `clustertrace`** because PyPI's similarity check rejected `agentlog` (too close to existing `agentlogger`). 551 substitutions across 64 files via deliberate three-case-sensitive replacement (AGENTLOG/Agentlog/agentlog). Env vars, class names, CLI, default DB path, export header key all updated. PyPI confirms `clustertrace` is free. 95/95 tests still pass. ruff + pyright clean. Tagged v0.5.0.






