"""OpenTelemetry exporter — ingest OTel spans into agentlog."""
from dataclasses import dataclass, field

import pytest

from agentlog import storage
from agentlog.otel import AgentlogSpanExporter


@dataclass
class _Ctx:
    trace_id: int
    span_id: int


@dataclass
class _Status:
    status_code: object


@dataclass
class _SpanKind:
    name: str = "INTERNAL"


@dataclass
class _Event:
    name: str
    attributes: dict = field(default_factory=dict)


@dataclass
class _FakeSpan:
    name: str
    trace_id: int
    span_id: int
    parent: _Ctx | None
    start_time: int  # ns
    end_time: int | None
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    status_code_name: str = "OK"
    kind_name: str = "INTERNAL"

    def get_span_context(self):
        return _Ctx(self.trace_id, self.span_id)

    @property
    def status(self):
        class _C:
            def __init__(self, n): self.name = n
        return _Status(_C(self.status_code_name))

    @property
    def kind(self):
        return _SpanKind(self.kind_name)


def test_basic_export_creates_trace_and_span():
    exporter = AgentlogSpanExporter()
    s = _FakeSpan(
        name="my_op",
        trace_id=0x11111111111111111111111111111111,
        span_id=0x2222222222222222,
        parent=None,
        start_time=1_000_000_000,
        end_time=2_000_000_000,
    )
    result = exporter.export([s])
    assert result == 0

    with storage.connect() as c:
        t = c.execute("SELECT id, name, status FROM traces").fetchone()
        sp = c.execute("SELECT name, kind, status FROM spans").fetchone()
    assert t["name"] == "my_op"
    assert t["status"] == "ok"
    assert sp["status"] == "ok"
    assert sp["kind"] == "function"


def test_otel_llm_attributes_map_to_llm_call_kind():
    s = _FakeSpan(
        name="anthropic.chat",
        trace_id=0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,
        span_id=0xbbbbbbbbbbbbbbbb,
        parent=None,
        start_time=1_000_000_000,
        end_time=2_000_000_000,
        attributes={
            "gen_ai.request.model": "claude-haiku-4-5-20251001",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
        },
    )
    AgentlogSpanExporter().export([s])
    with storage.connect() as c:
        kind = c.execute("SELECT kind FROM spans").fetchone()["kind"]
    assert kind == "llm_call"


def test_otel_error_status_recorded():
    s = _FakeSpan(
        name="thing",
        trace_id=0x12121212121212121212121212121212,
        span_id=0x3434343434343434,
        parent=None,
        start_time=1_000_000_000,
        end_time=2_000_000_000,
        status_code_name="ERROR",
        events=[
            _Event(
                name="exception",
                attributes={"exception.type": "ValueError", "exception.message": "bad"},
            )
        ],
    )
    AgentlogSpanExporter().export([s])
    with storage.connect() as c:
        t = c.execute("SELECT status, error_type, error_message FROM traces").fetchone()
        sp = c.execute("SELECT status, error_type FROM spans").fetchone()
    assert t["status"] == "error"
    assert t["error_type"] == "ValueError"
    assert "bad" in (t["error_message"] or "")
    assert sp["status"] == "error"


def test_otel_child_error_promotes_to_trace_failure():
    """When a child span errors but the root is OK, the trace must still be marked failed.

    Otherwise the clusters page silently misses OTel-ingested failures.
    """
    parent_ctx = _Ctx(0x77777777777777777777777777777777, 0xaaaaaaaaaaaaaaaa)
    # Root span ends OK
    root = _FakeSpan(
        name="root", trace_id=parent_ctx.trace_id, span_id=parent_ctx.span_id,
        parent=None, start_time=1_000_000_000, end_time=3_000_000_000,
        status_code_name="OK",
    )
    # Child span has an exception event and ERROR status
    child = _FakeSpan(
        name="child", trace_id=parent_ctx.trace_id, span_id=0xbbbbbbbbbbbbbbbb,
        parent=parent_ctx, start_time=1_500_000_000, end_time=2_500_000_000,
        status_code_name="ERROR",
        events=[_Event(name="exception", attributes={"exception.type": "RuntimeError", "exception.message": "child broke"})],
    )
    # Insert child first, then root — OTel exporters can deliver out-of-order
    AgentlogSpanExporter().export([child, root])
    with storage.connect() as c:
        t = c.execute("SELECT status, error_type, error_message FROM traces").fetchone()
    assert t["status"] == "error", "OTel trace must be flagged failed when any child errored"
    assert t["error_type"] == "RuntimeError"
    assert "child broke" in (t["error_message"] or "")


def test_otel_parent_chain():
    """Two spans with the same trace_id but different parent relationships."""
    parent_ctx = _Ctx(0x99999999999999999999999999999999, 0xaaaaaaaaaaaaaaaa)
    root = _FakeSpan(
        name="root", trace_id=parent_ctx.trace_id, span_id=parent_ctx.span_id,
        parent=None, start_time=1_000_000_000, end_time=3_000_000_000,
    )
    child = _FakeSpan(
        name="child", trace_id=parent_ctx.trace_id, span_id=0xbbbbbbbbbbbbbbbb,
        parent=parent_ctx, start_time=1_500_000_000, end_time=2_500_000_000,
    )
    AgentlogSpanExporter().export([root, child])
    with storage.connect() as c:
        spans = c.execute("SELECT name, parent_id FROM spans ORDER BY started_at").fetchall()
    assert spans[0]["name"] == "root"
    assert spans[0]["parent_id"] is None
    assert spans[1]["name"] == "child"
    assert spans[1]["parent_id"] is not None


def test_malformed_span_does_not_break_batch():
    class _Bad:
        # Missing required attributes
        def get_span_context(self):
            raise RuntimeError("nope")

    s_good = _FakeSpan(
        name="ok_one", trace_id=0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee,
        span_id=0xffffffffffffffff, parent=None,
        start_time=1_000_000_000, end_time=2_000_000_000,
    )

    result = AgentlogSpanExporter().export([_Bad(), s_good])
    assert result == 0
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    # the good span still landed
    assert n == 1


def test_shutdown_and_force_flush_are_noops():
    exp = AgentlogSpanExporter()
    assert exp.shutdown() is None
    assert exp.force_flush() is True


def test_unused_pytest_import_for_lint():
    """Keeps the pytest import live so ruff doesn't complain."""
    assert pytest.__name__ == "pytest"
