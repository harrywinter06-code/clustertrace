/**
 * Public tracing primitives. Mirrors the Python SDK's `clustertrace`
 * surface — `trace`, `span`, `toolCall`, `tag`, `metric`, `flush`.
 *
 * Implementation note: we use OpenTelemetry's own context propagation
 * (`context.with`) for nesting, so async hops are tracked correctly under
 * Node's AsyncLocalStorage. We do NOT roll our own contextvar.
 */
import {
  SpanKind,
  SpanStatusCode,
  context as otelContext,
  trace as otelTrace,
  type Span,
} from "@opentelemetry/api";

import { flushSpans, getTracer } from "./exporter.js";

/** Primitive scalar types accepted as span/tag/attribute values. */
type Scalar = string | number | boolean;

/** Options for the `trace()` higher-order wrapper. */
export interface TraceOptions {
  /** Override the span name (defaults to the function's `.name` or "trace"). */
  name?: string;
  /** Tags to attach to every invocation. */
  tags?: Record<string, Scalar>;
  /**
   * Tag the span kind in the dashboard ("function" | "llm_call" | "tool_call" | "agent").
   * Pure metadata — does not change behavior.
   */
  kind?: string;
}

export interface SpanAttrs {
  [key: string]: Scalar | undefined;
}

export interface ToolCallOptions {
  args?: unknown;
  result?: unknown;
  error?: unknown;
}

function applyTags(span: Span, tags: Record<string, Scalar> | undefined): void {
  if (!tags) return;
  for (const [k, v] of Object.entries(tags)) {
    span.setAttribute(`clustertrace.tag.${k}`, v);
  }
}

function applyAttrs(span: Span, attrs: SpanAttrs | undefined): void {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined) span.setAttribute(k, v);
  }
}

function recordError(span: Span, err: unknown): void {
  const e = err instanceof Error ? err : new Error(String(err));
  span.recordException(e);
  span.setStatus({ code: SpanStatusCode.ERROR, message: e.message });
}

/**
 * `trace()` — higher-order wrapper. Returns a function with the same
 * signature; every call opens a root-ish span on the active context.
 *
 * Works for both sync and async functions. Sync functions emit a span
 * that ends synchronously; async functions emit a span that ends after
 * the returned promise settles.
 */
export function trace<A extends unknown[], R>(
  fn: (...args: A) => R,
  opts: TraceOptions = {},
): (...args: A) => R {
  const spanName = opts.name ?? fn.name ?? "trace";

  return function tracedFn(this: unknown, ...args: A): R {
    const tracer = getTracer();
    const span = tracer.startSpan(spanName, { kind: SpanKind.INTERNAL });
    applyTags(span, opts.tags);
    if (opts.kind) span.setAttribute("clustertrace.kind", opts.kind);

    const ctx = otelTrace.setSpan(otelContext.active(), span);

    const finish = (err?: unknown): void => {
      if (err !== undefined) {
        recordError(span, err);
      } else {
        span.setStatus({ code: SpanStatusCode.OK });
      }
      span.end();
    };

    try {
      const result: R = otelContext.with(ctx, () => fn.apply(this, args));

      // Async path — wait on the promise without changing its identity.
      if (isThenable(result)) {
        return (result as unknown as Promise<unknown>).then(
          (val) => {
            finish();
            return val;
          },
          (err: unknown) => {
            finish(err);
            throw err;
          },
        ) as R;
      }

      // Sync path.
      finish();
      return result;
    } catch (err) {
      finish(err);
      throw err;
    }
  };
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
  return (
    value !== null &&
    typeof value === "object" &&
    typeof (value as { then?: unknown }).then === "function"
  );
}

/**
 * `span()` — open a child span around `fn`. Works with both sync and async fns.
 *
 * If called outside any active trace, OTel still creates a span; it just
 * has no parent. The dashboard treats it as a one-span trace, mirroring
 * the Python `span()` behavior outside `@trace`.
 */
export function span<R>(
  name: string,
  fn: () => R,
  attrs?: SpanAttrs,
): R {
  const tracer = getTracer();
  const child = tracer.startSpan(name);
  applyAttrs(child, attrs);

  const ctx = otelTrace.setSpan(otelContext.active(), child);

  const finish = (err?: unknown): void => {
    if (err !== undefined) {
      recordError(child, err);
    } else {
      child.setStatus({ code: SpanStatusCode.OK });
    }
    child.end();
  };

  try {
    const result: R = otelContext.with(ctx, fn);
    if (isThenable(result)) {
      return (result as unknown as Promise<unknown>).then(
        (val) => {
          finish();
          return val;
        },
        (err: unknown) => {
          finish(err);
          throw err;
        },
      ) as R;
    }
    finish();
    return result;
  } catch (err) {
    finish(err);
    throw err;
  }
}

/**
 * `toolCall()` — record a fire-and-forget tool invocation.
 *
 * Unlike `span()`, this does not wrap a function — you pass the args and
 * result after the call has happened. Useful for instrumenting tools that
 * already return synchronously (e.g. local DB lookups) where opening and
 * closing a span around them would just clutter the call site.
 */
export function toolCall(name: string, opts: ToolCallOptions = {}): void {
  const tracer = getTracer();
  const s = tracer.startSpan(`tool:${name}`, { kind: SpanKind.INTERNAL });
  s.setAttribute("clustertrace.kind", "tool_call");
  s.setAttribute("tool.name", name);
  if (opts.args !== undefined) {
    s.setAttribute("tool.args_json", safeStringify(opts.args));
  }
  if (opts.result !== undefined) {
    s.setAttribute("tool.result_json", safeStringify(opts.result));
  }
  if (opts.error !== undefined) {
    recordError(s, opts.error);
  } else {
    s.setStatus({ code: SpanStatusCode.OK });
  }
  s.end();
}

/**
 * `tag()` — attach a key=value tag to the currently active span. Tags
 * are stored under the `clustertrace.tag.*` namespace so they survive the
 * OTel→clustertrace mapping intact (the Python OTel exporter mirrors any
 * `clustertrace.tag.*` attr into the trace_tags table on the trace's root
 * span — but in the v0.1 cut, tags-on-spans are visible in the dashboard
 * via the span attrs panel).
 *
 * No-op if there is no active span — matches the Python behavior.
 */
export function tag(key: string, value: Scalar): void {
  const active = otelTrace.getActiveSpan();
  if (!active) return;
  active.setAttribute(`clustertrace.tag.${key}`, value);
}

/**
 * `metric()` — attach a numeric metric to the currently active span.
 *
 * Stored as `clustertrace.metric.<name>` on the span, which the dashboard
 * reads alongside the regular metrics endpoint.
 *
 * No-op outside a span.
 */
export function metric(name: string, value: number): void {
  const active = otelTrace.getActiveSpan();
  if (!active) return;
  if (!Number.isFinite(value)) return;
  active.setAttribute(`clustertrace.metric.${name}`, value);
}

/**
 * `flush()` — await the OTel BatchSpanProcessor. Call this before process
 * exit (or in serverless after-callback) to make sure in-flight spans land.
 */
export async function flush(): Promise<void> {
  await flushSpans();
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, replacer);
  } catch {
    return String(value);
  }
}

function replacer(_k: string, v: unknown): unknown {
  if (v instanceof Error) {
    return { name: v.name, message: v.message, stack: v.stack };
  }
  if (typeof v === "bigint") return v.toString();
  return v;
}
