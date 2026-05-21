---
name: 'Wanted: native wrap_bedrock'
about: Help wanted — native AWS Bedrock wrapper for non-Anthropic-SDK paths
title: 'Add wrap_bedrock for the boto3 Bedrock runtime client'
labels: enhancement, good first issue, help wanted
assignees: ''
---

## What

A native `agentlog.wrap_bedrock(client)` that wraps `boto3.client("bedrock-runtime")` and logs `invoke_model` / `invoke_model_with_response_stream` calls.

## Why

Today, Bedrock works through `wrap_anthropic(AnthropicBedrock())` — but a lot of users hit Bedrock via the raw `boto3` client, which doesn't expose `.messages.create`. A native wrapper closes the gap and broadens addressable users meaningfully.

## Pointers

- Mirror the pattern in [`src/agentlog/anthropic.py`](../../src/agentlog/anthropic.py): `_record_*_span`, `_finish`, `_WrappedClient`.
- Parse Bedrock's `body` (it's a JSON-encoded string) to extract the model, input, output, and usage.
- Add `boto3` as an optional install extra: `agentlog[bedrock]`.
- Add a price entry in [`src/agentlog/cost.py`](../../src/agentlog/cost.py) for the Bedrock model IDs you support (`anthropic.claude-*-v2`, `meta.llama3-*`, etc.).
- Write `tests/test_wrap_bedrock.py` against a `FakeBedrockClient` (no AWS credentials needed).

[CONTRIBUTING.md](../../CONTRIBUTING.md) has the step-by-step recipe.

Estimated effort: 250 LOC code + 150 LOC tests, half a day.
