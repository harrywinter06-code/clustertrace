/**
 * clustertrace — local-first LLM agent observability for TypeScript.
 *
 * ```ts
 * import { trace, span, toolCall, configure } from "clustertrace";
 *
 * configure({ endpoint: "http://127.0.0.1:7777/v1/traces" });
 *
 * const ask = trace(async (q: string) => {
 *   await span("retrieve", async () => doRetrieve(q));
 *   toolCall("web_search", { args: { q }, result: hits });
 *   return "done";
 * }, { tags: { agent: "research" } });
 * ```
 */

export { configure, type ClustertraceConfig } from "./config.js";
export {
  flush,
  metric,
  span,
  tag,
  toolCall,
  trace,
  type SpanAttrs,
  type ToolCallOptions,
  type TraceOptions,
} from "./api.js";
export { wrapAnthropic } from "./wrap-anthropic.js";
export { wrapOpenAI } from "./wrap-openai.js";
