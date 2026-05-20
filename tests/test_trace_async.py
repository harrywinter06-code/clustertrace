import asyncio

import pytest

import agentlog
from agentlog import storage


async def test_decorator_captures_async_io():
    @agentlog.trace
    async def mul(a, b):
        await asyncio.sleep(0)
        return a * b

    assert await mul(3, 4) == 12

    with storage.connect() as c:
        spans = c.execute("SELECT name, status, output_json FROM spans").fetchall()
    assert len(spans) == 1
    assert spans[0]["status"] == "ok"
    assert spans[0]["output_json"] == "12"


async def test_decorator_captures_async_exception():
    @agentlog.trace
    async def boom():
        await asyncio.sleep(0)
        raise KeyError("missing")

    with pytest.raises(KeyError):
        await boom()

    with storage.connect() as c:
        t = c.execute("SELECT status, error_type FROM traces").fetchone()
        s = c.execute("SELECT status, error_type FROM spans").fetchone()
    assert t["status"] == "error"
    assert t["error_type"] == "KeyError"
    assert s["status"] == "error"


async def test_concurrent_async_traces_isolated():
    """Each top-level coroutine gets its own trace; spans don't leak across tasks."""

    @agentlog.trace
    async def task(n):
        await asyncio.sleep(0)
        with agentlog.span(f"inner_{n}"):
            await asyncio.sleep(0)
        return n

    results = await asyncio.gather(task(1), task(2), task(3))
    assert results == [1, 2, 3]

    with storage.connect() as c:
        traces = c.execute("SELECT id, status FROM traces").fetchall()
        spans = c.execute("SELECT trace_id, name FROM spans").fetchall()

    assert len(traces) == 3
    assert all(t["status"] == "ok" for t in traces)
    # exactly 2 spans per trace (the @trace span + the inner span)
    from collections import Counter

    counts = Counter(s["trace_id"] for s in spans)
    assert all(v == 2 for v in counts.values())
