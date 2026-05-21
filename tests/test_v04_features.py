"""Tests for the v0.4 features: pool, auto-cost, set-mode clustering, sample/skip,
flush, vacuum, streaming attrs, pagination, per-tag failure prefix, versioned export."""
import sqlite3
import time

import pytest

import agentlog
from agentlog import cluster, export, maintenance, storage

# --- Connection pool ---

def test_connection_pool_reuses_connection_within_thread(isolated_db):
    """Successive connect() calls in the same thread should yield the SAME conn."""
    with storage.connect() as c1:
        id1 = id(c1)
    with storage.connect() as c2:
        id2 = id(c2)
    assert id1 == id2, "connections should be pooled, not reopened"


def test_reset_initialized_cache_closes_pooled_connections(tmp_path, monkeypatch):
    db1 = tmp_path / "first.db"
    monkeypatch.setenv("AGENTLOG_DB", str(db1))
    storage.reset_initialized_cache()
    with storage.connect() as c1:
        pooled = c1
    # Swap DB
    db2 = tmp_path / "second.db"
    monkeypatch.setenv("AGENTLOG_DB", str(db2))
    storage.reset_initialized_cache()
    # Old pooled connection should be closed
    with pytest.raises(sqlite3.ProgrammingError):
        pooled.execute("SELECT 1")
    # New connection points at the new DB
    with storage.connect() as c2:
        path_now = c2.execute("PRAGMA database_list").fetchone()["file"]
    assert "second.db" in path_now


# --- Auto-cost ---

def test_auto_cost_populated_on_llm_call(isolated_db):
    """When wrap_anthropic finishes an LLM span, cost_usd is populated automatically."""
    class FakeUsage:
        input_tokens = 1000
        output_tokens = 500
    class FakeBlock:
        type = "text"; text = "hi"
    class FakeResp:
        id = "x"; model = "claude-haiku-4-5-20251001"; stop_reason = "end_turn"
        role = "assistant"; content = [FakeBlock()]; usage = FakeUsage()
    class FakeMsgs:
        def create(self, **kw): return FakeResp()
    class FakeClient:
        def __init__(self): self.messages = FakeMsgs()

    wrapped = agentlog.wrap_anthropic(FakeClient())

    @agentlog.trace
    def go():
        wrapped.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10, messages=[])
    go()

    with storage.connect() as c:
        span_cost = c.execute("SELECT cost_usd FROM spans WHERE kind='llm_call'").fetchone()["cost_usd"]
        trace_cost = c.execute("SELECT cost_usd FROM traces").fetchone()["cost_usd"]
    assert span_cost == pytest.approx(0.0035)
    assert trace_cost == pytest.approx(0.0035)


# --- Set-mode clustering ---

def test_set_mode_collapses_reorderings(isolated_db):
    """A→B and B→A should be ONE cluster in set mode, TWO in ordered mode."""
    @agentlog.trace
    def path_ab():
        with agentlog.span("step_a"): pass
        with agentlog.span("step_b"): pass

    @agentlog.trace
    def path_ba():
        with agentlog.span("step_b"): pass
        with agentlog.span("step_a"): pass

    path_ab(); path_ab(); path_ba(); path_ba()

    ordered = cluster.list_clusters(mode="ordered")
    assert len(ordered) == 2, "ordered mode must split reorderings"

    set_mode = cluster.list_clusters(mode="set")
    assert len(set_mode) == 1, "set mode must collapse reorderings"
    assert set_mode[0].count == 4


