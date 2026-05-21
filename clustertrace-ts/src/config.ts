/**
 * Global SDK configuration.
 *
 * Held as a module-level singleton; `configure()` mutates it. The exporter
 * reads from here at first-span time, which means `configure()` MUST be
 * called before the first traced function — same as the Python SDK.
 */

export interface ClustertraceConfig {
  /** OTLP/HTTP endpoint that accepts span batches. */
  endpoint: string;
  /** 0.0–1.0; spans below this random draw are dropped. 1.0 = log everything. */
  sampleRate: number;
  /**
   * Logical service name attached as resource attribute. Shows up as a tag in
   * the dashboard so you can split traces from multiple TS apps.
   */
  serviceName: string;
  /**
   * Per-export timeout (ms). Spans are dropped (with one warning) if the
   * Python side is unreachable — never blocks the user's code.
   */
  timeoutMs: number;
}

const DEFAULTS: ClustertraceConfig = {
  endpoint: "http://127.0.0.1:7777/v1/traces",
  sampleRate: 1.0,
  serviceName: "clustertrace-ts",
  timeoutMs: 5_000,
};

let current: ClustertraceConfig = { ...DEFAULTS };

/**
 * Override one or more config fields. Subsequent traces use the new values.
 * The OTel exporter is constructed lazily on the first span, so calling
 * this before any tracing happens reliably picks up the new endpoint.
 */
export function configure(opts: Partial<ClustertraceConfig>): void {
  current = { ...current, ...opts };
}

export function getConfig(): Readonly<ClustertraceConfig> {
  return current;
}

/** Test-only: reset to defaults. Not part of the public surface. */
export function _resetConfigForTests(): void {
  current = { ...DEFAULTS };
}
