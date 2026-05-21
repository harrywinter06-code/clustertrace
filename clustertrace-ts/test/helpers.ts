/**
 * Test helpers — installs an InMemorySpanExporter so tests can assert
 * exactly which spans the SDK emitted without standing up a real HTTP server.
 */
import { InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";

import { _initWithExporterForTests, shutdownExporter } from "../src/exporter.js";

export function installMemoryExporter(): InMemorySpanExporter {
  const exp = new InMemorySpanExporter();
  _initWithExporterForTests(exp);
  return exp;
}

export async function teardown(): Promise<void> {
  await shutdownExporter();
}
