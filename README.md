<div align="center">

![clustertrace clusters page](docs/hero.svg)

# clustertrace

**Local-first LLM agent observability that tells you *which clusters* of traces are failing — not which individual ones.**

<!-- Badges hidden until repo is pushed to GitHub + published to PyPI; URLs below currently 404.
[![tests](https://github.com/harrywinter06-code/clustertrace/actions/workflows/test.yml/badge.svg)](https://github.com/harrywinter06-code/clustertrace/actions/workflows/test.yml)
[![pypi](https://img.shields.io/pypi/v/clustertrace.svg)](https://pypi.org/project/clustertrace/)
[![python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
-->

</div>

Drop in a decorator, an SDK wrapper, or your existing OpenTelemetry setup. Get traces grouped by execution pattern, cost per call, full-text search, and replay of failing runs — all running off a single SQLite file on your laptop.

**Two clusters explain 10 of the 12 failures (83%)** in the bundled 60-trace demo — 18 distinct execution patterns collapsed into one screen instead of 12 stack traces to scroll.

---

## 30-second trial — no API key needed

```bash
pip install clustertrace
clustertrace demo
```

60 pre-recorded traces of three agents (research, RAG, tool-use), dashboard auto-launches, no API spend. *(Pre-PyPI install via `pip install "clustertrace @ git+<repo-url>"` once the repo is pushed; both PyPI and GitHub URLs go live with the first public push.)*

---

## When you're ready to use it for real

### 1. Native decorator

```python
import clustertrace

@clustertrace.trace(tags={"agent": "research"})
async def plan(query): ...

with clustertrace.span("retrieval", k=5):
    ...

clustertrace.tool_call("web_search", args={"q": query}, result=hits)
clustertrace.tag("user_tier", "pro")
clustertrace.metric("score", 0.85)        # numeric — aggregated to a time-series chart
```

Async-safe — concurrent `asyncio.gather` calls produce separate traces; nesting tracks the parent via `contextvars`.

### 2. SDK wrappers (no decorator needed)

```python
from anthropic import Anthropic, AnthropicBedrock, AnthropicVertex
from openai import OpenAI
import clustertrace

client  = clustertrace.wrap_anthropic(Anthropic())          # direct API
bedrock = clustertrace.wrap_anthropic(AnthropicBedrock())   # AWS Bedrock
vertex  = clustertrace.wrap_anthropic(AnthropicVertex())    # Google Vertex
oai     = clustertrace.wrap_openai(OpenAI())                # OpenAI
```

Explicit wrap — no global monkey-patching. Async clients (`AsyncAnthropic`, `AsyncOpenAI`) are detected automatically.

### 3. OpenTelemetry exporter (use your existing instrumentation)

If you already have OTel set up — LangChain, LlamaIndex, Bedrock auto-instrumentation, your own custom spans — add clustertrace as an exporter:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from clustertrace.otel import ClustertraceSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ClustertraceSpanExporter()))
```

The clusters page, cost view, and search work on OTel-sourced traces too. `gen_ai.*` and `llm.*` attribute conventions are mapped onto clustertrace's schema.

---

## What you get

| Page | What |
|---|---|
| **/** | filterable trace list (status, tag, name search) with live polling and per-trace cost |
| **/clusters** | distinct execution patterns — count, failure rate, sample trace, longest common failure prefix, top failing nodes |
| **/search** | FTS5 search across span name + input + output + error_message; supports phrases, OR, NEAR |
| **/metrics** | per-metric aggregates + rolling-mean sparklines for everything you've passed to `clustertrace.metric()` |
| **/failures** | per-span error-rate bars, step-of-failure histogram, force-directed call graph |
| **/trace/&lt;id&gt;** | Gantt timeline + expandable I/O + tags + metrics + per-span cost |

See [`examples/sample-trace.html`](examples/sample-trace.html) for a self-contained shareable snapshot of one failing trace — 16 KB single file with embedded data and renderer, no external assets.

## CLI

```bash
clustertrace demo                                      # one-step trial with bundled data
clustertrace dashboard                                 # launch local server
clustertrace stats                                     # one-screen DB summary
clustertrace inspect --latest                          # rich terminal Gantt of last trace
clustertrace inspect --failed                          # most-recent failed trace
clustertrace inspect <trace_id> --expand <span_id>     # dump that span's I/O
clustertrace mcp                                       # MCP server for AI editors (stdio)
clustertrace mcp install --target claude-code          # wire into your editor
clustertrace backfill-cost                             # compute $ for every LLM call
clustertrace backfill-signatures                       # signatures for older traces
clustertrace snapshot <trace_id> -o trace.html         # self-contained shareable HTML
clustertrace export <trace_id>                         # JSONL to stdout
clustertrace export --all > backup.jsonl               # everything
clustertrace import < backup.jsonl                     # merge (skips existing IDs)
clustertrace replay <trace_id> --entry mod:fn          # re-run with captured args
clustertrace db-path                                   # print SQLite path
```

## Use with AI editors (MCP)

`clustertrace mcp` runs a Model Context Protocol server that exposes your
traces, clusters, and search to any MCP-capable AI editor — Claude Code,
Cursor, Continue. Now "show me a failing trace of this pattern" or
"diff this trace against a successful one" is a single AI assistant command.

```bash
pip install "clustertrace[mcp]"
clustertrace mcp install --target claude-code   # or cursor, or continue
# Restart your editor — clustertrace's tools are now available.
```

Six read-only tools are exposed:

| Tool | What it does |
|---|---|
| `list_clusters` | distinct execution patterns with count + failure rate |
| `get_trace` | full record (trace + spans + tags) for one trace id |
| `search` | FTS5 search over span name + I/O + error messages |
| `failure_summary` | aggregate failure-pattern view, optionally grouped by tag |
| `recent_failed` | the N most recent traces with status=error |
| `compare_traces` | structured diff (insert/delete/equal) of two traces' spans |

Without `--target`, `clustertrace mcp install` prints the JSON snippet for
you to paste into your editor's config manually:

```json
{
  "clustertrace": {
    "command": "clustertrace",
    "args": ["mcp"]
  }
}
```

v0.9 ships read-only tools only — annotate/assert mutation tools land in v1.0
after we see how the read-only surface gets used.

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `CLUSTERTRACE_DB` | `~/.clustertrace/traces.db` | SQLite file path |
| `CLUSTERTRACE_MAX_PAYLOAD_BYTES` | `32768` | Per-field cap on serialized span I/O |
| `CLUSTERTRACE_PRICING_JSON` | (none) | Override or extend the model price table |
| `CLUSTERTRACE_OTLP_MAX_BYTES` | `16777216` | Body cap on `POST /v1/traces`; 413 on overflow |

---

## Case studies

- [**Maintainer dogfood self-study**](docs/case-studies/maintainer-dogfood.md) — synthetic research agent, 40% → 15% failure rate after a four-line fix the cluster page surfaced in five seconds. Reproducible from `examples/case_study_research_agent.py`. Honest about what it does *not* prove (no real customer numbers yet).

---

## When clustertrace is the wrong tool

For production multi-tenant observability — teams, retention policies, PII redaction, managed dashboards — that's a different problem; clustertrace is a debug tool that runs against a single SQLite file on your laptop. It's intentionally simpler. Single-user, no auth, no persistence-tiering.

---

## FAQ

**Why "clustering" instead of just listing traces?** Even at 60 traces (the bundled demo), eyeballing the list doesn't find the pattern. Clustering collapses them into 18 distinct execution patterns and surfaces that 2 patterns account for 10 of the 12 failures (83%). That's the kind of structural signal you can't see from a list — and at production volumes it's the diagnosis that points you at the actual fix without 100× the reading work.

**Why local-only / no auth?** Trade-off: keeps the binary small and the trial frictionless. Single-user is the right default for a debug tool. The README is explicit that production observability with retention and teams is a different tool's job.

**Does it work with LangChain / LlamaIndex / DSPy?** Yes, via the OpenTelemetry path. Anything emitting OTel spans flows into clustertrace. We map `gen_ai.*` / `llm.*` attribute conventions onto our schema so cost and clustering still work.

**Does it support streaming?** The span is logged on completion. Chunk-by-chunk capture isn't implemented yet (v0.4 target).

**What's the algorithmic depth?** Cluster signatures use exact-string equality on a normalized, run-length-collapsed span sequence. Reorderings split clusters today (`A→B→C` and `A→C→B` are two clusters). Reorder-insensitive matching via set-of-edges or tree-edit-distance is the v0.4 algorithmic move. The README doesn't oversell the implementation — see [ARCHITECTURE.md](ARCHITECTURE.md) for the full design trade-offs.

**How much does the demo cost?** $0. The bundled 60 traces are pre-recorded. The full reproduction script (`examples/generate_demo_data.py`, 240 traces) costs ~$2-3 in Haiku.

---

## Overhead

`@clustertrace.trace` adds a low-microsecond decorator overhead (~35 µs of pure-Python wrapping work on modern hardware), but **the SQLite write that follows is the real per-call cost: ~5 ms on Linux/macOS, ~30 ms on Windows NTFS**. The headline number a user running `examples/benchmark.py` will see for an end-to-end traced call is dominated by that disk write, not the decorator. For a debug tool on a laptop this is fine — you don't trace 100/sec. For production:

```python
@clustertrace.trace(sample=0.01)   # log 1% of calls
def hot_path(): ...

@clustertrace.trace(skip=True)     # zero overhead — returns the function unwrapped
def loop_body(): ...
```

Run `python examples/benchmark.py` to see the numbers on your hardware.

## Known limitations

- **Streaming responses are logged on completion only**, not chunk-by-chunk. The `streaming: true` attribute is recorded so you can filter — but the intermediate chunks aren't captured. v0.5 target.
- **Replay with prompt diff is half-built** — `clustertrace replay` re-runs with captured args; modifying the prompt before re-invocation is not yet exposed. v0.5.
- **Native wrappers only for Anthropic and OpenAI.** Bedrock + Vertex work through `wrap_anthropic` (shared `.messages.create` interface). Gemini works through OpenTelemetry.
- **Single-user, no auth.** Dashboard is intended for `127.0.0.1`. See [SECURITY.md](SECURITY.md).

## Contributing

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the design choices, [CONTRIBUTING.md](CONTRIBUTING.md) for the setup and the step-by-step recipe for adding a new SDK wrapper. Real gaps that would meaningfully help users are listed at the bottom of CONTRIBUTING.md.

## License

MIT.
