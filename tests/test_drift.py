"""Drift detection — per-cluster failure-rate change between two windows."""
from __future__ import annotations

import importlib.resources
import time

from fastapi.testclient import TestClient

from clustertrace import cluster, drift, export, storage
from clustertrace.dashboard.app import app


def _seed_trace(
    sig: str, status: str, started_at: float, trace_id: str | None = None
) -> str:
    """Insert a single trace + a child span so the signature column gets set."""
    import uuid
    tid = trace_id or f"t-{uuid.uuid4().hex[:8]}"
    with storage.connect() as conn:
        conn.execute(
            """INSERT INTO traces(id, name, started_at, ended_at, status, signature)
               VALUES(?, 'seed', ?, ?, ?, ?)""",
            (tid, started_at, started_at + 0.01, status, sig),
        )
    return tid


def test_drift_flags_regressed_cluster():
    """A cluster with 0% errors before and 75% now appears as regressed."""
    now = time.time()
    sig = "a:ok|b:error"
    # Previous window: 12 traces, all clean
    for i in range(12):
        _seed_trace("a:ok|b:ok", "ok", now - 36 * 3600 + i)
    # Current window: 8 traces of the bad-shape cluster, 6 errors
    for i in range(6):
        _seed_trace(sig, "error", now - 12 * 3600 + i)
    for i in range(2):
        _seed_trace(sig, "ok", now - 12 * 3600 + 100 + i)
    # Same shape existed in the previous window with 0 errors
    for i in range(12):
        _seed_trace(sig, "ok", now - 36 * 3600 + 1000 + i)

    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    sigs_in_drift = {d["signature"]: d for d in out["drifts"]}
    assert sig in sigs_in_drift
    d = sigs_in_drift[sig]
    assert d["direction"] == "regressed"
    assert d["current"]["n"] == 8
    assert d["current"]["errs"] == 6
    assert d["current"]["rate"] == 0.75
    assert d["previous"]["rate"] == 0.0
    assert abs(d["delta"] - 0.75) < 1e-9


def test_drift_excludes_low_n_current_window():
    """Clusters with fewer than 3 traces in current window are filtered out (noise floor)."""
    now = time.time()
    # 2 traces only in current — should be excluded
    for i in range(2):
        _seed_trace("rare:error", "error", now - 1000 + i)
    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    assert all(d["signature"] != "rare:error" for d in out["drifts"])


def test_drift_sorts_by_abs_delta_descending():
    """Output is ranked by magnitude of the change, regardless of direction."""
    now = time.time()
    # Cluster A: small regression (+0.2)
    for i in range(10):
        _seed_trace("a:ok", "ok", now - 36 * 3600 + i)
    for i in range(10):
        _seed_trace("a:ok", "error" if i < 2 else "ok", now - 1000 + i)
    # Cluster B: large improvement (-0.8)
    for i in range(10):
        _seed_trace("b:ok", "error" if i < 9 else "ok", now - 36 * 3600 + 1000 + i)
    for i in range(10):
        _seed_trace("b:ok", "error" if i < 1 else "ok", now - 2000 + i)

    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    deltas = [d["delta"] for d in out["drifts"]]
    abs_deltas = [abs(x) for x in deltas]
    assert abs_deltas == sorted(abs_deltas, reverse=True)


def test_drift_handles_empty_compare_window():
    """A brand-new cluster (no prior traces) is treated as previous rate = 0."""
    now = time.time()
    for i in range(5):
        _seed_trace("new:error", "error", now - 100 + i)
    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    matches = [d for d in out["drifts"] if d["signature"] == "new:error"]
    assert matches
    d = matches[0]
    assert d["previous"]["n"] == 0
    assert d["previous"]["rate"] == 0.0
    assert d["current"]["rate"] == 1.0


def test_drift_zero_delta_is_filtered():
    """Clusters whose rate didn't move at all aren't included (keeps the response focused)."""
    now = time.time()
    for i in range(8):
        _seed_trace("steady:ok", "ok", now - 36 * 3600 + i)
    for i in range(8):
        _seed_trace("steady:ok", "ok", now - 1000 + i)
    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    assert all(d["signature"] != "steady:ok" for d in out["drifts"])


def test_drift_includes_window_metadata():
    """Response shape matches the brief: current_window + compare_window + drifts."""
    now = time.time()
    for i in range(5):
        _seed_trace("x:error", "error", now - 100 + i)
    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    assert "current_window" in out
    assert "compare_window" in out
    assert "drifts" in out
    assert out["current_window"]["end"] == now
    assert out["current_window"]["start"] == now - 24 * 3600
    assert out["compare_window"]["end"] == now - 24 * 3600
    assert out["compare_window"]["start"] == now - 48 * 3600


def test_drift_api_endpoint_accepts_duration_strings():
    """The HTTP endpoint accepts 24h / 7d / 30d (reuses maintenance.parse_duration)."""
    client = TestClient(app)
    r = client.get("/api/cluster-drift?window=24h&compare=24h")
    assert r.status_code == 200
    body = r.json()
    assert "drifts" in body
    r = client.get("/api/cluster-drift?window=7d&compare=7d")
    assert r.status_code == 200
    r = client.get("/api/cluster-drift?window=30d&compare=30d")
    assert r.status_code == 200


def test_drift_api_rejects_bad_duration():
    """Garbage `window=` returns 400, not 500."""
    client = TestClient(app)
    r = client.get("/api/cluster-drift?window=banana&compare=24h")
    assert r.status_code == 400


def test_drift_dashboard_page_renders():
    """The /drift page returns HTML."""
    client = TestClient(app)
    r = client.get("/drift")
    assert r.status_code == 200
    assert "<h1>Cluster drift</h1>" in r.text


def test_drift_works_on_demo_data():
    """Drift on the bundled demo data must yield at least one non-zero delta.

    The demo dataset is a flat sequence of agent runs spanning ~8 minutes.
    A 60/40 split (current window is the later 60%) catches the
    `retrieve:ok|rerank:error` cluster that exists only in the later half —
    a real regression-style finding.
    """
    data_file = importlib.resources.files("clustertrace").joinpath("data/demo-traces.jsonl")
    with data_file.open("r", encoding="utf-8") as f:
        imported, _ = export.import_lines(f)
    assert imported > 0
    cluster.backfill_signatures()

    with storage.connect() as conn:
        bounds = conn.execute(
            "SELECT MIN(started_at), MAX(started_at) FROM traces"
        ).fetchone()
    t0, t1 = bounds[0], bounds[1]
    total = t1 - t0
    out = drift.compute_drift(
        window_seconds=total * 0.6, compare_seconds=total * 0.4, now=t1
    )
    # Brief: "at least one cluster with a non-zero delta on the demo data"
    assert any(abs(d["delta"]) > 0 for d in out["drifts"]), (
        f"expected at least one drifted cluster on demo data; got {out['drifts']}"
    )


def test_drift_excludes_running_traces():
    """`status='running'` traces are not aggregated into either window."""
    now = time.time()
    for i in range(5):
        _seed_trace("real:ok", "ok", now - 100 + i)
    # Add a running one — should not affect anything
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO traces(id, name, started_at, status, signature) "
            "VALUES('running-1', 'r', ?, 'running', 'real:ok')",
            (now - 50,),
        )
    out = drift.compute_drift(window_seconds=24 * 3600, compare_seconds=24 * 3600, now=now)
    # The 'real:ok' cluster either appears or doesn't (steady = filtered),
    # but the running trace must NOT be counted. We assert that by checking
    # the current_window.trace_count == 5, not 6.
    assert out["current_window"]["trace_count"] == 5
