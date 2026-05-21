/**
 * Instrumenting Vercel AI SDK (`ai`) calls with clustertrace.
 *
 * The Vercel AI SDK already emits OpenTelemetry spans via `experimental_telemetry`.
 * Two integration paths:
 *   1. Easiest: enable experimental_telemetry and configure() clustertrace —
 *      our endpoint accepts any OTel JS exporter. The Vercel-generated spans
 *      land alongside your `trace()`-wrapped wrappers.
 *   2. Explicit: wrap your own functions that call `generateText` / `streamText`.
 *
 * This example shows path 2 since it composes more clearly with `tag()` and
 * `toolCall()`. Run:
 *   npx tsx examples/vercel-ai.ts
 */
import { configure, flush, span, tag, trace } from "clustertrace";

configure({
  endpoint: "http://127.0.0.1:7777/v1/traces",
  serviceName: "vercel-ai-demo",
});

// In a real app: `import { generateText } from "ai";`
async function generateText(opts: { model: string; prompt: string }): Promise<{ text: string }> {
  await new Promise((r) => setTimeout(r, 25));
  return { text: `Echo: ${opts.prompt}` };
}

const ask = trace(
  async (prompt: string) => {
    tag("model", "gpt-4o-mini");
    const { text } = await span("ai.generateText", async () =>
      generateText({ model: "gpt-4o-mini", prompt }),
    );
    return text;
  },
  { name: "vercel-ai.ask", tags: { provider: "openai" } },
);

async function main(): Promise<void> {
  console.warn(await ask("Hello, world"));
  console.warn(await ask("Another question"));
  await flush();
}

void main();
