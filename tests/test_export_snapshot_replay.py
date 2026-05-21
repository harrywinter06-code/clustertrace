"""JSON export/import round-trip, HTML snapshot, replay."""
import io

import clustertrace
from clustertrace import export, snapshot, storage


def test_export_import_roundtrip(tmp_path, monkeypatch):
    @clustertrace.trace(tags={"agent": "research"})
    def go():
        clustertrace.metric("score", 0.9)
        clustertrace.tool_call("lookup", args={"q": "x"}, result={"y": 1})
    go()
    with storage.connect() as c:
        original_id = c.execute("SELECT id FROM traces").fetchone()["id"]
        original_tags = storage.get_trace_tags(original_id)
        original_metrics = storage.get_trace_metrics(original_id)

    buf = io.StringIO()
    n = export.export_all(buf)
    assert n == 1
    lines = buf.getvalue().splitlines()

    # Now move to a fresh DB and import
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("CLUSTERTRACE_DB", str(fresh))
    storage.reset_initialized_cache()

    imported, skipped = export.import_lines(lines)
    assert imported == 1
    # Skipped count is 1 because of the header line that export_all now emits.
    assert skipped == 1

    assert storage.get_trace_tags(original_id) == original_tags
    assert storage.get_trace_metrics(original_id) == original_metrics


def test_export_import_skips_existing():
    @clustertrace.trace
    def go(): pass
    go()
    buf = io.StringIO()
    export.export_all(buf)
    lines = buf.getvalue().splitlines()
    # Re-import into the same DB → all should be skipped (including the header)
    imported, skipped = export.import_lines(lines)
    assert imported == 0
    assert skipped == len(lines)  # header is in `lines` and also counts as skipped


def test_snapshot_renders_self_contained_html():
    @clustertrace.trace(tags={"agent": "test"})
    def go():
        with clustertrace.span("inner"): pass
        return {"answer": 42}
    go()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    html = snapshot.render(tid)
    assert "<!doctype html>" in html.lower()
    assert "clustertrace snapshot" in html
    # No external dependencies (relative URLs, CDN links, etc.)
    assert "http://" not in html
    # Only allowed external link is the GitHub footer attribution
    assert html.count("https://") == 1
    # Contains the trace name + a span
    assert "inner" in html
    assert "test" in html


def test_snapshot_unknown_trace_raises():
    import pytest
    with pytest.raises(KeyError):
        snapshot.render("not-a-real-trace-id")


def test_replay_invokes_entrypoint_with_original_args():
    """Replay re-runs the entrypoint with the captured args; new trace tagged replay_of."""
    # Register a function in a real module-like namespace so importlib can find it
    import sys
    import types
    fake_mod = types.ModuleType("fake_replay_target")
    calls: list = []

    @clustertrace.trace
    def target(x, y=1):
        calls.append((x, y))
        return x + y

    fake_mod.target = target
    sys.modules["fake_replay_target"] = fake_mod

    target(5, y=10)

    with storage.connect() as c:
        original_id = c.execute("SELECT id FROM traces ORDER BY started_at DESC LIMIT 1").fetchone()["id"]

    from clustertrace import replay as rp
    new_id = rp.replay(original_id, entry="fake_replay_target:target")

    # The replay decorator wrapped the call so we should see TWO total invocations of `target`
    assert len(calls) == 2
    assert calls[0] == (5, 10)
    assert calls[1] == (5, 10)
    # The new trace exists and is tagged replay_of
    assert new_id != ""
    tags = storage.get_trace_tags(new_id)
    assert tags.get("replay_of") == original_id
    sys.modules.pop("fake_replay_target", None)
