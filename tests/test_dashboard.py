"""Smoke-test the dashboard's JSON endpoints against a populated DB."""
from fastapi.testclient import TestClient

import agentlog
from agentlog.dashboard.app import app


def _seed():
    @agentlog.trace
    def ok_path():
        with agentlog.span("step_a"):
            agentlog.tool_call("lookup", args={"q": "x"}, result={"hits": 3})
        return "done"

    @agentlog.trace
    def failing_path():
        with agentlog.span("step_a"):
            raise RuntimeError("kaboom")

    ok_path()
    ok_path()
    try:
        failing_path()
    except RuntimeError:
        pass


def test_traces_endpoint_returns_seeded_rows():
    _seed()
    client = TestClient(app)
    r = client.get("/api/traces")
    assert r.status_code == 200
    body = r.json()
    assert "traces" in body
    assert len(body["traces"]) == 3
    statuses = sorted(t["status"] for t in body["traces"])
    assert statuses == ["error", "ok", "ok"]


def test_trace_detail_endpoint():
    _seed()
    client = TestClient(app)
    r = client.get("/api/traces")
    tid = r.json()["traces"][0]["id"]
    detail = client.get(f"/api/trace/{tid}")
    assert detail.status_code == 200
    j = detail.json()
    assert j["trace"]["id"] == tid
    assert isinstance(j["spans"], list)
    assert len(j["spans"]) >= 1


def test_failure_graph_endpoint_aggregates():
    _seed()
    client = TestClient(app)
    r = client.get("/api/failure-graph")
    assert r.status_code == 200
    j = r.json()
    assert "nodes" in j and "edges" in j and "fail_steps" in j
    assert len(j["nodes"]) > 0
    # at least one node should have errors > 0 because we seeded a failing trace
    assert any(n["errors"] > 0 for n in j["nodes"])


def test_stats_endpoint():
    _seed()
    client = TestClient(app)
    r = client.get("/api/stats")
    assert r.status_code == 200
    j = r.json()
    assert j["traces_total"] == 3
    assert j["traces_error"] == 1


def test_index_renders():
    _seed()
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "agentlog" in r.text.lower()


def test_unknown_trace_returns_404():
    client = TestClient(app)
    r = client.get("/api/trace/nope")
    assert r.status_code == 404
