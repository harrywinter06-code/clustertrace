# Phase 5 — TypeScript SDK

> **Self-contained brief.** Independent of all other phases. Lives in a new directory `clustertrace-ts/` at the repo root. Can be developed in parallel with everything.

## What this is

A TypeScript / JavaScript package that lets Node / Bun / Deno agent code emit traces to clustertrace's local SQLite store. Published to npm as `clustertrace`. Mirrors the Python SDK's surface:

```typescript
import { trace, span, toolCall, wrapAnthropic, wrapOpenAI } from "clustertrace";

const myAgent = trace(async (query: string) => {
  await span("retrieve", async () => { ... });
  toolCall("web_search", { args: { q: query }, result: hits });
  return "done";
}, { tags: { agent: "research" } });
```

The TS SDK doesn't open SQLite itself (that would mean shipping a SQLite binding to npm). Instead it talks to a small **`clustertrace ingest` HTTP server** that the Python clustertrace exposes, OR emits OpenTelemetry spans to `clustertrace`'s existing OTel exporter via OTLP/HTTP.

## Why it exists

The largest agent ecosystem in 2026 is JS/TS — Mastra, Vercel AI SDK, LangChain.js, LlamaIndex.TS. Without a JS SDK, every user has to either run a Python proxy or rebuild their agent in Python. Halving the addressable market is unacceptable for a "go-to" tool.

## Pre-flight checks

1. **Confirm Node ≥ 20 is available** for development
2. **Decide the transport.** Two options, recommend B:
   - A. Custom HTTP API: Python adds a `clustertrace ingest --port 7778` server; TS SDK POSTs JSON to it. Pros: control. Cons: another moving part.
   - B. **OpenTelemetry**: TS SDK uses `@opentelemetry/sdk-node` with an OTLP/HTTP exporter pointed at the dashboard's `:7777/v1/traces`. Python clustertrace adds an OTLP/HTTP ingestion endpoint that delegates to `ClustertraceSpanExporter`. Pros: standard, works with any other OTel-instrumented JS code automatically. Cons: more work on the Python side to add the OTLP receiver.
3. **Pick a package manager.** Recommend `bun` for fast install + builds in CI; emit standard ESM + CJS so npm install works for everyone

## What ships (binary, all must pass)

### Python side (small addition)

- [ ] New endpoint `POST /v1/traces` on the dashboard that accepts OTLP/JSON span batches and delegates each span to `ClustertraceSpanExporter._export_one`
- [ ] CORS headers set so a browser-based agent could also post (`Access-Control-Allow-Origin: *` on `/v1/traces` only)
- [ ] Test that posts a hand-crafted OTLP/JSON batch and verifies traces land in SQLite

### TypeScript SDK — `clustertrace-ts/`

- [ ] New directory at the repo root: `clustertrace-ts/`
- [ ] `package.json` with name=`clustertrace`, version=`0.1.0`, license=MIT, types pointing at `dist/index.d.ts`
- [ ] Build: TypeScript → `dist/{esm,cjs}`. Use `tsup` for the build
- [ ] Tests with `vitest`. CI runs both Node 20 and Node 22
- [ ] Lint with `eslint` (flat config)
- [ ] README in `clustertrace-ts/README.md` — quickstart + API table

### TS API surface

- [ ] `trace(fn, opts?)` — higher-order function that wraps `fn` and emits a span on every call (sync + async)
- [ ] `span(name, fn, attrs?)` — opens a child span around `fn` (works with async)
- [ ] `toolCall(name, { args, result, error? })`
- [ ] `tag(key, value)`
- [ ] `metric(name, value)`
- [ ] `flush()` — async, awaits the OTel exporter's BatchSpanProcessor
- [ ] `wrapAnthropic(client)` — uses `@anthropic-ai/sdk` types; wraps `client.messages.create`
- [ ] `wrapOpenAI(client)` — uses `openai` types; wraps `client.chat.completions.create`
- [ ] `configure({ endpoint?: string, sampleRate?: number })` — global config; default endpoint `http://127.0.0.1:7777/v1/traces`

### Examples in `clustertrace-ts/examples/`

- [ ] `mastra.ts` — instrumenting a Mastra agent
- [ ] `vercel-ai.ts` — instrumenting Vercel AI SDK calls
- [ ] `langchain.ts` — LangChain.js with @opentelemetry/instrumentation-langchain

## Hard rules

- **No bundled native modules.** The SDK is pure TS/JS — no `better-sqlite3`, no node-gyp. All persistence goes through the HTTP endpoint to the Python side.
- **Sampled-out / disconnected mode must not break the user's app.** If the endpoint is unreachable, log a warning (once per session) and silently drop spans. Never throw.
- **Type-safe public API.** Every exported function has proper TypeScript types. `tsc --strict` passes.

## Tech stack

- TypeScript 5.5+
- `@opentelemetry/sdk-node`, `@opentelemetry/exporter-trace-otlp-http`, `@opentelemetry/api`
- `tsup` for builds, `vitest` for tests, `eslint` for lint
- Peer-dependency types only for `@anthropic-ai/sdk` and `openai` (don't pull them in as hard deps)

## Decision boundaries

**Decide and commit:**
- Whether the Python ingestion endpoint requires auth (recommend: no — local-first, same threat model as the dashboard)
- Sync vs async-only API surface (recommend: both — JS users expect sync wrappers for sync code)
- ESM-only vs dual (recommend: dual; emit both ESM and CJS)

**Stop and write `BLOCKED.md` if:**
- OTel JS exporters don't support the `gen_ai.*` attribute conventions yet (would force us to define our own attr namespace — flag)
- npm package name `clustertrace` is taken (it wasn't checked yet — `npm view clustertrace` early)

## What does NOT ship in this phase

- A Python ↔ JS sync layer beyond OTel
- Browser-side instrumentation (Node only in v0.1)
- Streaming support in `wrapAnthropic` / `wrapOpenAI` (out of scope, mirror Python)
- A npm-published version of the Python SDK (separate package)
- A SaaS-hosted endpoint (always local)

## Time budget

15–25 hours wall clock. The Python OTLP endpoint is small (~3h). The TS SDK is most of the time. Examples + docs at the end.

## Operational rules

- Commit per surface
- The TS package version starts at `0.1.0`. It's independent of the Python package's version.
- After Phase 5 ships, the Python package bumps to `1.0.0-rc.1` (signaling the public API is going to stabilize)
- Tag `ts-v0.1.0` for the TS package release (separate from Python tags)

## References

- OpenTelemetry JS docs: https://opentelemetry.io/docs/instrumentation/js/
- Mastra docs: https://mastra.ai
- Vercel AI SDK docs: https://sdk.vercel.ai
- The existing Python SDK as a reference for API shape: [`src/clustertrace/__init__.py`](../../src/clustertrace/__init__.py)

## Definition of done

```bash
npm view clustertrace            # confirms the name is free or owned by us
cd clustertrace-ts && bun install && bun test          # green
bun run build                     # produces dist/{esm,cjs}/index.{js,d.ts}
node examples/vercel-ai.js        # emits spans; clustertrace dashboard shows them
# In the dashboard:
#   /clusters lists clusters whose traces originated from TS
#   /api/stats shows the TS-emitted traces alongside Python ones
```
