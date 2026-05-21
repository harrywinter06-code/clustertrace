/**
 * Instrumenting LangChain.js with clustertrace.
 *
 * Two paths:
 *   1. Use `@langchain/community/callbacks` LangChainTracer with our OTLP
 *      endpoint set via env var (LANGCHAIN_TRACING_V2=true, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT).
 *   2. Use `@opentelemetry/instrumentation-langchain` (recommended). It emits
 *      gen_ai.* spans automatically — pointing your OTel exporter at the
 *      clustertrace endpoint surfaces them in the clusters view.
 *
 * Below: path 2 as the recommended setup, plus an explicit `trace()` wrap
 * for the outer orchestration layer.
 *
 * Run:
 *   npx tsx examples/langchain.ts
 */
import { configure, flush, span, trace } from "clustertrace";

configure({
  endpoint: "http://127.0.0.1:7777/v1/traces",
  serviceName: "langchain-demo",
});

// In a real app you'd wire up LangChain auto-instrumentation here, e.g.
//   import { registerInstrumentations } from "@opentelemetry/instrumentation";
//   import { LangChainInstrumentation } from "@traceloop/instrumentation-langchain";
//   registerInstrumentations({ instrumentations: [new LangChainInstrumentation()] });
//
// Then build your chain as usual:
//   const chain = ChatPromptTemplate.fromTemplate("Q: {q}").pipe(llm);
//   await chain.invoke({ q: "what is clustertrace?" });
// Both your `trace()` span AND the LangChain-emitted gen_ai spans land in
// the same trace tree.

async function fakeChainInvoke(input: { q: string }): Promise<string> {
  await new Promise((r) => setTimeout(r, 30));
  return `A: clustertrace observes ${input.q}`;
}

const askChain = trace(
  async (q: string) => {
    const answer = await span("langchain.chain.invoke", async () => fakeChainInvoke({ q }));
    return answer;
  },
  { name: "langchain.ask", tags: { framework: "langchain.js" } },
);

async function main(): Promise<void> {
  console.warn(await askChain("what is clustertrace?"));
  await flush();
}

void main();
