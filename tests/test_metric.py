"""clustertrace.metric() attaches numeric values to traces."""
import clustertrace
from clustertrace import storage


def test_metric_persisted():
    @clustertrace.trace
    def go():
        clustertrace.metric("accuracy", 0.85)
        clustertrace.metric("steps", 5)
        clustertrace.metric("passed", True)  # coerces to 1.0
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    metrics = storage.get_trace_metrics(tid)
    assert metrics["accuracy"] == 0.85
    assert metrics["steps"] == 5.0
    assert metrics["passed"] == 1.0


def test_metric_outside_trace_is_noop():
    clustertrace.metric("orphan", 0.5)  # must not raise
    with storage.connect() as c:
        rows = c.execute("SELECT * FROM trace_metrics").fetchall()
    assert rows == []


def test_same_name_replaces():
    @clustertrace.trace
    def go():
        clustertrace.metric("score", 0.5)
        clustertrace.metric("score", 0.9)
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    assert storage.get_trace_metrics(tid) == {"score": 0.9}
