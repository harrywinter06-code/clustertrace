import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { flush, wrapOpenAI } from "../src/index.js";
import { installMemoryExporter, teardown } from "./helpers.js";
import type { InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";

function makeFakeOpenAI() {
  return {
    chat: {
      completions: {
        async create(_params: unknown) {
          return {
            id: "chatcmpl-1",
            model: "gpt-4o-mini",
            usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
            choices: [{ finish_reason: "stop" }],
          };
        },
      },
    },
  };
}

describe("wrapOpenAI()", () => {
  let mem: InMemorySpanExporter;
  beforeEach(() => {
    mem = installMemoryExporter();
  });
  afterEach(async () => {
    await teardown();
  });

  it("wraps chat.completions.create and emits an llm_call span", async () => {
    const client = wrapOpenAI(makeFakeOpenAI());
    const resp = await client.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
    });
    expect((resp as { id: string }).id).toBe("chatcmpl-1");

    await flush();
    const s = mem.getFinishedSpans().find((x) => x.name.startsWith("openai.chat.completions.create"));
    expect(s).toBeDefined();
    expect(s!.attributes["clustertrace.kind"]).toBe("llm_call");
    expect(s!.attributes["gen_ai.system"]).toBe("openai");
    expect(s!.attributes["gen_ai.request.model"]).toBe("gpt-4o-mini");
    expect(s!.attributes["gen_ai.usage.input_tokens"]).toBe(10);
    expect(s!.attributes["gen_ai.usage.output_tokens"]).toBe(5);
    expect(s!.attributes["gen_ai.response.finish_reasons"]).toBe("stop");
  });

  it("propagates errors and marks the span as failed", async () => {
    const fake = {
      chat: {
        completions: {
          async create() {
            throw new Error("invalid api key");
          },
        },
      },
    };
    const client = wrapOpenAI(fake);
    await expect(
      client.chat.completions.create({ model: "gpt-4o-mini", messages: [] }),
    ).rejects.toThrow("invalid api key");
    await flush();
    const s = mem.getFinishedSpans().find((x) => x.name.startsWith("openai.chat.completions.create"));
    expect(s!.status.code).toBe(2);
  });
});