def test_list_clusters_supports_offset(isolated_db):
    """Pagination via limit + offset."""
    @agentlog.trace
    def make(i):
        with agentlog.span(f"s{i}"): pass
    for i in range(5):
        make(i)
    page1 = cluster.list_clusters(limit=2, offset=0)
    page2 = cluster.list_clusters(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    # No overlap
    sig1 = {c.signature for c in page1}
    sig2 = {c.signature for c in page2}
    assert sig1.isdisjoint(sig2)


# --- @trace sample/skip ---

def test_trace_skip_bypasses_instrumentation(isolated_db):
    @agentlog.trace(skip=True)
    def hot():
        return 42
    assert hot() == 42
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert n == 0


def test_trace_sample_zero_never_records(isolated_db):
    @agentlog.trace(sample=0.0)
    def go(): return 1
    for _ in range(20):
        go()
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert n == 0


def test_trace_sample_one_always_records(isolated_db):
    @agentlog.trace(sample=1.0)
    def go(): return 1
    for _ in range(10):
        go()
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert n == 10


def test_sampling_always_records_inside_active_trace(isolated_db):
    """A sampled-out function should still be traced when called from a parent trace."""
    @agentlog.trace(sample=0.0)
    def child(): return 1

    @agentlog.trace
    def parent():
        child()

    parent()
    with storage.connect() as c:
        spans = c.execute("SELECT name FROM spans ORDER BY started_at").fetchall()
    names = [s["name"] for s in spans]
    assert any("parent" in n for n in names)
    assert any("child" in n for n in names), "child must be recorded under an active parent"


# --- Per-tag failure prefix ---

def test_failure_prefix_grouped_by_agent_tag(isolated_db):
    """When two agent topologies fail differently, per-tag prefixes should show both."""
    @agentlog.trace(tags={"agent": "rag"})
    def rag():
        with agentlog.span("retrieve"): pass
        with agentlog.span("rerank"):
            raise RuntimeError("off-topic")

    @agentlog.trace(tags={"agent": "tool_use"})
    def tool_use():
        with agentlog.span("plan"): pass
        with agentlog.span("web_search"):
            raise RuntimeError("rate limit")

    for _ in range(3):
        try: rag()
        except RuntimeError: pass
    for _ in range(2):
        try: tool_use()
        except RuntimeError: pass

    summary = cluster.failure_summary(group_by_tag="agent")
    by_tag = summary["prefixes_by_tag"]
    assert "rag" in by_tag
    assert "tool_use" in by_tag
    # global prefix is empty (rag and tool_use share no opening), per-tag prefixes are populated
    assert summary["common_failure_prefix"] == []
    assert by_tag["rag"][0] == {"name": "retrieve", "status": "ok"}
    assert by_tag["tool_use"][0] == {"name": "plan", "status": "ok"}


# --- Streaming-aware attrs ---

def test_streaming_attr_recorded_on_anthropic_wrapper(isolated_db):
    class FakeResp:
        id = "x"; model = "m"; stop_reason = "end_turn"; role = "assistant"
        content = []; usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()
    class FakeMsgs:
        def create(self, **kw): return FakeResp()
    class FakeClient:
        def __init__(self): self.messages = FakeMsgs()

    wrapped = agentlog.wrap_anthropic(FakeClient())
    wrapped.messages.create(model="claude-haiku-4-5", stream=True, messages=[])
    with storage.connect() as c:
        attrs = c.execute("SELECT attrs_json FROM spans WHERE kind='llm_call'").fetchone()["attrs_json"]
    assert '"streaming": true' in attrs


# --- flush + cleanup ---

def test_cleanup_orphans_finalizes_stale_running_traces(isolated_db):
    # Manually insert a "running" trace from the past
    old = time.time() - 3600
    with storage.connect() as c:
        c.execute("INSERT INTO traces(id, name, started_at, status) VALUES('orphan', 'old', ?, 'running')", (old,))
    n = maintenance.cleanup_orphans(stale_after_seconds=60.0)
    assert n == 1
    with storage.connect() as c:
        row = c.execute("SELECT status, error_type FROM traces WHERE id='orphan'").fetchone()
    assert row["status"] == "incomplete"
    assert row["error_type"] == "IncompleteTrace"


def test_cleanup_orphans_leaves_fresh_running_traces_alone(isolated_db):
    with storage.connect() as c:
        c.execute("INSERT INTO traces(id, name, started_at, status) VALUES('fresh', 'now', ?, 'running')", (time.time(),))
    n = maintenance.cleanup_orphans(stale_after_seconds=300.0)
    assert n == 0
    with storage.connect() as c:
        row = c.execute("SELECT status FROM traces WHERE id='fresh'").fetchone()
    assert row["status"] == "running"


def test_flush_is_idempotent(isolated_db):
    assert agentlog.flush() == 0
    assert agentlog.flush() == 0


# --- vacuum ---

def test_vacuum_dry_run_counts_without_deleting(isolated_db):
    @agentlog.trace
    def old(): pass
    old()
    # Push trace's start time into the past
    with storage.connect() as c:
        c.execute("UPDATE traces SET started_at = ?", (time.time() - 86400 * 60,))
    n, freed = maintenance.vacuum(older_than_seconds=86400 * 30, dry_run=True)
    assert n == 1
    assert freed == 0
    with storage.connect() as c:
        remaining = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert remaining == 1


def test_vacuum_deletes_old_traces(isolated_db):
    @agentlog.trace
    def go(): pass
    go(); go()
    with storage.connect() as c:
        c.execute("UPDATE traces SET started_at = ?", (time.time() - 86400 * 60,))
    n, _freed = maintenance.vacuum(older_than_seconds=86400 * 30, dry_run=False)
    assert n == 2
    with storage.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 0
        # cascade should have removed spans too
        assert c.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 0


def test_parse_duration_handles_units():
    assert maintenance.parse_duration("30s") == 30
    assert maintenance.parse_duration("5m") == 300
    assert maintenance.parse_duration("2h") == 7200
    assert maintenance.parse_duration("7d") == 604800
    assert maintenance.parse_duration("1w") == 604800
    with pytest.raises(ValueError):
        maintenance.parse_duration("bogus")


# --- Versioned export ---

def test_export_emits_header_with_versions(isolated_db):
    import io
    @agentlog.trace
    def go(): pass
    go()
    buf = io.StringIO()
    n = export.export_all(buf)
    assert n == 1
    lines = buf.getvalue().splitlines()
    import json
    header = json.loads(lines[0])
    assert header["_agentlog_header"] is True
    assert header["export_format_version"] == export.EXPORT_FORMAT_VERSION
    assert header["agentlog_version"] == agentlog.__version__
    assert header["schema_version"] == storage._SCHEMA_VERSION


def test_import_refuses_unknown_future_export_version(isolated_db):
    import io
    future = '{"_agentlog_header": true, "export_format_version": 9999}\n'
    with pytest.raises(ValueError, match="newer than this agentlog supports"):
        export.import_lines(io.StringIO(future))
