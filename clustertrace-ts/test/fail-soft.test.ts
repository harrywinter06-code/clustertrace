/**
 * The "endpoint unreachable" hard rule: if the dashboard is down, the SDK
 * must NOT throw out of any public API, and the user's app must keep running.
 *
 * We point the exporter at a definitely-closed port and assert that:
 *   1. `trace()` returns normally
 *   2. `flush()` resolves without throwing
 *   3. No unhandled rejection escapes
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configure, flush, trace } from "../src/index.js";
import { _resetConfigForTests } from "../src/config.js";
import { _resetWarnedForTests, shutdownExporter } from "../src/exporter.js";

describe("fail-soft contract", () => {
  beforeEach(() => {
    _resetConfigForTests();
    _resetWarnedForTests();
  });
  afterEach(async () => {
    await shutdownExporter();
  });

  it("does not throw when the endpoint is unreachable", async () => {
    // Closed port — connection refused.
    configure({ endpoint: "http://127.0.0.1:1/v1/traces", timeoutMs: 250 });

    // Silence console.warn for the duration of this test.
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const work = trace(() => "result", { name: "offline" });
    expect(work()).toBe("result");

    // flush is the most likely place for an internal error to bubble.
    await expect(flush()).resolves.toBeUndefined();

    warnSpy.mockRestore();
  });
});
