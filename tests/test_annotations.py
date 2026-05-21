"""Cluster annotations: persistence, validation, vacuum-survival, and the
failure-summary side effect that drops `expected-failure` clusters from the
headline count.
"""
from __future__ import annotations

import time

import pytest

import clustertrace
from clustertrace import annotations, cluster, storage
from clustertrace.dashboard.app import app


def _seed_trace(
    name: str = "agent",
    status: str = "ok",
    *,
    error_type: str | None = None,
    started: float | None = None,
) -> str:
    """Insert a finished trace + one child span so it gets a real signature."""
    started_at = started if started is not None else time.time()
    tid = f"t-{name}-{int(started_at * 1_000_000)}-{status}"
    storage.insert_trace(tid, name, started_at)
    storage.insert_span(
        span_id=f"{tid}:root",
        trace_id=tid,
        parent_id=None,
        name=name,
        kind="function",
        started_at=started_at,
    )
    storage.finish_span(f"{tid}:root", started_at + 0.001, "ok" if status != "error" else "error")
    storage.insert_span(
        span_id=f"{tid}:step",
        trace_id=tid,
        parent_id=f"{tid}:root",
        name="step",
        kind="function",
        started_at=started_at + 0.0001,
    )
    storage.finish_span(f"{tid}:step", started_at + 0.0005, status)
    storage.finish_trace(
        tid, started_at + 0.001, status, error_type=error_type,
        error_message=("boom" if status == "error" else None),
    )
    cluster.compute_and_store_signature(tid)
    return tid


def _get_sig_hash(name: str = "agent", status: str = "ok") -> str:
    """Seed one trace of the given shape and return its cluster sig_hash."""
    tid = _seed_trace(name=name, status=status)
    sig = storage.get_trace_metrics  # noqa — unused, only to confirm import wired
    with storage.connect() as conn:
        row = conn.execute("SELECT signature FROM traces WHERE id = ?", (tid,)).fetchone()
    return cluster.signature_hash(row["signature"])


def test_annotate_round_trip() -> None:
    sig = _get_sig_hash()
    out = clustertrace.annotate_cluster(sig, status="wontfix", note="known issue")
    assert out["status"] == "wontfix"
    assert out["note"] == "known issue"
    fresh = clustertrace.get_cluster_annotation(sig)
    assert fresh is not None
    assert fresh["status"] == "wontfix"


def test_annotate_rejects_unknown_status() -> None:
    sig = _get_sig_hash()
    with pytest.raises(ValueError):
        clustertrace.annotate_cluster(sig, status="bogus-status")


def test_annotate_rejects_oversize_note() -> None:
    sig = _get_sig_hash()
    with pytest.raises(ValueError):
        clustertrace.annotate_cluster(sig, note="x" * (annotations.NOTE_MAX_LEN + 1))


def test_annotate_with_no_fields_raises() -> None:
    sig = _get_sig_hash()
    with pytest.raises(ValueError):
        clustertrace.annotate_cluster(sig)


def test_tag_is_appended_not_replaced() -> None:
    sig = _get_sig_hash()
    clustertrace.annotate_cluster(sig, tag="auth")
    clustertrace.annotate_cluster(sig, tag="known")
    # Re-adding the same tag is idempotent
    clustertrace.annotate_cluster(sig, tag="auth")
    fresh = clustertrace.get_cluster_annotation(sig)
    assert sorted(fresh["tags"]) == ["auth", "known"]


def test_clear_annotation_removes_row() -> None:
    sig = _get_sig_hash()
    clustertrace.annotate_cluster(sig, status="acceptable")
    assert clustertrace.clear_cluster_annotation(sig) is True
    assert clustertrace.get_cluster_annotation(sig) is None
    # Second clear is a no-op.
    assert clustertrace.clear_cluster_annotation(sig) is False


def test_status_update_overwrites_previous() -> None:
    sig = _get_sig_hash()
    clustertrace.annotate_cluster(sig, status="wontfix")
    clustertrace.annotate_cluster(sig, status="priority")
    assert clustertrace.get_cluster_annotation(sig)["status"] == "priority"


def test_note_partial_update_preserves_status() -> None:
    sig = _get_sig_hash()
    clustertrace.annotate_cluster(sig, status="wontfix", note="first")
    clustertrace.annotate_cluster(sig, note="second")
    fresh = clustertrace.get_cluster_annotation(sig)
    assert fresh["status"] == "wontfix"
    assert fresh["note"] == "second"


def test_annotations_survive_vacuum() -> None:
    """The brief: annotations must persist when old traces are vacuumed off."""
    from clustertrace import maintenance

    # Seed an old trace and annotate its cluster.
    sig = _get_sig_hash()
    clustertrace.annotate_cluster(sig, status="expected-failure", note="known")

    # Push all traces' started_at into the distant past.
    with storage.connect() as conn:
        conn.execute("UPDATE traces SET started_at = 1.0, ended_at = 2.0")
    maintenance.vacuum(older_than_seconds=0, dry_run=False)

    # The annotation must still be there.
    assert clustertrace.get_cluster_annotation(sig) is not None


def test_expected_failure_excluded_from_dashboard_summary() -> None:
    """Mark a cluster as expected-failure; the headline counts must drop it."""
    from fastapi.testclient import TestClient

    # Two failing traces, same signature.
    _seed_trace(name="x", status="error")
    _seed_trace(name="x", status="error", started=time.time() + 0.01)
    # One healthy trace, distinct cluster.
    _seed_trace(name="y", status="ok")

    with storage.connect() as conn:
        sig_row = conn.execute(
            "SELECT signature FROM traces WHERE name='x' LIMIT 1"
        ).fetchone()
    failing_hash = cluster.signature_hash(sig_row["signature"])

    client = TestClient(app)
    before = client.get("/api/failure-summary").json()
    assert before["traces_failed"] == 2

    clustertrace.annotate_cluster(failing_hash, status="expected-failure")
    after = client.get("/api/failure-summary").json()
    assert after["traces_failed_after_annotations"] == 0
    assert after["expected_failure_traces"] == 2


def test_dashboard_post_cluster_annotations_endpoint() -> None:
    """POST /api/cluster-annotations writes through to the same store."""
    from fastapi.testclient import TestClient

    sig = _get_sig_hash()
    client = TestClient(app)
    r = client.post("/api/cluster-annotations", json={
        "sig_hash": sig,
        "status": "priority",
        "note": "fix me",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "priority"
    # Round-trip via the Python API
    assert clustertrace.get_cluster_annotation(sig)["status"] == "priority"


def test_dashboard_post_rejects_unknown_status() -> None:
    from fastapi.testclient import TestClient

    sig = _get_sig_hash()
    client = TestClient(app)
    r = client.post("/api/cluster-annotations", json={
        "sig_hash": sig,
        "status": "bogus",
    })
    assert r.status_code == 400
