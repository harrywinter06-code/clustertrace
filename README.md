<div align="center">

![agentlog clusters page](docs/hero.svg)

# agentlog

**Local-first LLM agent observability that tells you *which clusters* of traces are failing — not which individual ones.**

[![tests](https://github.com/harrywinter06/agentlog/actions/workflows/test.yml/badge.svg)](https://github.com/harrywinter06/agentlog/actions/workflows/test.yml)
[![pypi](https://img.shields.io/pypi/v/agentlog.svg)](https://pypi.org/project/agentlog/)
[![python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

Drop in a decorator, an SDK wrapper, or your existing OpenTelemetry setup. Get traces grouped by execution pattern, cost per call, full-text search, and replay of failing runs — all running off a single SQLite file on your laptop.

**Two clusters explain 87% of all failures** in the bundled demo. That's the kind of diagnosis the clusters page hands you in one screen instead of 47 stack traces.

---

## 30-second trial — no API key needed

```bash
pip install agentlog
agentlog demo
```

60 pre-recorded traces of three agents (research, RAG, tool-use), dashboard auto-launches, no API spend. *Pre-PyPI:* `pip install "agentlog @ git+https://github.com/harrywinter06/agentlog"`.

---

## When you're ready to use it for real

### 1. Native decorator

```python
import agentlog

@agentlog.trace(tags={"agent": "research"})
async def plan(query): ...

with agentlog.span("retrieval", k=5):
    ...

agentlog.tool_call("web_search", args={"q": query}, result=hits)
agentlog.tag("user_tier", "pro")
agentlog.metric("score", 0.85)        # numeric — aggregated to a time-series chart
```

Async-safe — concurrent `asyncio.gather` calls produce separate traces; nesting tracks the parent via `contextvars`.

### 2. SDK wrappers (no decorator needed)

```python
from anthropic import Anthropic, AnthropicBedrock, AnthropicVertex
from openai import OpenAI
import agentlog

client  = agentlog.wrap_anthropic(Anthropic())          # direct API
bedrock = agentlog.wrap_anthropic(AnthropicBedrock())   # AWS Bedrock
vertex  = agentlog.wrap_anthropic(AnthropicVertex())    # Google Vertex
oai     = agentlog.wrap_openai(OpenAI())                # OpenAI
```

Explicit wrap — no global monkey-patching. Async clients (`AsyncAnthropic`, `AsyncOpenAI`) are detected automatically.

### 3. OpenTelemetry exporter (use your existing instrumentation)

If you already have OTel set up — LangChain, LlamaIndex, Bedrock auto-instrumentation, your own custom spans — add agentlog as an exporter:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from agentlog.otel import AgentlogSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(AgentlogSpanExporter()))
```

The clusters page, cost view, and search work on OTel-sourced traces too. `gen_ai.*` and `llm.*` attribute conventions are mapped onto agentlog's schema.

---

## What you get

| Page | What |
|---|---|
| **/** | filterable trace list (status, tag, name search) with live polling and per-trace cost |
| **/clusters** | distinct execution patterns — count, failure rate, sample trace, longest common failure prefix, top failing nodes |
| **/search** | FTS5 search across span name + input + output + error_message; supports phrases, OR, NEAR |
| **/metrics** | per-metric aggregates + rolling-mean sparklines for everything you've passed to `agentlog.metric()` |
| **/failures** | per-span error-rate bars, step-of-failure histogram, force-directed call graph |
| **/trace/&lt;id&gt;** | Gantt timeline + expandable I/O + tags + metrics + per-span cost |

See [`examples/sample-trace.html`](examples/sample-trace.html) for a self-contained shareable snapshot of one failing trace — 16 KB single file with embedded data and renderer, no external assets.

## CLI

```bash
agentlog demo                                      # one-step trial with bundled data
agentlog dashboard                                 # launch local server
agentlog stats                                     # one-screen DB summary
agentlog backfill-cost                             # compute $ for every LLM call
agentlog backfill-signatures                       # signatures for older traces
agentlog snapshot <trace_id> -o trace.html         # self-contained shareable HTML
agentlog export <trace_id>                         # JSONL to stdout
agentlog export --all > backup.jsonl               # everything
agentlog import < backup.jsonl                     # merge (skips existing IDs)
agentlog replay <trace_id> --entry mod:fn          # re-run with captured args
agentlog db-path                                   # print SQLite path
```

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `AGENTLOG_DB` | `~/.agentlog/traces.db` | SQLite file path |
| `AGENTLOG_MAX_PAYLOAD_BYTES` | `32768` | Per-field cap on serialized span I/O |
| `AGENTLOG_PRICING_JSON` | (none) | Override or extend the model price table |

---

## How does this compare to Langfuse / Phoenix / LangSmith?

|  | agentlog | Langfuse OSS | Arize Phoenix | LangSmith |
|---|---|---|---|---|
| Local-first (one binary / SQLite) | yes | no (Postgres + worker) | yes (in-memory or Postgres) | no (SaaS) |
| **Clusters traces by execution pattern** | **yes — the differentiator** | no | partial (groupings by ID, not signature) | no |
| **Longest common failure prefix** | **yes** | no | no | no |
| OpenTelemetry ingestion | yes (exporter) | yes | yes | partial |
| Cost tracking | yes (built-in pricing) | yes | yes | yes |
| Full-text search | yes (FTS5) | yes | yes | yes |
| Replay with captured args | yes | partial | partial | yes |
| Self-contained shareable trace HTML | **yes** (no other tool ships this) | no | no | no |
| Decorator + OTel + SDK wrappers | all three | OTel + wrappers | OTel | wrappers |
| Single-file install, no server setup | yes | no | yes (for in-mem) | n/a |
| Multi-user / teams | no | yes | yes | yes |
| Production retention / sampling | no | yes | yes | yes |

**Pick agentlog when:** you're debugging a single agent or running a small eval suite on your laptop, you want clustering + failure-prefix mining as a first-class view, and you'd rather `pip install` than `docker compose up`.

**Pick Langfuse / Phoenix / LangSmith when:** you're running in production, need teams, need retention policies, need PII redaction, or want a managed dashboard. agentlog is intentionally simpler.

---

## FAQ

**Why not just use Langfuse OSS?** Langfuse is more capable for production deployment — multi-user, Postgres-backed, fully featured. It's also a four-container Docker stack that needs a workers process and a separate web service. agentlog is one Python package and one SQLite file. If you want to debug an agent on your laptop tonight, agentlog is faster to set up; if you want to deploy a tracing service for a team, Langfuse is the right answer.

**Why "clustering" instead of just listing traces?** Because at 200+ traces, eyeballing the list doesn't find the pattern. The demo data has 29 distinct execution patterns; the top 2 account for 87% of all failures. That's the kind of structural signal you can't see from a list — and it's the diagnosis that points you at the actual fix.

**Why local-only / no auth?** Trade-off: keeps the binary small and the trial frictionless. Single-user is the right default for a debug tool. The README is explicit that production observability with retention and teams is a different tool's job.

**Does it work with LangChain / LlamaIndex / DSPy?** Yes, via the OpenTelemetry path. Anything emitting OTel spans flows into agentlog. We map `gen_ai.*` / `llm.*` attribute conventions onto our schema so cost and clustering still work.

**Does it support streaming?** The span is logged on completion. Chunk-by-chunk capture isn't implemented yet (v0.4 target).

**What's the algorithmic depth?** Cluster signatures use exact-string equality on a normalized, run-length-collapsed span sequence. Reorderings split clusters today (`A→B→C` and `A→C→B` are two clusters). Reorder-insensitive matching via set-of-edges or tree-edit-distance is the v0.4 algorithmic move. The README doesn't oversell the implementation — see [ARCHITECTURE.md](ARCHITECTURE.md) for the full design trade-offs.

**How much does the demo cost?** $0. The bundled 60 traces are pre-recorded. The full reproduction script (`examples/generate_demo_data.py`, 240 traces) costs ~$2-3 in Haiku.

---

## Overhead

`@agentlog.trace` adds **~35 µs of pure-Python overhead** per call on modern hardware; the SQLite write that follows is the real cost (~5 ms on Linux/macOS, ~30 ms on Windows NTFS). For a debug tool on a laptop this is fine — you don't trace 100/sec. For production:

```python
@agentlog.trace(sample=0.01)   # log 1% of calls
def hot_path(): ...

@agentlog.trace(skip=True)     # zero overhead — returns the function unwrapped
def loop_body(): ...
```

Run `python examples/benchmark.py` to see the numbers on your hardware.

## Known limitations

- **Streaming responses are logged on completion only**, not chunk-by-chunk. The `streaming: true` attribute is recorded so you can filter — but the intermediate chunks aren't captured. v0.5 target.
- **Replay with prompt diff is half-built** — `agentlog replay` re-runs with captured args; modifying the prompt before re-invocation is not yet exposed. v0.5.
- **Native wrappers only for Anthropic and OpenAI.** Bedrock + Vertex work through `wrap_anthropic` (shared `.messages.create` interface). Gemini works through OpenTelemetry.
- **Single-user, no auth.** Dashboard is intended for `127.0.0.1`. See [SECURITY.md](SECURITY.md).

## Contributing

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the design choices, [CONTRIBUTING.md](CONTRIBUTING.md) for the setup and the step-by-step recipe for adding a new SDK wrapper. Real gaps that would meaningfully help users are listed at the bottom of CONTRIBUTING.md.

## License

MIT.
