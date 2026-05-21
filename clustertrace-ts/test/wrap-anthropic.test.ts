import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { flush, wrapAnthropic } from "../src/index.js";
import { installMemoryExporter, teardown } from "./helpers.js";
import type { InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";

function makeFakeAnthropic(opts: {
  response?: unknown;
  throws?: Error;
} = {}) {
  const fake = {
    messages: {
      async create(_params: unknown) {
        if (opts.throws) throw opts.throws;
        return (
          opts.response ?? {
            id: "msg_123",
            model: "claude-haiku-4-5",
            stop_reason: "end_turn",
            usage: { input_tokens: 12, output_tokens: 7 },
          }
        );
      },
    },
  };
  return fake;
}

describe("wrapAnthropic()", () => {
  let mem: InMemorySpanExporter;
  beforeEach(() => {
    mem = installMemoryExporter();
  });
  afterEach(async () => {
    await teardown();
  });

  it("wraps messages.create and emits an llm_call span with usage", async () => {
    const client = wrapAnthropic(makeFakeAnthropic());
    const resp = await client.messages.create({
      model: "claude-haiku-4-5",
      max_tokens: 100,
      messages: [{ role: "user", content: "hi" }],
    });
    expect((resp as { id: string }).id).toBe("msg_123");

    await flush();
    const s = mem.getFinishedSpans().find((x) => x.name.startsWith("anthropic.messages.create"));
    expect(s).toBeDefined();
    expect(s!.attributes["clustertrace.kind"]).toBe("llm_call");
    expect(s!.attributes["gen_ai.system"]).toBe("anthropic");
    expect(s!.attributes["gen_ai.request.model"]).toBe("claude-haiku-4-5");
    expect(s!.attributes["gen_ai.request.max_tokens"]).toBe(100);
    expect(s!.attributes["gen_ai.usage.input_tokens"]).toBe(12);
    expect(s!.attributes["gen_ai.usage.output_tokens"]).toBe(7);
    expect(s!.attributes["gen_ai.response.finish_reasons"]).toBe("end_turn");
    expect(s!.status.code).toBe(1);
  });

  it("propagates errors and marks the span as failed", async () => {
    const client = wrapAnthropic(
      makeFakeAnthropic({ throws: new Error("rate limited") }),
    );
    await expect(
      client.messages.create({ model: "claude-haiku-4-5", messages: [] }),
    ).rejects.toThrow("rate limited");

    await flush();
    const s = mem.getFinishedSpans().find((x) => x.name.startsWith("anthropic.messages.create"));
    expect(s).toBeDefined();
    expect(s!.status.code).toBe(2);
  });

  it("leaves non-messages methods untouched (proxy passthrough)", async () => {
    const client = wrapAnthropic({
      messages: { create: async () => ({}) },
      apiKey: "sk-fake",
    } as unknown as Parameters<typeof wrapAnthropic>[0]);
    expect((client as unknown as { apiKey: string }).apiKey).toBe("sk-fake");
  });
});
