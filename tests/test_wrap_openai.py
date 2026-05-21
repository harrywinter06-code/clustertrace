"""Verify wrap_openai logs chat.completions.create on sync + async clients."""
import pytest

import clustertrace
from clustertrace import storage


class FakeUsage:
    def __init__(self):
        self.prompt_tokens = 12
        self.completion_tokens = 7
        self.total_tokens = 19


class FakeMessage:
    def __init__(self, content="hi", tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, content="hi", finish_reason="stop"):
        self.index = 0
        self.finish_reason = finish_reason
        self.message = FakeMessage(content=content)


class FakeResponse:
    def __init__(self):
        self.id = "chatcmpl_fake"
        self.model = "gpt-4o-mini"
        self.object = "chat.completion"
        self.created = 1700000000
        self.choices = [FakeChoice()]
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("model") == "raise":
            raise RuntimeError("boom")
        return FakeResponse()


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeOpenAI:
    def __init__(self):
        self.chat = FakeChat()


class FakeAsyncCompletions:
    async def create(self, **kwargs):
        if kwargs.get("model") == "raise":
            raise RuntimeError("boom")
        return FakeResponse()


class FakeAsyncChat:
    def __init__(self):
        self.completions = FakeAsyncCompletions()


class FakeAsyncOpenAI:
    def __init__(self):
        self.chat = FakeAsyncChat()


def test_wrap_logs_successful_sync_call():
    wrapped = clustertrace.wrap_openai(FakeOpenAI())
    resp = wrapped.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=50,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.id == "chatcmpl_fake"
    with storage.connect() as c:
        row = c.execute("SELECT name, kind, status, attrs_json FROM spans").fetchone()
    assert row["kind"] == "llm_call"
    assert row["status"] == "ok"
    assert "gpt-4o-mini" in row["name"]
    assert '"prompt_tokens": 12' in row["attrs_json"]
    assert '"completion_tokens": 7' in row["attrs_json"]


def test_wrap_propagates_sync_exception():
    wrapped = clustertrace.wrap_openai(FakeOpenAI())
    with pytest.raises(RuntimeError):
        wrapped.chat.completions.create(model="raise", max_tokens=10, messages=[])
    with storage.connect() as c:
        s = c.execute("SELECT status, error_type FROM spans").fetchone()
        t = c.execute("SELECT status, error_type FROM traces").fetchone()
    assert s["status"] == "error"
    assert s["error_type"] == "RuntimeError"
    assert t["status"] == "error"


async def test_wrap_logs_async_call():
    wrapped = clustertrace.wrap_openai(FakeAsyncOpenAI())
    resp = await wrapped.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=50,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.id == "chatcmpl_fake"
    with storage.connect() as c:
        row = c.execute("SELECT status, kind FROM spans").fetchone()
    assert row["status"] == "ok"
    assert row["kind"] == "llm_call"


def test_wrap_nested_under_trace():
    wrapped = clustertrace.wrap_openai(FakeOpenAI())

    @clustertrace.trace
    def calls():
        wrapped.chat.completions.create(
            model="gpt-4o-mini", max_tokens=10, messages=[]
        )

    calls()
    with storage.connect() as c:
        spans = c.execute("SELECT name, parent_id FROM spans ORDER BY started_at").fetchall()
    assert len(spans) == 2
    assert spans[1]["parent_id"] is not None  # llm_call nested under @trace span


def test_wrap_exposes_other_attrs():
    class WithExtras(FakeOpenAI):
        custom = "yep"

    wrapped = clustertrace.wrap_openai(WithExtras())
    assert wrapped.custom == "yep"
