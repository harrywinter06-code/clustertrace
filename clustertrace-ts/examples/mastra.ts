/**
 * Instrumenting a Mastra agent with clustertrace.
 *
 * Mastra (https://mastra.ai) is a TS-native agent framework. Wrap each
 * agent function with `trace()` and you get the clusters view for free —
 * the dashboard groups traces by execution pattern so retries / forks /
 * tool sequences become legible at scale.
 *
 * Run:
 *   npx tsx examples/mastra.ts
 *
 * Expects the clustertrace dashboard on http://127.0.0.1:7777
 *   pip install clustertrace && clustertrace dashboard
 */
import { configure, flush, span, tag, toolCall, trace } from "clustertrace";

// NOTE: this file imports `clustertrace` as a package name so it reads
// naturally as docs. For local development, point your tsconfig paths or
// run after `bun run build && npm link`.

configure({ endpoint: "http://127.0.0.1:7777/v1/traces" });

// Pretend Mastra-shaped agent. In real Mastra code you'd be calling
// `createAgent({ ... }).generate(query)` — wrap that call instead.
const researchAgent = trace(
  async (query: string) => {
    tag("query_class", classify(query));

    const docs = await span("retrieve", async () => {
      // Replace with: await embeddings.search(query)
      await new Promise((r) => setTimeout(r, 20));
      return [{ title: "doc1", score: 0.9 }, { title: "doc2", score: 0.7 }];
    });

    toolCall("rank", { args: { docs }, result: docs.slice(0, 1) });

    const answer = await span("synthesize", async () => {
      await new Promise((r) => setTimeout(r, 30));
      return `Based on ${docs[0].title}: clustertrace is local-first observability.`;
    });

    return answer;
  },
  { tags: { agent: "research", framework: "mastra" } },
);

function classify(q: string): string {
  return q.length < 10 ? "short" : "long";
}

async function main(): Promise<void> {
  const answer = await researchAgent("What is clustertrace?");
  console.warn(answer);
  await flush();
}

void main();
