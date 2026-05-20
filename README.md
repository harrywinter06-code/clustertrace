# agentlog

**Local-first LLM agent observability that actually tells you what to fix.** Drop in a decorator (or your existing OpenTelemetry setup), get traces grouped by execution pattern, cost-per-call, search across span I/O, replay of failing runs, and an honest answer to *"which two clusters are eating my error budget?"*

```bash
pip install agentlog                 # core + dashboard
pip install "agentlog[anthropic]"    # adds wrap_anthropic (works with Anthropic Bedrock + Vertex clients too)
pip install "agentlog[openai]"       # adds wrap_openai
```

```python
import agentlog

@agentlog.trace(tags={"agent": "research"})
def my_agent(query):
    ...
```

```bash
agentlog dashboard      # http://127.0.0.1:7777
```

That's the entire onboarding.

---

## The pitch

Most LLM tracing tools dump traces into a list and let you go find the failures yourself. agentlog:

1. **Groups every trace by structural signature** — sequence of span names + statuses, consecutive duplicates collapsed — so you see your N distinct execution patterns instead of a stream of N hundred individual traces.
2. **Surfaces which patterns fail** — failure rate per cluster, longest path-prefix shared by every failed run.
3. **Tells you what each cluster costs** — pricing table covers Anthropic / OpenAI / Gemini; override with `$AGENTLOG_PRICING_JSON`.
4. **Lets you re-run a failing trace** — `agentlog replay <id> --entry mod:fn` re-invokes the entrypoint with the captured args, tags the new trace `replay_of=<original>`, the dashboard pairs them.

### A real example

The demo runs three small agents (`research`, `rag`, `tool_use`) ~80 times each — **246 traces, 47 failures, $0.20 total cost**. agentlog finds **29 distinct execution patterns**. The top two clusters explain **87% of all failures**:

| Pattern | Count | Failure rate |
|---|---|---|
| `retrieve:ok → rerank:error` | 29 | 100% |
| `plan:ok → anthropic.messages.create:ok → web_search:error` | 12 | 100% |

The first one is the RAG agent failing at the reranker because retrieval handed it off-topic docs — fix the retriever, not the reranker. The second is the tool-use agent always tripping over a `web_search` rate limit after the planner runs. Both diagnoses in one screen instead of 47 stack traces.

You can reproduce this yourself:

```bash
git clone https://github.com/harrywinter06/agentlog
cd agentlog && uv pip install -e ".[anthropic,dev]"
ANTHROPIC_API_KEY=sk-ant-... AGENTLOG_DB=./demo.db python examples/generate_demo_data.py 80
AGENTLOG_DB=./demo.db agentlog dashboard  # open /clusters
```

---

## Use it without changing your code

Three ways into agentlog, in order of friction:

### 1. Native decorator

```python
import agentlog

@agentlog.trace(tags={"agent": "research"})
async def plan(query): ...

with agentlog.span("retrieval", k=5):
    ...

agentlog.tool_call("web_search", args={"q": query}, result=hits)
agentlog.tag("user_tier", "pro")
agentlog.metric("score", 0.85)        # numeric — aggregates to a time-series chart
```

Async-safe — concurrent `asyncio.gather` calls produce separate traces; nesting tracks the parent via `contextvars`.

### 2. SDK wrappers (no decorator needed)

```python
from anthropic import Anthropic, AnthropicBedrock, AnthropicVertex
from openai import OpenAI
import agentlog

# All four of these work — wrap_anthropic uses duck typing on .messages.create
client = agentlog.wrap_anthropic(Anthropic())          # direct API
bedrock = agentlog.wrap_anthropic(AnthropicBedrock())  # AWS Bedrock
vertex  = agentlog.wrap_anthropic(AnthropicVertex())   # Google Vertex
oai     = agentlog.wrap_openai(OpenAI())               # OpenAI
```

Explicit wrap — no global monkey-patching of `requests` or the SDK module.

### 3. OpenTelemetry exporter (use your existing instrumentation)

If you already have OTel set up — LangChain, LlamaIndex, Bedrock auto-instrumentation, your own custom spans — just add agentlog as an exporter:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from agentlog.otel import AgentlogSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(AgentlogSpanExporter()))
```

The clusters page, cost view, and search work on OTel-sourced traces too. agentlog maps `gen_ai.*` and `llm.*` attribute conventions onto its own schema.

---

## What the dashboard shows

| Page | What |
|---|---|
| **/** | filterable trace list (status, tag, name search) with live polling, **cost per trace** |
| **/clusters** | each distinct execution pattern as one row — count, failure rate, sample trace, common failure prefix, top failing nodes |
| **/search** | FTS5 search across span name + input + output + error_message; supports phrases, OR, NEAR |
| **/metrics** | per-metric aggregates + rolling-mean sparklines for everything you've passed to `agentlog.metric()` |
| **/failures** | per-span error-rate bars, step-of-failure histogram, force-directed call graph |
| **/trace/&lt;id&gt;** | Gantt timeline + expandable I/O + tags + metrics + per-span cost |

See [`examples/sample-trace.html`](examples/sample-trace.html) for a self-contained shareable snapshot of one failing trace.

---

## CLI

```bash
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

---

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `AGENTLOG_DB` | `~/.agentlog/traces.db` | SQLite file path |
| `AGENTLOG_MAX_PAYLOAD_BYTES` | `32768` | Per-field cap on serialized span I/O |
| `AGENTLOG_PRICING_JSON` | (none) | Override or extend the model price table |

---

## What ships in v0.3

- `@trace` (sync + async), `agentlog.span`, `agentlog.tool_call`, `agentlog.tag`, `agentlog.metric`
- `wrap_anthropic` (works with Anthropic, AnthropicBedrock, AnthropicVertex) and `wrap_openai` (sync + async clients)
- **`AgentlogSpanExporter`** — any OpenTelemetry-instrumented app pipes spans into agentlog with one line
- **Structural clustering** with longest-common-failure-prefix mining
- **Cost tracking** with a built-in pricing table + env-var overrides
- **FTS5 search** across span I/O
- **Numeric metrics** + rolling-mean sparkline charts
- **Replay** stored traces with their captured inputs
- **JSON export/import** for portability
- **Self-contained shareable HTML snapshots** (single file, no external assets)
- SQLite storage with WAL, schema migrations, and on-demand backfill
- Per-field payload truncation
- 74 tests, ruff clean

## Known limitations

- Reorderings split clusters. Two traces that do `A→B→C` and `A→C→B` are two clusters today, not one. Fix is on the v0.4 list (set-of-edges or tree-edit-distance signature mode).
- Streaming responses are logged on completion only, not chunk-by-chunk.
- Native wrappers only for Anthropic and OpenAI. Bedrock and Vertex *work through* `wrap_anthropic` because the Anthropic SDK's Bedrock/Vertex clients share the `.messages.create` interface — but a native `wrap_bedrock` / `wrap_gemini` would catch token attribution edge cases better. PRs welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)).
- Replay with a modified prompt isn't implemented yet — the machinery is there, the prompt-diff stage isn't. v0.4.
- No retention policy. The DB grows until you delete it; `~/.agentlog/traces.db` is safe to remove between sessions.

## Why I built this

I was debugging an agent and noticed every tracing tool I tried treated each trace as a standalone item. With 200 traces and 40 failures, the only way to find a pattern was to eyeball them. The unblocking observation: most failures cluster on a small number of execution paths. Group by path, surface the failing groups, show what they share — that's most of the diagnosis.

This is the smallest thing that delivers that. Read [ARCHITECTURE.md](ARCHITECTURE.md) for the trade-offs.

## License

MIT.
