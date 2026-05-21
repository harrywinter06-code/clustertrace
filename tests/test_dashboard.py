"""Smoke-test the dashboard's JSON endpoints against a populated DB."""
from fastapi.testclient import TestClient

import clustertrace
from clustertrace.dashboard.app import app


def _seed():
    @clustertrace.trace
    def ok_path():
        with clustertrace.span("step_a"):
            clustertrace.tool_call("lookup", args={"q": "x"}, result={"hits": 3})
        return "done"

    @clustertrace.trace
    def failing_path():
        with clustertrace.span("step_a"):
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
    assert "clustertrace" in r.text.lower()


def test_unknown_trace_returns_404():
    client = TestClient(app)
    r = client.get("/api/trace/nope")
    assert r.status_code == 404


def test_clusters_endpoint_groups_traces():
    @clustertrace.trace
    def path_a():
        with clustertrace.span("step1"): pass
        with clustertrace.span("step2"): pass

    @clustertrace.trace
    def path_b():
        with clustertrace.span("step1"): pass

    for _ in range(2): path_a()
    for _ in range(3): path_b()

    client = TestClient(app)
    r = client.get("/api/clusters")
    assert r.status_code == 200
    clusters = r.json()["clusters"]
    counts = sorted(c["count"] for c in clusters)
    assert counts == [2, 3]


def test_failure_summary_endpoint():
    @clustertrace.trace
    def fails():
        with clustertrace.span("a"): pass
        raise ValueError("nope")

    try: fails()
    except ValueError: pass

    client = TestClient(app)
    r = client.get("/api/failure-summary")
    assert r.status_code == 200
    s = r.json()
    assert s["traces_failed"] == 1


def test_traces_filter_by_status():
    @clustertrace.trace
    def good(): pass

    @clustertrace.trace
    def bad(): raise RuntimeError("x")

    good()
    good()
    try: bad()
    except RuntimeError: pass

    client = TestClient(app)
    assert client.get("/api/traces?status=ok").json()["total"] == 2
    assert client.get("/api/traces?status=error").json()["total"] == 1


def test_traces_filter_by_tag():
    @clustertrace.trace(tags={"agent": "rag"})
    def a(): pass

    @clustertrace.trace(tags={"agent": "tool_use"})
    def b(): pass

    a(); a(); b()
    client = TestClient(app)
    r1 = client.get("/api/traces?tag=agent%3Drag").json()
    r2 = client.get("/api/traces?tag=agent%3Dtool_use").json()
    assert r1["total"] == 2
    assert r2["total"] == 1


def test_tags_endpoint_lists_known_keys():
    @clustertrace.trace(tags={"agent": "rag", "v": "1"})
    def a(): pass
    a(); a()
    client = TestClient(app)
    tags = client.get("/api/tags").json()["tags"]
    assert "agent" in tags
    assert tags["agent"][0]["value"] == "rag"
    assert tags["agent"][0]["count"] == 2
