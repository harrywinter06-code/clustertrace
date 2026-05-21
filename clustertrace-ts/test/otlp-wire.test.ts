/**
 * End-to-end wire format check: stand up a tiny HTTP server, point the SDK
 * at it, fire a span, and assert the POST body looks like OTLP/JSON that
 * the Python `/v1/traces` endpoint can decode.
 *
 * This catches breakage in the OTel→OTLP encoder version we depend on.
 */
import http from "node:http";
import type { AddressInfo } from "node:net";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { configure, flush, trace } from "../src/index.js";
import { _resetConfigForTests } from "../src/config.js";
import { shutdownExporter } from "../src/exporter.js";

describe("OTLP/JSON wire format", () => {
  let server: http.Server;
  let received: unknown;
  let url: string;

  beforeEach(async () => {
    _resetConfigForTests();
    received = undefined;
    server = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        try {
          received = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
        } catch {
          received = null;
        }
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ partialSuccess: {} }));
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = (server.address() as AddressInfo).port;
    url = `http://127.0.0.1:${port}/v1/traces`;
  });

  afterEach(async () => {
    await shutdownExporter();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  it("POSTs an ExportTraceServiceRequest body with resourceSpans → scopeSpans → spans", async () => {
    configure({ endpoint: url, timeoutMs: 4_000 });

    const work = trace(() => "ok", { name: "wire-test", tags: { agent: "x" } });
    work();
    await flush();

    expect(received).toBeTruthy();
    const body = received as {
      resourceSpans?: Array<{
        scopeSpans?: Array<{ spans?: Array<{ name?: string; attributes?: unknown[] }> }>;
      }>;
    };
    expect(Array.isArray(body.resourceSpans)).toBe(true);
    expect(body.resourceSpans!.length).toBeGreaterThan(0);
    const span = body.resourceSpans![0].scopeSpans![0].spans![0];
    expect(span.name).toBe("wire-test");
    // The attributes array should include our tag in KeyValue shape.
    const attrs = (span.attributes ?? []) as Array<{ key: string; value: { stringValue?: string } }>;
    expect(attrs.some((a) => a.key === "clustertrace.tag.agent")).toBe(true);
  });
});
