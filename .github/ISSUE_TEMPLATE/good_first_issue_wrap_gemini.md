---
name: 'Wanted: native wrap_gemini'
about: Help wanted — native Google Gemini wrapper
title: 'Add wrap_gemini for google-genai SDK'
labels: enhancement, good first issue, help wanted
assignees: ''
---

## What

`clustertrace.wrap_gemini(client)` wrapping the `google.genai.Client.models.generate_content` (and the streaming + async variants).

## Why

Gemini works through OpenTelemetry today, but a native wrapper:
- captures fields the OTel auto-instrumentor doesn't (function-calling tool args, safety settings)
- lands costs from the bundled pricing table without needing the OTel `gen_ai.*` attribute mapping
- gives a more accurate `messages.create`-style span shape

## Pointers

- Mirror [`src/clustertrace/openai.py`](../../src/clustertrace/openai.py).
- Watch out for sync vs async (`Client` vs `AsyncClient`).
- Add `google-genai` as an extra: `clustertrace[gemini]`.
- Add prices to [`src/clustertrace/cost.py`](../../src/clustertrace/cost.py) — `gemini-2.5-pro`, `gemini-2.5-flash`, etc.
- Tests in `tests/test_wrap_gemini.py` against a `FakeGeminiClient`.

[CONTRIBUTING.md](../../CONTRIBUTING.md) has the recipe.

Estimated effort: 250 LOC code + 150 LOC tests, half a day.
