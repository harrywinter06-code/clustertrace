# clustertrace (TypeScript)

Local-first LLM agent observability for Node / Bun / Deno. Mirrors the
[Python clustertrace](https://github.com/harrywinter06-code/clustertrace) SDK's
surface and ships spans to the same dashboard via OTLP/HTTP.

> **Status:** v0.1.0 — Node only, no streaming wrap, no browser-side
> instrumentation. See the [Phase 5 roadmap](../docs/roadmap/phase-5-typescript-sdk.md)
> for what is and isn't in this release.

## Install

```bash
npm install clustertrace
# or: bun add clustertrace / pnpm add clustertrace / yarn add clustertrace
```

Requires Node 20+.

## Quickstart

Start the Python dashboard (in your repo or wherever):

```bash
pip install clustertrace
clustertrace dashboard --port 7777
```

Then in your TS code:

```ts
import { trace, span, toolCall, configure } from "clustertrace";

// Optional — the default already points at http://127.0.0.1:7777/v1/traces.
configure({ endpoint: "http://127.0.0.1:7777/v1/traces" });

const ask = trace(async (query: string) => {
  const hits = await span("retrieve", async () => fetchDocs(query));
  toolCall("rank", { args: { query }, result: hits.slice(0, 5) });
  return summarize(hits);
}, { tags: { agent: "research" } });

await ask("what is clustertrace?");
```

Open `http://127.0.0.1:7777` — your traces are there, grouped into clusters
alongside any Python-emitted traces from the same project.

## API

| Function | Purpose |
| --- | --- |
| `trace(fn, opts?)` | Higher-order wrapper. Returns a function with the same signature; every call emits a span. Works for sync + async fns. |
| `span(name, fn, attrs?)` | Open a child span around `fn`. Works for sync + async. |
| `toolCall(name, { args, result, error? })` | Record a tool invocation after it ran. |
| `tag(key, value)` | Attach a tag to the active span. No-op outside a span. |
| `metric(name, value)` | Attach a numeric metric to the active span. No-op outside a span. |
| `flush()` | `await` this before process exit so in-flight spans land in the dashboard. |
| `wrapAnthropic(client)` | Wrap an `@anthropic-ai/sdk` client so `messages.create` calls are logged. |
| `wrapOpenAI(client)` | Wrap an `openai` client so `chat.completions.create` calls are logged. |
| `configure({ endpoint?, sampleRate?, serviceName?, timeoutMs? })` | Global config. Call before the first trace. |

### `trace(fn, opts?)`

```ts
const sum = trace((a: number, b: number) => a + b, { name: "math.sum" });
```

`opts`:
- `name?: string` — defaults to `fn.name` or `"trace"`.
- `tags?: Record<string, string | number | boolean>` — attached to every call.
- `kind?: string` — `"function"` | `"llm_call"` | `"tool_call"` | `"agent"`, pure metadata.

### `wrapAnthropic(client)`

```ts
import Anthropic from "@anthropic-ai/sdk";
import { wrapAnthropic } from "clustertrace";

const client = wrapAnthropic(new Anthropic());
const resp = await client.messages.create({
  model: "claude-haiku-4-5-20251001",
  max_tokens: 256,
  messages: [{ role: "user", content: "hi" }],
});
```

The wrapped client emits an `llm_call` span with `gen_ai.*` attributes
(`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`). These
are the standard OTel GenAI conventions, so the clustertrace Python OTel
exporter automatically normalises them into the dashboard's token counts.

Streaming (`{ stream: true }`) is recorded as a span tag but not deep-instrumented
in this release — same as the Python SDK's v0.5.

### `wrapOpenAI(client)`

```ts
import OpenAI from "openai";
import { wrapOpenAI } from "clustertrace";

const client = wrapOpenAI(new OpenAI());
const resp = await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "hi" }],
});
```

## How it works

The SDK uses OpenTelemetry under the hood and emits OTLP/HTTP (JSON) span
batches to the Python dashboard's `POST /v1/traces` endpoint. That endpoint
delegates each span to the existing `ClustertraceSpanExporter._export_one`,
which is the same code path that ingests Python-side OTel spans. Result:
TS-emitted traces are first-class citizens — they show up in the clusters
view, the failure graph, and the search index alongside Python traces.

No SQLite binding, no native modules — the SDK is pure TS/JS. All persistence
goes through the HTTP endpoint.

## Fail-soft contract

If the dashboard is unreachable, the SDK logs **one warning per session**
and silently drops spans. It never throws out of `trace`, `span`, `toolCall`,
`tag`, `metric`, or `flush`. Your agent code keeps running.

## Configuration

```ts
configure({
  endpoint: "http://127.0.0.1:7777/v1/traces",   // default
  sampleRate: 1.0,                                // 0.0–1.0
  serviceName: "my-agent",                        // OTel resource attr
  timeoutMs: 5000,                                // per-export timeout
});
```

`configure()` must be called **before** the first `trace()` invocation —
the OTel exporter is built lazily on first use.

## Process exit

```ts
process.on("beforeExit", async () => {
  await flush();
});
```

In serverless / Lambda, call `await flush()` at the end of each handler.

## Examples

- [`examples/mastra.ts`](./examples/mastra.ts) — instrument a Mastra agent
- [`examples/vercel-ai.ts`](./examples/vercel-ai.ts) — Vercel AI SDK
- [`examples/langchain.ts`](./examples/langchain.ts) — LangChain.js

## License

MIT
