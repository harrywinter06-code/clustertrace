"""Red-team regression tests — bugs caught by rigorous probing in v0.5.1.

Each test starts with a one-liner describing the bug, so a future maintainer
sees what each test is preventing.
"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import clustertrace
from clustertrace import cluster, storage
from clustertrace.dashboard.app import app

# --- BUG 1: NaN / Infinity in a traced function's I/O crashed /api/trace/<id> ---
# Python's default json.dumps emits NaN / Infinity / -Infinity, which are not
# valid RFC-8259 JSON. The dashboard's response encoder (allow_nan=False)
# refused to serialize them, crashing the endpoint with a 500.

def test_nan_in_trace_io_does_not_crash_dashboard():
    @clustertrace.trace
    def returns_nan(): return float("nan")
    returns_nan()
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces").fetchone()["id"]
    client = TestClient(app)
    r = client.get(f"/api/trace/{tid}")
    assert r.status_code == 200, "NaN must not crash trace detail"
    body = r.json()  # asserts the response is valid JSON
    assert body["trace"]["id"] == tid


def test_infinity_in_trace_io_does_not_crash_dashboard():
    @clustertrace.trace
    def returns_inf(): return float("inf")
    returns_inf()
    client = TestClient(app)
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces ORDER BY started_at DESC LIMIT 1").fetchone()["id"]
    r = client.get(f"/api/trace/{tid}")
    assert r.status_code == 200
    r.json()  # must parse


def test_negative_infinity_too():
    @clustertrace.trace
    def returns_neg_inf(): return float("-inf")
    returns_neg_inf()
    client = TestClient(app)
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces ORDER BY started_at DESC LIMIT 1").fetchone()["id"]
    assert client.get(f"/api/trace/{tid}").status_code == 200


def test_nested_nan_in_dict_also_handled():
    @clustertrace.trace
    def deep(): return {"a": [1, float("nan"), {"b": float("inf")}]}
    deep()
    client = TestClient(app)
    with storage.connect() as c:
        tid = c.execute("SELECT id FROM traces ORDER BY started_at DESC LIMIT 1").fetchone()["id"]
    assert client.get(f"/api/trace/{tid}").status_code == 200


# --- BUG 2: Multi-threaded storage.connect() raced on PRAGMA WAL ---
# Each new thread's connection ran `PRAGMA journal_mode=WAL`, which needs an
# exclusive file lock. With the connection pool keeping other threads'
# connections open, the PRAGMA failed with "database is locked".

def test_concurrent_threads_can_open_connections():
    """8 threads all calling storage.connect() must succeed."""
    import concurrent.futures
    @clustertrace.trace
    def warm(): pass
    warm()  # init the schema first

    errors: list[Exception] = []
    def worker(_):
        try:
            with storage.connect() as conn:
                conn.execute("SELECT 1")
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(32)))
    assert not errors, f"got concurrent-open errors: {errors[:3]}"


def test_concurrent_thread_writes_dont_lose_traces():
    """10 threads writing 20 traces each must produce 200 rows + zero orphans."""
    @clustertrace.trace
    def t(i): return i
    t(0)  # warm up

    def make(start):
        for j in range(20):
            t(start * 100 + j)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(10)]
    for th in threads: th.start()
    for th in threads: th.join()

    with storage.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM spans WHERE parent_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM spans p WHERE p.id = spans.parent_id)"
        ).fetchone()[0]
    # 200 thread-written + 1 warm-up
    assert n == 201
    assert orphans == 0


# --- BUG 3: '|' or ':' in span names corrupted cluster signature decoding ---
# Signatures are stored as `name:status|name:status|...`. If a span name
# contained `|`, sig.split('|') produced too many parts. If it contained `:`,
# part.split(':', 1) folded the rest of the name into the status. Either way,
# list_clusters returned malformed patterns that broke the UI and tests.

def test_pipe_in_span_name_preserves_pattern_round_trip():
    @clustertrace.trace
    def go():
        with clustertrace.span("step|with|pipes"):
            pass
    go()
    clusters = cluster.list_clusters()
    found = False
    for cl in clusters:
        for name, status in cl.pattern:
            if "|" in name:
                found = True
                assert status == "ok"
    assert found, "pipe-named span did not survive the round-trip"


def test_colon_in_span_name_preserves_status_field():
    @clustertrace.trace
    def go():
        with clustertrace.span("namespace:operation"):
            pass
    go()
    clusters = cluster.list_clusters()
    found = False
    for cl in clusters:
        for name, status in cl.pattern:
            if ":" in name:
                found = True
                assert status == "ok", f"colon polluted status: got {status!r}"
                assert name == "namespace:operation"
    assert found


def test_percent_in_span_name_does_not_break_round_trip():
    """The escape uses %25 for '%'. Names already containing % must survive."""
    @clustertrace.trace
    def go():
        with clustertrace.span("100%-name"):
            pass
    go()
    clusters = cluster.list_clusters()
    for cl in clusters:
        for name, status in cl.pattern:
            if "%" in name:
                assert name == "100%-name"
                assert status == "ok"
                return
    pytest.fail("percent-name span lost during signature round-trip")


# --- BUG 4: Future schema version silently accepted, writes crashed later ---
# A DB created by a newer clustertrace would be opened by an older clustertrace
# without warning, but the new tables/columns the newer version added would be
# missing, so subsequent writes crashed with cryptic SQLite errors. The user
# would have no idea their DB came from a newer version.

def test_future_schema_version_raises_clear_error(tmp_path, monkeypatch):
    db = tmp_path / "future.db"
    raw = sqlite3.connect(str(db))
    raw.executescript(
        "CREATE TABLE schema_meta(key TEXT, value TEXT); "
        "INSERT INTO schema_meta VALUES('version','99')"
    )
    raw.close()
    monkeypatch.setenv("CLUSTERTRACE_DB", str(db))
    storage.reset_initialized_cache()
    with pytest.raises(RuntimeError, match="version 99"):
        with storage.connect() as c:
            c.execute("SELECT 1")
