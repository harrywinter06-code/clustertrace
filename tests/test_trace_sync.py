import pytest

import agentlog
from agentlog import storage


def test_decorator_captures_sync_io():
    @agentlog.trace
    def add(a, b):
        return a + b

    assert add(2, 3) == 5

    with storage.connect() as c:
        traces = c.execute("SELECT * FROM traces").fetchall()
        spans = c.execute("SELECT * FROM spans").fetchall()

    assert len(traces) == 1
    assert traces[0]["status"] == "ok"
    assert len(spans) == 1
    span = spans[0]
    assert span["name"].endswith("add")
    assert span["status"] == "ok"
    assert span["output_json"] == "5"
    assert '"args": [2, 3]' in span["input_json"]


def test_decorator_captures_sync_exception():
    @agentlog.trace
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boom()

    with storage.connect() as c:
        trace_row = c.execute("SELECT * FROM traces").fetchone()
        span = c.execute("SELECT * FROM spans").fetchone()

    assert trace_row["status"] == "error"
    assert trace_row["error_type"] == "ValueError"
    assert "nope" in trace_row["error_message"]
    assert span["status"] == "error"


def test_span_nesting_under_trace():
    @agentlog.trace
    def outer():
        with agentlog.span("inner"):
            with agentlog.span("inner_inner"):
                pass

    outer()
    with storage.connect() as c:
        spans = c.execute("SELECT id, name, parent_id FROM spans ORDER BY started_at").fetchall()
    names = [s["name"] for s in spans]
    assert names[0].endswith("outer")
    assert names[1] == "inner"
    assert names[2] == "inner_inner"
    # parent chain: inner -> outer; inner_inner -> inner
    assert spans[1]["parent_id"] == spans[0]["id"]
    assert spans[2]["parent_id"] == spans[1]["id"]


def test_tool_call_logged():
    @agentlog.trace
    def runs():
        agentlog.tool_call("my_tool", args={"x": 1}, result={"y": 2})

    runs()
    with storage.connect() as c:
        rows = c.execute("SELECT name, kind, status, input_json, output_json FROM spans").fetchall()
    tool_rows = [r for r in rows if r["kind"] == "tool_call"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["name"] == "my_tool"
    assert tool_rows[0]["status"] == "ok"
    assert '"x": 1' in tool_rows[0]["input_json"]
    assert '"y": 2' in tool_rows[0]["output_json"]


def test_tool_call_with_error():
    @agentlog.trace
    def runs():
        agentlog.tool_call("flaky", args={}, error=RuntimeError("boom"))

    runs()
    with storage.connect() as c:
        rows = c.execute("SELECT name, status, error_type FROM spans WHERE kind='tool_call'").fetchall()
    assert rows[0]["status"] == "error"
    assert rows[0]["error_type"] == "RuntimeError"
