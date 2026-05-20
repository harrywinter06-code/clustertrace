"""agentlog.metric() attaches numeric values to traces."""
import agentlog
from agentlog import storage


def test_metric_persisted():
    @agentlog.trace
    def go():
        agentlog.metric("accuracy", 0.85)
        agentlog.metric("steps", 5)
        agentlog.metric("passed", True)  # coerces to 1.0
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    metrics = storage.get_trace_metrics(tid)
    assert metrics["accuracy"] == 0.85
    assert metrics["steps"] == 5.0
    assert metrics["passed"] == 1.0


def test_metric_outside_trace_is_noop():
    agentlog.metric("orphan", 0.5)  # must not raise
    with storage.connect() as c:
        rows = c.execute("SELECT * FROM trace_metrics").fetchall()
    assert rows == []


def test_same_name_replaces():
    @agentlog.trace
    def go():
        agentlog.metric("score", 0.5)
        agentlog.metric("score", 0.9)
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    assert storage.get_trace_metrics(tid) == {"score": 0.9}
