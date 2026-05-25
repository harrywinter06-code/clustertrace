"""Coverage for the /review page's API endpoints.

Each test seeds a small SQLite DB via the public storage helpers, then hits
the corresponding /api/review/* endpoint and asserts the shape + key numbers.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from clustertrace import storage
from clustertrace.dashboard.app import app


def _make_trace(
    trace_id: str,
    *,
    name: str = "session",
    cost_usd: float | None = 0.10,
    started_at: float | None = None,
    signature: str | None = "func->llm_call",
    status: str = "ok",
) -> None:
    """Helper that writes a complete trace row directly via storage."""
    started = started_at if started_at is not None else time.time()
    storage.insert_trace(trace_id, name, started)
    if signature is not None:
        storage.set_trace_signature(trace_id, signature)
    storage.finish_trace(trace_id, started + 1.0, status)
    if cost_usd is not None:
        storage.set_trace_cost(trace_id, cost_usd)


def _make_llm_span(
    trace_id: str,
    span_id: str,
    *,
    input_tokens: int = 100,
    cache_read_tokens: int = 0,
    stop_reason: str | None = None,
    model: str = "claude-haiku-4-5",
) -> None:
    """LLM-call span with the attrs the review API queries."""
    started = time.time()
    attrs: dict[str, object] = {
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "model": model,
    }
    if stop_reason is not None:
        attrs["stop_reason"] = stop_reason
    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,
        parent_id=None,
        name="anthropic.messages.create",
        kind="llm_call",
        started_at=started,
        input_data=None,
        attrs=attrs,
    )
    storage.finish_span(span_id, started + 0.5, "ok", output_data=None, attrs=attrs)


def test_review_summary_counts_sessions_and_cost():
    client = TestClient(app)
    _make_trace("t-rev-1", cost_usd=0.25)
    _make_trace("t-rev-2", cost_usd=0.15)
    r = client.get("/api/review/summary?window=all")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == "all"
    assert body["sessions"] >= 2
    assert body["total_cost_usd"] >= 0.40


def test_review_expensive_orders_by_cost_desc():
    client = TestClient(app)
    _make_trace("t-exp-cheap", cost_usd=0.01)
    _make_trace("t-exp-mid", cost_usd=0.30)
    _make_trace("t-exp-pricey", cost_usd=1.20)
    r = client.get("/api/review/expensive?window=all&limit=3")
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()["traces"]]
    # The most expensive must lead; the cheap one must not lead.
    assert ids[0] == "t-exp-pricey"
    assert "t-exp-cheap" != ids[0]


def test_review_cache_rate_excludes_zero_token_patterns():
    client = TestClient(app)
    _make_trace("t-cache-1", signature="cache-sig-A")
    _make_llm_span("t-cache-1", "s-cache-1", input_tokens=100, cache_read_tokens=300)
    _make_trace("t-cache-2", signature="cache-sig-A")
    _make_llm_span("t-cache-2", "s-cache-2", input_tokens=100, cache_read_tokens=300)
    # A pattern with zero token data — should be filtered out by the HAVING clause.
    _make_trace("t-cache-empty", signature="cache-sig-empty")

    r = client.get("/api/review/cache-rate?window=all&limit=10")
    assert r.status_code == 200
    sigs = {p["signature"]: p for p in r.json()["patterns"]}
    assert "cache-sig-A" in sigs
    assert "cache-sig-empty" not in sigs
    # 600 cached / (600 cached + 200 new) = 0.75
    assert abs(sigs["cache-sig-A"]["cache_hit_rate"] - 0.75) < 1e-9


def test_review_top_pattern_picks_most_frequent():
    client = TestClient(app)
    for i in range(3):
        _make_trace(f"t-top-A-{i}", signature="top-sig-A")
    for i in range(7):
        _make_trace(f"t-top-B-{i}", signature="top-sig-B")
    r = client.get("/api/review/top-pattern?window=all")
    assert r.status_code == 200
    p = r.json()["pattern"]
    assert p is not None
    assert p["signature"] == "top-sig-B"
    assert p["run_count"] >= 7


def test_review_dead_ends_finds_max_tokens_and_errors():
    client = TestClient(app)
    # one max_tokens
    _make_trace("t-de-max", status="ok")
    _make_llm_span("t-de-max", "s-de-max", stop_reason="max_tokens")
    # one refusal
    _make_trace("t-de-ref", status="ok")
    _make_llm_span("t-de-ref", "s-de-ref", stop_reason="refusal")
    # one error trace (no LLM span needed)
    _make_trace("t-de-err", status="error")
    # one healthy
    _make_trace("t-de-ok", status="ok")
    _make_llm_span("t-de-ok", "s-de-ok", stop_reason="end_turn")

    r = client.get("/api/review/dead-ends?window=all&limit=20")
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()["traces"]}
    assert "t-de-max" in ids
    assert "t-de-ref" in ids
    assert "t-de-err" in ids
    assert "t-de-ok" not in ids


def test_review_commitments_post_then_list_then_outcome():
    client = TestClient(app)
    # POST a new commitment
    r = client.post(
        "/api/review/commitments",
        json={"text": "Use plan-mode for >=3 file edits", "window": "7d"},
    )
    assert r.status_code == 200
    new_id = r.json()["id"]

    # GET list — should include it
    r = client.get("/api/review/commitments")
    items = r.json()["commitments"]
    matching = [c for c in items if c["id"] == new_id]
    assert matching, "newly-posted commitment did not appear in list"
    assert matching[0]["text"] == "Use plan-mode for >=3 file edits"
    assert matching[0]["outcome"] is None

    # POST outcome — should accept
    r = client.post(
        f"/api/review/commitments/{new_id}/outcome",
        json={"outcome": "kept"},
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "kept"

    # GET list again — outcome should be persisted
    r = client.get("/api/review/commitments")
    matching = [c for c in r.json()["commitments"] if c["id"] == new_id]
    assert matching[0]["outcome"] == "kept"


def test_review_commitment_rejects_empty_text():
    client = TestClient(app)
    r = client.post("/api/review/commitments", json={"text": "  ", "window": "7d"})
    assert r.status_code == 400


def test_review_commitment_outcome_rejects_unknown_value():
    client = TestClient(app)
    r = client.post("/api/review/commitments", json={"text": "do a thing"})
    cid = r.json()["id"]
    r = client.post(
        f"/api/review/commitments/{cid}/outcome", json={"outcome": "kinda?"}
    )
    assert r.status_code == 400
