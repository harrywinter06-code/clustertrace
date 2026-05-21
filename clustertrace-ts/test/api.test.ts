import { describe, expect, it, afterEach, beforeEach } from "vitest";

import { flush, metric, span, tag, toolCall, trace } from "../src/index.js";
import { installMemoryExporter, teardown } from "./helpers.js";
import type { InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";

describe("trace()", () => {
  let mem: InMemorySpanExporter;
  beforeEach(() => {
    mem = installMemoryExporter();
  });
  afterEach(async () => {
    await teardown();
  });

  it("wraps a sync function and emits one span on call", async () => {
    const work = trace((a: number, b: number) => a + b, { name: "sum" });
    expect(work(2, 3)).toBe(5);
    await flush();
    const spans = mem.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].name).toBe("sum");
    expect(spans[0].status.code).toBe(1); // OK
  });

  it("wraps an async function and waits for the promise", async () => {
    const work = trace(
      async (q: string) => {
        await new Promise((r) => setTimeout(r, 5));
        return q.toUpperCase();
      },
      { name: "ask" },
    );
    expect(await work("hi")).toBe("HI");
    await flush();
    const spans = mem.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].name).toBe("ask");
    expect(spans[0].status.code).toBe(1);
  });

  it("records errors on sync throws and re-throws", async () => {
    const work = trace(() => {
      throw new Error("boom");
    }, { name: "broken" });
    expect(() => work()).toThrow("boom");
    await flush();
    const spans = mem.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(2); // ERROR
    expect(spans[0].events.some((e) => e.name === "exception")).toBe(true);
  });

  it("records errors on async rejections", async () => {
    const work = trace(async () => {
      throw new Error("async-boom");
    }, { name: "broken-async" });
    await expect(work()).rejects.toThrow("async-boom");
    await flush();
    const spans = mem.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(2);
  });

  it("applies tags as clustertrace.tag.* attributes", async () => {
    const work = trace(() => null, { name: "tagged", tags: { agent: "research", v: "1" } });
    work();
    await flush();
    const s = mem.getFinishedSpans()[0];
    expect(s.attributes["clustertrace.tag.agent"]).toBe("research");
    expect(s.attributes["clustertrace.tag.v"]).toBe("1");
  });

  it("uses the function's name when no name is supplied", async () => {
    function myAgent() {
      return 42;
    }
    trace(myAgent)();
    await flush();
    expect(mem.getFinishedSpans()[0].name).toBe("myAgent");
  });
});

describe("span()", () => {
  let mem: InMemorySpanExporter;
  beforeEach(() => {
    mem = installMemoryExporter();
  });
  afterEach(async () => {
    await teardown();
  });

  it("creates a child span nested under the parent trace", async () => {
    const outer = trace(() => {
      span("inner", () => 7);
    }, { name: "outer" });
    outer();
    await flush();
    const spans = mem.getFinishedSpans();
    expect(spans).toHaveLength(2);
    const inner = spans.find((s) => s.name === "inner");
    const outerSpan = spans.find((s) => s.name === "outer");
    expect(inner).toBeDefined();
    expect(outerSpan).toBeDefined();
    // OTel SDK 1.x exposes parent linkage via parentSpanId; 2.x renames to
    // parentSpanContext. Accept either to keep this test version-agnostic.
    const parentId =
      (inner as unknown as { parentSpanId?: string }).parentSpanId ??
      (inner as unknown as { parentSpanContext?: { spanId: string } }).parentSpanContext?.spanId;
    expect(parentId).toBe(outerSpan!.spanContext().spanId);
  });

  it("works with async child fns", async () => {
    const outer = trace(async () => {
      await span("retrieve", async () => {
        await new Promise((r) => setTimeout(r, 3));
        return "ok";
      });
    }, { name: "outer-async" });
    await outer();
    await flush();
    const inner = mem.getFinishedSpans().find((s) => s.name === "retrieve");
    expect(inner).toBeDefined();
    expect(inner!.status.code).toBe(1);
  });

  it("records the child span's error without losing the throw", async () => {
    const outer = trace(() => {
      expect(() =>
        span("bad", () => {
          throw new RangeError("oob");
        }),
      ).toThrow("oob");
    }, { name: "outer-err" });
    outer();
    await flush();
    const inner = mem.getFinishedSpans().find((s) => s.name === "bad");
    expect(inner).toBeDefined();
    expect(inner!.status.code).toBe(2);
  });

  it("applies attrs to the span", async () => {
    const outer = trace(() => {
      span("step", () => null, { step: "embed", retries: 0 });
    }, { name: "outer" });
    outer();
    await flush();
    const inner = mem.getFinishedSpans().find((s) => s.name === "step");
    expect(inner?.attributes.step).toBe("embed");
    expect(inner?.attributes.retries).toBe(0);
  });
});

describe("toolCall()", () => {
  let mem: InMemorySpanExporter;
  beforeEach(() => {
    mem = installMemoryExporter();
  });
  afterEach(async () => {
    await teardown();
  });

  it("emits a tool_call span with args + result", async () => {
    const outer = trace(() => {
      toolCall("web_search", { args: { q: "anthropic" }, result: { hits: 3 } });
    }, { name: "outer" });
    outer();
    await flush();
    const tool = mem.getFinishedSpans().find((s) => s.name === "tool:web_search");
    expect(tool).toBeDefined();
    expect(tool!.attributes["clustertrace.kind"]).toBe("tool_call");
    expect(tool!.attributes["tool.name"]).toBe("web_search");
    expect(tool!.attributes["tool.args_json"]).toContain("anthropic");
    expect(tool!.attributes["tool.result_json"]).toContain("hits");
    expect(tool!.status.code).toBe(1);
  });

  it("marks the span as error when an error option is provided", async () => {
    const outer = trace(() => {
      toolCall("flaky", { args: {}, error: new Error("nope") });
    }, { name: "outer" });
    outer();
    await flush();
    const tool = mem.getFinishedSpans().find((s) => s.name === "tool:flaky");
    expect(tool!.status.code).toBe(2);
  });
});

describe("tag() and metric()", () => {
  let mem: InMemorySpanExporter;
  beforeEach(() => {
    mem = installMemoryExporter();
  });
  afterEach(async () => {
    await teardown();
  });

  it("attaches a tag to the active span", async () => {
    const outer = trace(() => {
      tag("model", "claude-haiku-4-5");
      metric("score", 0.95);
    }, { name: "outer" });
    outer();
    await flush();
    const s = mem.getFinishedSpans().find((x) => x.name === "outer")!;
    expect(s.attributes["clustertrace.tag.model"]).toBe("claude-haiku-4-5");
    expect(s.attributes["clustertrace.metric.score"]).toBe(0.95);
  });

  it("is a no-op outside an active span", () => {
    // Should not throw.
    expect(() => tag("k", "v")).not.toThrow();
    expect(() => metric("m", 1)).not.toThrow();
  });

  it("rejects non-finite metric values", async () => {
    const outer = trace(() => {
      metric("bad", Number.NaN);
      metric("worse", Number.POSITIVE_INFINITY);
    }, { name: "outer" });
    outer();
    await flush();
    const s = mem.getFinishedSpans().find((x) => x.name === "outer")!;
    expect(s.attributes["clustertrace.metric.bad"]).toBeUndefined();
    expect(s.attributes["clustertrace.metric.worse"]).toBeUndefined();
  });
});
