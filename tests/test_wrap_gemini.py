"""Verify wrap_gemini logs google-genai generate_content calls."""

import clustertrace
from clustertrace import storage


class FakeUsageMetadata:
    prompt_token_count = 8
    candidates_token_count = 5
    total_token_count = 13


class FakeGeminiResponse:
    model_version = "gemini-2.5-flash"
    text = "hello"
    usage_metadata = FakeUsageMetadata()


class FakeModels:
    def generate_content(self, **kwargs):
        return FakeGeminiResponse()

    def generate_content_stream(self, **kwargs):
        return iter([FakeGeminiResponse()])


class FakeGeminiClient:
    custom = "yes"

    def __init__(self):
        self.models = FakeModels()


def test_wrap_gemini_logs_generate_content():
    wrapped = clustertrace.wrap_gemini(FakeGeminiClient())

    resp = wrapped.models.generate_content(model="gemini-2.5-flash", contents="hi")

    assert resp.text == "hello"
    with storage.connect() as conn:
        row = conn.execute("SELECT name, kind, status, attrs_json FROM spans").fetchone()

    assert row["kind"] == "llm_call"
    assert row["status"] == "ok"
    assert "gemini.models.generate_content:gemini-2.5-flash" in row["name"]
    assert '"provider": "gemini"' in row["attrs_json"]
    assert '"input_tokens": 8' in row["attrs_json"]
    assert '"output_tokens": 5' in row["attrs_json"]


def test_wrap_gemini_logs_streaming_call():
    wrapped = clustertrace.wrap_gemini(FakeGeminiClient())

    chunks = list(wrapped.models.generate_content_stream(model="gemini-2.5-flash", contents="hi"))

    assert len(chunks) == 1
    with storage.connect() as conn:
        row = conn.execute("SELECT status, attrs_json FROM spans").fetchone()

    assert row["status"] == "ok"
    assert '"streaming": true' in row["attrs_json"]


def test_wrap_gemini_exposes_unrelated_attrs():
    wrapped = clustertrace.wrap_gemini(FakeGeminiClient())

    assert wrapped.custom == "yes"
