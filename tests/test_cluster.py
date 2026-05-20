"""Clustering, signature computation, failure-prefix mining."""
import pytest

import agentlog
from agentlog import cluster, storage


def test_signature_collapses_consecutive_duplicates():
    spans = [
        {"name": "a", "status": "ok", "parent_id": "root", "started_at": 1},
        {"name": "b", "status": "ok", "parent_id": "root", "started_at": 2},
        {"name": "b", "status": "ok", "parent_id": "root", "started_at": 3},
        {"name": "b", "status": "ok", "parent_id": "root", "started_at": 4},
        {"name": "c", "status": "error", "parent_id": "root", "started_at": 5},
    ]
    sig = cluster.signature_for_spans(spans)
    assert sig == "a:ok|b:ok|c:error"


def test_signature_excludes_root_span():
    spans = [
        {"name": "root_fn", "status": "ok", "parent_id": None, "started_at": 0},
        {"name": "child", "status": "ok", "parent_id": "x", "started_at": 1},
    ]
    assert cluster.signature_for_spans(spans) == "child:ok"


def test_signature_normalizes_llm_call_names():
    spans = [
        {
            "name": "anthropic.messages.create:claude-haiku-4-5-20251001",
            "status": "ok",
            "parent_id": "x",
            "started_at": 1,
        },
        {
            "name": "anthropic.messages.create:claude-haiku-4-5-20251001",
            "status": "ok",
            "parent_id": "x",
            "started_at": 2,  # collapsed
        },
        {
            "name": "openai.chat.completions.create:gpt-4o",
            "status": "ok",
            "parent_id": "x",
            "started_at": 3,
        },
    ]
    sig = cluster.signature_for_spans(spans)
    assert sig == "anthropic.messages.create:ok|openai.chat.completions.create:ok"


def test_clusters_grouped_by_identical_signature():
    @agentlog.trace
    def path_a():
        with agentlog.span("step1"): pass
        with agentlog.span("step2"): pass

    @agentlog.trace
    def path_b():
        with agentlog.span("step1"): pass
        agentlog.tool_call("other", args={}, result={})

    for _ in range(3): path_a()
    for _ in range(2): path_b()

    clusters = cluster.list_clusters()
    counts = sorted(c.count for c in clusters)
    assert counts == [2, 3]  # exactly 2 distinct execution patterns


def test_failure_prefix_finds_common_path():
    """When all failed traces share the same opening sequence, find it."""
    sigs = [
        "a:ok|b:ok|c:error",
        "a:ok|b:ok|d:ok|c:error",
        "a:ok|b:ok|x:ok|y:error",
    ]
    prefix = cluster.failure_prefix(sigs)
    assert prefix == [("a", "ok"), ("b", "ok")]


def test_failure_prefix_empty_when_disjoint():
    assert cluster.failure_prefix(["a:error", "b:error"]) == []


def test_backfill_fills_missing_signatures():
    """Old traces with NULL signatures get backfilled."""
    @agentlog.trace
    def go():
        with agentlog.span("s"): pass
    go()
    with storage.connect() as c:
        c.execute("UPDATE traces SET signature = NULL")
    n = cluster.backfill_signatures()
    assert n == 1
    with storage.connect() as c:
        v = c.execute("SELECT signature FROM traces").fetchone()["signature"]
    assert v == "s:ok"


def test_failure_summary_aggregates_correctly():
    """End-to-end: seed mixed traces and check the summary numbers."""
    @agentlog.trace
    def ok_path():
        with agentlog.span("a"): pass

    @agentlog.trace
    def err_path():
        with agentlog.span("a"): pass
        raise RuntimeError("boom")

    for _ in range(4): ok_path()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            err_path()

    summary = cluster.failure_summary()
    assert summary["traces_total"] == 6
    assert summary["traces_failed"] == 2
    assert summary["overall_failure_rate"] == pytest.approx(2 / 6)
    # both failed traces share the opening "a:ok" step
    assert summary["common_failure_prefix"][0] == {"name": "a", "status": "ok"}
