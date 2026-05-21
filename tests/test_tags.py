"""Tags attached via decorator + runtime API, with filtering."""
import clustertrace
from clustertrace import storage


def test_decorator_tags_persisted():
    @clustertrace.trace(tags={"agent": "rag", "version": "v1"})
    def go(): pass
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    assert storage.get_trace_tags(tid) == {"agent": "rag", "version": "v1"}


def test_runtime_tag_adds_to_current_trace():
    @clustertrace.trace
    def go():
        clustertrace.tag("user", "alice")
        clustertrace.tag("plan_steps", 3)  # non-string value gets coerced
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    tags = storage.get_trace_tags(tid)
    assert tags["user"] == "alice"
    assert tags["plan_steps"] == "3"


def test_tag_outside_trace_is_noop():
    clustertrace.tag("orphan", "x")  # must not raise
    with storage.connect() as c:
        rows = c.execute("SELECT * FROM trace_tags").fetchall()
    assert rows == []


def test_decorator_and_runtime_tags_merge():
    @clustertrace.trace(tags={"agent": "researcher"})
    def go():
        clustertrace.tag("query", "papers")
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    assert storage.get_trace_tags(tid) == {"agent": "researcher", "query": "papers"}


def test_same_key_replaces():
    @clustertrace.trace(tags={"k": "v1"})
    def go():
        clustertrace.tag("k", "v2")  # later wins
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    assert storage.get_trace_tags(tid) == {"k": "v2"}
