/**
 * Lazily-built OTel tracer + exporter that ships spans to clustertrace.
 *
 * Why lazy: `configure()` must be able to override the endpoint before the
 * first span flies. Building the SDK at module import would freeze defaults
 * before the user has a chance to call `configure()`.
 *
 * Fail-soft contract (hard rule from the roadmap): if the dashboard is
 * unreachable we WARN ONCE per session and silently drop spans. We never
 * throw out of public API methods. The user's agent must keep running.
 */
import { type Tracer } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { Resource } from "@opentelemetry/resources";
import {
  BatchSpanProcessor,
  type SpanExporter,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  ATTR_SERVICE_NAME,
  ATTR_SERVICE_VERSION,
} from "@opentelemetry/semantic-conventions";

import { getConfig } from "./config.js";

const TRACER_NAME = "clustertrace";
const TRACER_VERSION = "0.1.0";

interface Internal {
  provider: NodeTracerProvider;
  processor: BatchSpanProcessor;
  tracer: Tracer;
}

let initialized: Internal | undefined;
let warned = false;

function warnOnce(msg: string): void {
  if (warned) return;
  warned = true;
  console.warn(`[clustertrace] ${msg} — spans for this session will be dropped silently.`);
}

/**
 * Wraps the OTLP exporter so any export failure is swallowed and reported
 * via a single warning, not propagated upward. The OTel `BatchSpanProcessor`
 * already retries on transient failures, but we want defense-in-depth: a DNS
 * miss or a refused connection must never become an unhandled rejection.
 */
class SilentExporter implements SpanExporter {
  constructor(private readonly inner: SpanExporter) {}

  export(...args: Parameters<SpanExporter["export"]>): void {
    try {
      this.inner.export(args[0], (result) => {
        if (result.code !== 0) {
          warnOnce(
            `OTLP export failed (${result.error?.message ?? "unknown error"}) at ${getConfig().endpoint}`,
          );
        }
        args[1](result);
      });
    } catch (err) {
      warnOnce(`OTLP exporter threw: ${(err as Error).message ?? String(err)}`);
      // Still call the user callback so BatchSpanProcessor doesn't hang.
      args[1]({ code: 1, error: err as Error });
    }
  }

  shutdown(): Promise<void> {
    return this.inner.shutdown().catch(() => undefined);
  }

  forceFlush?(): Promise<void> {
    return this.inner.forceFlush?.().catch(() => undefined) ?? Promise.resolve();
  }
}

function buildProvider(exporterOverride?: SpanExporter): Internal {
  const cfg = getConfig();
  const exporter =
    exporterOverride ??
    new SilentExporter(
      new OTLPTraceExporter({
        url: cfg.endpoint,
        timeoutMillis: cfg.timeoutMs,
      }),
    );

  const processor = new BatchSpanProcessor(exporter, {
    // Tighter than OTel defaults — we want spans to land quickly in the
    // dashboard during dev, since clustertrace is local-first.
    scheduledDelayMillis: 500,
    maxQueueSize: 2048,
    maxExportBatchSize: 256,
    exportTimeoutMillis: cfg.timeoutMs,
  });

  const provider = new NodeTracerProvider({
    resource: new Resource({
      [ATTR_SERVICE_NAME]: cfg.serviceName,
      [ATTR_SERVICE_VERSION]: TRACER_VERSION,
    }),
    sampler: cfg.sampleRate >= 1
      ? undefined
      : {
          shouldSample: () => ({
            decision: Math.random() < cfg.sampleRate ? 2 : 0, // 2 = RECORD_AND_SAMPLED, 0 = NOT_RECORD
          }),
          toString: () => `ClustertraceRatioSampler{${cfg.sampleRate}}`,
        },
  });
  provider.addSpanProcessor(processor);

  provider.register();
  // Use the provider's own tracer (not the global API) so test re-init is reliable.
  // The global API caches the provider that was first registered; subsequent
  // register() calls in the same process don't replace it. Using
  // provider.getTracer ensures every test gets a tracer pointing at the right
  // exporter.
  const tracer = provider.getTracer(TRACER_NAME, TRACER_VERSION);

  return { provider, processor, tracer };
}

/**
 * Returns the (lazily-constructed) tracer. Subsequent calls reuse the same
 * instance. To re-init with a different config, call `shutdownExporter()`
 * first — this is mostly useful for tests.
 */
export function getTracer(): Tracer {
  if (!initialized) {
    initialized = buildProvider();
  }
  return initialized.tracer;
}

/** Test-only escape hatch — installs an in-memory exporter so tests can assert. */
export function _initWithExporterForTests(exporter: SpanExporter): void {
  if (initialized) {
    void initialized.processor.shutdown();
  }
  initialized = buildProvider(exporter);
  warned = false;
}

/** Awaits the BatchSpanProcessor — exposed as the public `flush()`. */
export async function flushSpans(): Promise<void> {
  if (!initialized) return;
  try {
    await initialized.processor.forceFlush();
  } catch {
    // Per hard rule: never throw out of public API.
  }
}

export async function shutdownExporter(): Promise<void> {
  if (!initialized) return;
  try {
    await initialized.processor.shutdown();
  } catch {
    // Per hard rule: never throw out of public API.
  }
  initialized = undefined;
  warned = false;
}

/** Test-only reset of the warned-once latch. */
export function _resetWarnedForTests(): void {
  warned = false;
}
