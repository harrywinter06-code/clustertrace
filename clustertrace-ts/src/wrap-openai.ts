/**
 * Wrap an OpenAI SDK client so `client.chat.completions.create` emits
 * an `llm_call` span with `gen_ai.*` attributes.
 *
 * Peer dep — we don't import `openai` directly.
 */
import { SpanKind, SpanStatusCode } from "@opentelemetry/api";

import { getTracer } from "./exporter.js";

interface OpenAILike {
  chat: {
    completions: {
      create: (...args: unknown[]) => Promise<unknown> | unknown;
      [k: string]: unknown;
    };
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

interface ChatCompletionsCreateArgs {
  model?: string;
  messages?: unknown;
  max_tokens?: number;
  temperature?: number;
  stream?: boolean;
}

interface OpenAIUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

interface OpenAIResponse {
  id?: string;
  model?: string;
  usage?: OpenAIUsage;
  choices?: Array<{ finish_reason?: string }>;
}

export function wrapOpenAI<T extends OpenAILike>(client: T): T {
  const originalCompletions = client.chat.completions;
  const originalCreate = originalCompletions.create.bind(originalCompletions);

  const wrappedCreate = function wrappedCreate(
    this: unknown,
    ...args: unknown[]
  ): Promise<unknown> {
    const params = (args[0] ?? {}) as ChatCompletionsCreateArgs;
    const model = params.model ?? "unknown";

    const tracer = getTracer();
    const span = tracer.startSpan(`openai.chat.completions.create:${model}`, {
      kind: SpanKind.CLIENT,
    });
    span.setAttribute("clustertrace.kind", "llm_call");
    span.setAttribute("gen_ai.system", "openai");
    span.setAttribute("gen_ai.request.model", model);
    if (params.max_tokens !== undefined) {
      span.setAttribute("gen_ai.request.max_tokens", params.max_tokens);
    }
    if (params.temperature !== undefined) {
      span.setAttribute("gen_ai.request.temperature", params.temperature);
    }
    if (params.stream) span.setAttribute("gen_ai.request.streaming", true);

    let resultMaybe: Promise<unknown> | unknown;
    try {
      resultMaybe = originalCreate(...args);
    } catch (err) {
      span.recordException(err instanceof Error ? err : new Error(String(err)));
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (err as Error).message,
      });
      span.end();
      throw err;
    }

    return Promise.resolve(resultMaybe).then(
      (resp) => {
        const r = resp as OpenAIResponse;
        if (r?.usage) {
          if (typeof r.usage.prompt_tokens === "number") {
            span.setAttribute("gen_ai.usage.input_tokens", r.usage.prompt_tokens);
          }
          if (typeof r.usage.completion_tokens === "number") {
            span.setAttribute("gen_ai.usage.output_tokens", r.usage.completion_tokens);
          }
        }
        if (r?.model) span.setAttribute("gen_ai.response.model", r.model);
        if (r?.id) span.setAttribute("gen_ai.response.id", r.id);
        const finishReason = r?.choices?.[0]?.finish_reason;
        if (finishReason) {
          span.setAttribute("gen_ai.response.finish_reasons", finishReason);
        }
        span.setStatus({ code: SpanStatusCode.OK });
        span.end();
        return resp;
      },
      (err: unknown) => {
        span.recordException(err instanceof Error ? err : new Error(String(err)));
        span.setStatus({
          code: SpanStatusCode.ERROR,
          message: err instanceof Error ? err.message : String(err),
        });
        span.end();
        throw err;
      },
    );
  };

  const wrappedCompletions: typeof originalCompletions = new Proxy(originalCompletions, {
    get(target, prop, receiver) {
      if (prop === "create") return wrappedCreate;
      return Reflect.get(target, prop, receiver);
    },
  });

  const wrappedChat: typeof client.chat = new Proxy(client.chat, {
    get(target, prop, receiver) {
      if (prop === "completions") return wrappedCompletions;
      return Reflect.get(target, prop, receiver);
    },
  });

  return new Proxy(client, {
    get(target, prop, receiver) {
      if (prop === "chat") return wrappedChat;
      return Reflect.get(target, prop, receiver);
    },
  }) as T;
}
