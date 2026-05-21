---
name: 'Wanted: streaming chunk capture'
about: Help wanted — capture streaming response chunks as they arrive
title: 'Capture streaming response chunks instead of completion-only'
labels: enhancement, help wanted
assignees: ''
---

## What

Today, when you call `messages.create(stream=True)` or `chat.completions.create(stream=True)`, agentlog records the span on completion only and tags it with `streaming: true`. It does **not** capture the intermediate chunks.

We want: chunk-by-chunk capture so debugging an agent that gets stuck mid-stream (or fails partway through) is possible.

## Why

Streaming is the default for most production agents. Without chunk capture, agentlog is invisible to that workflow.

## Design sketch

- Wrap the returned stream/iterator in a `_StreamingResponseWrapper`.
- On each `__next__`, append to an in-memory list on the wrapper.
- On `__exit__` / `StopIteration`, write the accumulated chunks into `spans.output_json` as a list, plus a `chunks_received: N` attr.
- Be careful with the OpenAI streaming context manager (`with client.chat.completions.create(stream=True) as stream:`) — needs `__enter__` / `__exit__` proxies.
- Handle async streams (`async for chunk in stream`) too — `__aiter__` / `__anext__`.

## Pointers

- [`src/agentlog/anthropic.py`](../../src/agentlog/anthropic.py) `_WrappedMessages.create`.
- [`src/agentlog/openai.py`](../../src/agentlog/openai.py) `_WrappedCompletions.create`.

Estimated effort: 300 LOC + 200 LOC tests, a full day. The fiddly bits are sync vs async + context-manager vs iterator semantics.
