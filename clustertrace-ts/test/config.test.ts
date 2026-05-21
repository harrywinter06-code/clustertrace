import { describe, expect, it, beforeEach } from "vitest";

import { configure, type ClustertraceConfig } from "../src/index.js";
import { _resetConfigForTests, getConfig } from "../src/config.js";

describe("configure()", () => {
  beforeEach(() => {
    _resetConfigForTests();
  });

  it("starts with sensible defaults", () => {
    const cfg = getConfig();
    expect(cfg.endpoint).toBe("http://127.0.0.1:7777/v1/traces");
    expect(cfg.sampleRate).toBe(1.0);
    expect(cfg.timeoutMs).toBeGreaterThan(0);
  });

  it("merges partial overrides", () => {
    configure({ endpoint: "http://example.com/v1/traces" });
    const cfg = getConfig();
    expect(cfg.endpoint).toBe("http://example.com/v1/traces");
    expect(cfg.sampleRate).toBe(1.0); // unchanged
  });

  it("exposes the ClustertraceConfig type", () => {
    const cfg: Partial<ClustertraceConfig> = { sampleRate: 0.5 };
    configure(cfg);
    expect(getConfig().sampleRate).toBe(0.5);
  });
});
