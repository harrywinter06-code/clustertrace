/**
 * Wrap an Anthropic SDK client so that `client.messages.create` calls
 * emit `llm_call`-flavored spans with `gen_ai.*` attributes — the same
 * convention the Python clustertrace OTel exporter recognises.
 *
 * The user provides their own `@anthropic-ai/sdk` instance; we don't depend
 * on the SDK directly (peer dep only) and we don't monkey-patch globals.
 */
import { SpanKind, SpanStatusCode } from "@opentelemetry/api";

import { getTracer } from "./exporter.js";

/**
 * Minimal duck-typed shape we need. The real `Anthropic` client has many
 * more methods; we only wrap `messages.create`, leaving everything else
 * passthrough.
 */
interface AnthropicLike {
  messages: {
    create: (...args: unknown[]) => Promise<unknown> | unknown;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

interface MessagesCreateArgs {
  model?: string;
  messages?: unknown;
  max_tokens?: number;
  temperature?: number;
  stream?: boolean;
}

interface AnthropicUsage {
  input_tokens?: number;
  output_tokens?: number;
}

interface AnthropicResponse {
  id?: string;
  model?: string;
  stop_reason?: string;
  usage?: AnthropicUsage;
}

function isStreaming(args: MessagesCreateArgs | undefined): boolean {
  return Boolean(args?.stream);
}

export function wrapAnthropic<T extends AnthropicLike>(client: T): T {
  // Build a proxy that intercepts only `messages.create`. Everything else
  // passes through verbatim.
  const originalMessages = client.messages;
  const originalCreate = originalMessages.create.bind(originalMessages);

  const wrappedCreate = function wrappedCreate(
    this: unknown,
    ...args: unknown[]
  ): Promise<unknown> {
    const params = (args[0] ?? {}) as MessagesCreateArgs;
    const model = params.model ?? "unknown";
    const streaming = isStreaming(params);

    const tracer = getTracer();
    const span = tracer.startSpan(`anthropic.messages.create:${model}`, {
      kind: SpanKind.CLIENT,
    });
    span.setAttribute("clustertrace.kind", "llm_call");
    span.setAttribute("gen_ai.system", "anthropic");
    span.setAttribute("gen_ai.request.model", model);
    if (params.max_tokens !== undefined) {
      span.setAttribute("gen_ai.request.max_tokens", params.max_tokens);
    }
    if (params.temperature !== undefined) {
      span.setAttribute("gen_ai.request.temperature", params.temperature);
    }
    if (streaming) span.setAttribute("gen_ai.request.streaming", true);

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
        const r = resp as AnthropicResponse;
        if (r?.usage) {
          if (typeof r.usage.input_tokens === "number") {
            span.setAttribute("gen_ai.usage.input_tokens", r.usage.input_tokens);
          }
          if (typeof r.usage.output_tokens === "number") {
            span.setAttribute("gen_ai.usage.output_tokens", r.usage.output_tokens);
          }
        }
        if (r?.model) span.setAttribute("gen_ai.response.model", r.model);
        if (r?.stop_reason) span.setAttribute("gen_ai.response.finish_reasons", r.stop_reason);
        if (r?.id) span.setAttribute("gen_ai.response.id", r.id);
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

  // Build a wrapped messages proxy that swaps in our `create`.
  const wrappedMessages: typeof originalMessages = new Proxy(originalMessages, {
    get(target, prop, receiver) {
      if (prop === "create") return wrappedCreate;
      return Reflect.get(target, prop, receiver);
    },
  });

  return new Proxy(client, {
    get(target, prop, receiver) {
      if (prop === "messages") return wrappedMessages;
      return Reflect.get(target, prop, receiver);
    },
  }) as T;
}
