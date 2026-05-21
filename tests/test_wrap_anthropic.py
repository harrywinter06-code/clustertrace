"""Verify wrap_anthropic logs calls and propagates exceptions, using a fake client."""
import pytest

import clustertrace
from clustertrace import storage


class FakeUsage:
    def __init__(self, in_tok, out_tok):
        self.input_tokens = in_tok
        self.output_tokens = out_tok


class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self):
        self.id = "msg_fake_123"
        self.model = "claude-haiku-4-5-20251001"
        self.stop_reason = "end_turn"
        self.role = "assistant"
        self.content = [FakeTextBlock("hello world")]
        self.usage = FakeUsage(in_tok=10, out_tok=5)


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("model") == "raise":
            raise RuntimeError("simulated API error")
        return FakeResponse()


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def test_wrap_logs_successful_call():
    wrapped = clustertrace.wrap_anthropic(FakeClient())
    resp = wrapped.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.id == "msg_fake_123"

    with storage.connect() as c:
        rows = c.execute("SELECT name, kind, status, attrs_json, output_json FROM spans").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "llm_call"
    assert rows[0]["status"] == "ok"
    assert "claude-haiku" in rows[0]["name"]
    attrs = rows[0]["attrs_json"]
    assert '"input_tokens": 10' in attrs
    assert '"output_tokens": 5' in attrs


def test_wrap_propagates_and_logs_exception():
    wrapped = clustertrace.wrap_anthropic(FakeClient())
    with pytest.raises(RuntimeError):
        wrapped.messages.create(model="raise", max_tokens=10, messages=[])

    with storage.connect() as c:
        row = c.execute("SELECT status, error_type FROM spans").fetchone()
        trace = c.execute("SELECT status, error_type FROM traces").fetchone()
    assert row["status"] == "error"
    assert row["error_type"] == "RuntimeError"
    assert trace["status"] == "error"


def test_wrap_does_not_break_unrelated_attrs():
    """Wrapper should still expose anything else the client had via __getattr__."""

    class WithExtras(FakeClient):
        custom = "yes"

    wrapped = clustertrace.wrap_anthropic(WithExtras())
    assert wrapped.custom == "yes"


def test_wrap_under_trace_creates_nested_span():
    """When called inside a @trace function, the llm_call span nests under it."""
    wrapped = clustertrace.wrap_anthropic(FakeClient())

    @clustertrace.trace
    def calls():
        wrapped.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=10, messages=[]
        )

    calls()
    with storage.connect() as c:
        spans = c.execute("SELECT name, kind, parent_id FROM spans ORDER BY started_at").fetchall()
    assert len(spans) == 2
    # llm_call's parent should be the @trace function span
    assert spans[1]["parent_id"] is not None
