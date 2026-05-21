# Phase 2 — Competitor ingest

> **Self-contained brief.** A Claude Code session reading just this file should have everything needed to start building.

## What this is

Import traces from competing observability tools into clustertrace's SQLite store. After this phase:

```bash
clustertrace import --from langfuse  < langfuse-export.json
clustertrace import --from phoenix   < phoenix-spans.jsonl
clustertrace import --from langsmith < langsmith-export.json
clustertrace import --from otel      < otlp-spans.json
```

Each command parses the source format, maps it to clustertrace's schema, and yields cluster-page diagnostics on data that came from a different tool.

## Why it exists

This is the **parasite strategy** that makes the project addressable to every user of every competitor in the space. Without it, clustertrace is yet-another-tracer. With it, anyone already using Langfuse can get the cluster page in 60 seconds with `langfuse export | clustertrace import --from langfuse`. We never compete head-on; we plug in.

## Pre-flight checks

1. **Read the existing import path** — `src/clustertrace/export.py`. Note the JSONL format, the header line, and how `import_lines` skips header + reuses storage.insert_trace / insert_span / set_metric. Mirror the same boundaries.
2. **Confirm the OTel ingestion already works** — `src/clustertrace/otel.py` has `ClustertraceSpanExporter`. The competitor importers should reuse the same mapping logic for OTel-shaped data (gen_ai.* attrs).
3. **Pull one real example of each format**. You don't have access to all of these — collect from public docs and tests:
   - Langfuse: https://langfuse.com/docs/data-platform (their JSON shape)
   - Phoenix: https://docs.arize.com/phoenix/api/openinference-tracing
   - LangSmith: https://docs.smith.langchain.com (their export format)
   - OTel: https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/trace/v1/trace.proto (JSON encoding per OTLP spec)
   If you can't find a real example, write a fake-but-correct one in tests/fixtures/ based on the docs.

## What ships (binary, all must pass)

### Importer module structure

- [ ] New package `src/clustertrace/importers/` containing one module per source:
  - `langfuse.py` — `import_langfuse(stream) -> tuple[int, int]`
  - `phoenix.py` — `import_phoenix(stream) -> tuple[int, int]`
  - `langsmith.py` — `import_langsmith(stream) -> tuple[int, int]`
  - `otel.py` — `import_otlp(stream) -> tuple[int, int]` (different from `clustertrace/otel.py` which is the *exporter*)
- [ ] Each importer takes an iterable of lines OR a file-like; returns `(imported, skipped)` same as `export.import_lines`
- [ ] Each importer reuses `storage.insert_trace`, `storage.insert_span`, `storage.add_trace_tag`, `storage.set_metric` — do not write SQL directly in importers
- [ ] Each importer maps source-specific token-count / model / cost attributes onto clustertrace's standard attrs:
  - `attrs["model"]`, `attrs["input_tokens"]`, `attrs["output_tokens"]`, `attrs["streaming"]`

### Format mapping rules (per source)

- [ ] **Langfuse**: `trace.id` → traces.id; `observations[i].type == "GENERATION"` → kind=`llm_call`; `observations[i].type == "SPAN"` → kind=`function`; usage block → attrs token counts; tags → trace_tags
- [ ] **Phoenix / OpenInference**: OpenInference span attributes follow `llm.*` / `gen_ai.*` conventions already supported by `clustertrace/otel.py`. Reuse the mapping table.
- [ ] **LangSmith**: `run.id` → traces.id; `run_type == "llm"` → kind=`llm_call`; `inputs` / `outputs` → input_json / output_json; `tags` → trace_tags
- [ ] **OTel OTLP/JSON**: standard `resource_spans[].scope_spans[].spans[]` shape; map `name`, `attributes`, `events`, `status` exactly as `ClustertraceSpanExporter` does today

### CLI

- [ ] `clustertrace import --from <name>` reads stdin by default
- [ ] `clustertrace import --from <name> --file <path>` reads from a file
- [ ] Available sources listed by `clustertrace import --help` (one line per source)
- [ ] Unknown source → exit code 2 with `unknown source: <name>; supported: langfuse, phoenix, langsmith, otel`
- [ ] After import, prints `imported N traces, skipped M lines`

### Tests

- [ ] One fixture file per importer in `tests/fixtures/import_*.json[l]` with a representative payload
- [ ] One test per importer that loads the fixture, runs the importer, and verifies:
  - Correct number of traces inserted
  - At least one span has the right `kind` (llm_call vs function)
  - At least one trace has the right cost-relevant attrs (model + tokens)
  - Tags survive
- [ ] One integration test: end-to-end `clustertrace import --from <X>` followed by `clustertrace stats` showing the expected count

## Hard rules

- **Importers are append-only.** Do not delete or modify existing traces. If a source trace ID collides with an existing clustertrace trace ID, skip and count as `skipped` (don't crash).
- **No new dependencies** unless the source format genuinely requires one. OTLP protobuf decoding would need `opentelemetry-proto` — make that an optional extra: `clustertrace[otel-import]`. Default `import --from otel` reads OTLP/JSON only.
- **Do not write competitor SDK code into our default deps.** No `langfuse`, `langsmith`, `phoenix` packages in our requirements. We parse their *export formats*, not their SDKs.

## Tech stack

- Python 3.11+ stdlib only for the four importers — `json` is enough
- Optional extra `clustertrace[otel-import]` for OTLP/proto, if you implement it (you don't have to in this phase)

## Decision boundaries

**Decide and commit:**
- Exact attribute mapping table for each source (document in the importer's module docstring)
- Whether duplicate IDs across sources mean different traces (yes: prefix with `langfuse:`, `phoenix:`, etc., so the same ID from two sources doesn't collide)
- How to handle nested observations (LangSmith) when they aren't strictly hierarchical

**Stop and write `BLOCKED.md` if:**
- Any source's format is so different that mapping doesn't fit clustertrace's schema (e.g. requires multi-trace semantics)
- A source uses a non-JSON-serializable encoding by default (LangSmith protobuf?) — document as needing the optional extra

## What does NOT ship in this phase

- Bidirectional sync (we only import, never write back)
- Real-time streaming import (file/stdin batch only)
- Auto-detect source format (user must specify `--from <name>`)
- Native SDK wrappers for the competitors (still goes via their export)
- Importer for Helicone, Braintrust, Galileo (the top 4 are enough; expand later)

## Time budget

6–10 hours wall clock for all four importers + tests + CLI wiring + docs.

## Operational rules

- Commit per importer (4 commits minimum)
- After each commit, append to `STATUS.md`
- Bump version to `0.7.0` in `pyproject.toml` and `src/clustertrace/__init__.py` on first commit of this phase
- Tag final commit `v0.7.0`

## References

- Existing export module: [`src/clustertrace/export.py`](../../src/clustertrace/export.py)
- OTel exporter (do not confuse with importer): [`src/clustertrace/otel.py`](../../src/clustertrace/otel.py)
- Langfuse export shape: their public docs (search "tracing export schema")
- OpenInference (Phoenix) spec: https://github.com/Arize-ai/openinference

## Definition of done

```bash
echo '{"traces":[...]}' | clustertrace import --from langfuse
echo '{...}' | clustertrace import --from phoenix
echo '{...}' | clustertrace import --from langsmith
echo '{...}' | clustertrace import --from otel
clustertrace stats               # shows N+M+P+Q traces
clustertrace dashboard           # cluster page works on imported data
pytest tests/test_importers/ -q  # green
```
