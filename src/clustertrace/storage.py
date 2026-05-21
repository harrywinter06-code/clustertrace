"""SQLite storage for clustertrace traces. Local-first — no network I/O."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Each migration is a no-op if already applied (guarded by schema_meta.version).
# To add a migration: append a new SQL string and bump _SCHEMA_VERSION.
_MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS traces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        status TEXT NOT NULL DEFAULT 'running',
        error_type TEXT,
        error_message TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_traces_started_at ON traces(started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);

    CREATE TABLE IF NOT EXISTS spans (
        id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL,
        parent_id TEXT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        status TEXT NOT NULL DEFAULT 'running',
        error_type TEXT,
        error_message TEXT,
        input_json TEXT,
        output_json TEXT,
        attrs_json TEXT,
        FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
    CREATE INDEX IF NOT EXISTS idx_spans_parent_id ON spans(parent_id);
    CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name);
    CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status);
    """,
    # v2 — tags + signature columns for clustering
    """
    CREATE TABLE IF NOT EXISTS trace_tags (
        trace_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (trace_id, key),
        FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_trace_tags_kv ON trace_tags(key, value);

    ALTER TABLE traces ADD COLUMN signature TEXT;
    CREATE INDEX IF NOT EXISTS idx_traces_signature ON traces(signature);
    """,
    # v3 — numeric metrics, cost cache, and FTS5 search index
    """
    CREATE TABLE IF NOT EXISTS trace_metrics (
        trace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        value REAL NOT NULL,
        recorded_at REAL NOT NULL,
        PRIMARY KEY (trace_id, name),
        FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_trace_metrics_name ON trace_metrics(name);

    ALTER TABLE traces ADD COLUMN cost_usd REAL;
    ALTER TABLE spans ADD COLUMN cost_usd REAL;

    CREATE VIRTUAL TABLE IF NOT EXISTS spans_fts USING fts5(
        span_id UNINDEXED,
        trace_id UNINDEXED,
        name,
        input_json,
        output_json,
        error_message,
        tokenize='unicode61'
    );

    -- Triggers keep spans_fts in sync with spans
    CREATE TRIGGER IF NOT EXISTS spans_fts_insert AFTER INSERT ON spans BEGIN
        INSERT INTO spans_fts(span_id, trace_id, name, input_json, output_json, error_message)
        VALUES (new.id, new.trace_id, new.name,
                COALESCE(new.input_json, ''), COALESCE(new.output_json, ''), COALESCE(new.error_message, ''));
    END;
    CREATE TRIGGER IF NOT EXISTS spans_fts_update AFTER UPDATE ON spans BEGIN
        UPDATE spans_fts
        SET input_json = COALESCE(new.input_json, ''),
            output_json = COALESCE(new.output_json, ''),
            error_message = COALESCE(new.error_message, '')
        WHERE span_id = new.id;
    END;
    CREATE TRIGGER IF NOT EXISTS spans_fts_delete AFTER DELETE ON spans BEGIN
        DELETE FROM spans_fts WHERE span_id = old.id;
    END;

    -- Backfill spans_fts from any rows that pre-date this migration.
    INSERT INTO spans_fts(span_id, trace_id, name, input_json, output_json, error_message)
    SELECT id, trace_id, name,
           COALESCE(input_json, ''), COALESCE(output_json, ''), COALESCE(error_message, '')
    FROM spans
    WHERE id NOT IN (SELECT span_id FROM spans_fts);
    """,
]
_SCHEMA_VERSION = len(_MIGRATIONS)

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_lock = threading.Lock()
_initialized: set[str] = set()

# Thread-local connection pool. Each thread gets one connection per resolved
# DB path. Reusing the connection avoids the PRAGMA-setup + open/close cost
# that dominated burst write latency (200 async traces dropped from ~55s to
# ~2s once we stopped opening a fresh connection per write).
_local = threading.local()


def get_db_path() -> Path:
    """Return the SQLite path. Honors $CLUSTERTRACE_DB, defaults to ~/.clustertrace/traces.db."""
    override = os.environ.get("CLUSTERTRACE_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".clustertrace" / "traces.db"


def _ensure_initialized(path: Path) -> None:
    key = str(path.resolve())
    if key in _initialized:
        return
    with _lock:
        if key in _initialized:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path), timeout=10.0) as conn:
            # Set WAL once here, while we hold the init lock. WAL is persistent
            # on the file header so subsequent connections inherit it without
            # needing to issue the (exclusive-lock-acquiring) PRAGMA themselves.
            # That was the root cause of the multi-threaded "database is locked"
            # race: each worker thread tried to set WAL on its own connection
            # while another thread's connection was already holding a shared lock.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_BOOTSTRAP)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            applied = int(row[0]) if row else 0
            if applied > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema is version {applied}, but this clustertrace "
                    f"only knows up to version {_SCHEMA_VERSION}. Upgrade clustertrace "
                    f"or point CLUSTERTRACE_DB at a different file."
                )
            for i in range(applied, _SCHEMA_VERSION):
                conn.executescript(_MIGRATIONS[i])
            if applied < _SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            conn.commit()
        _initialized.add(key)


def _get_or_open_connection(path: Path) -> sqlite3.Connection:
    """Return a thread-local SQLite connection, opening it on first use.

    Does NOT re-set PRAGMA journal_mode=WAL here — that's set once during
    `_ensure_initialized`, and WAL is persistent on the file header. Setting
    it per-connection caused a multi-thread race (PRAGMA needs an exclusive
    lock, but other connections in the pool are already holding shared locks).
    """
    pool = getattr(_local, "connections", None)
    if pool is None:
        pool = {}
        _local.connections = pool
    key = str(path.resolve())
    conn = pool.get(key)
    if conn is None:
        conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")  # per-connection setting, no lock needed
        conn.row_factory = sqlite3.Row
        pool[key] = conn
    return conn


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a pooled SQLite connection.

    The connection is shared per (thread, db-path) and not closed on exit —
    closing per-write was the dominant cost under burst load. Tests that
    swap `$CLUSTERTRACE_DB` call `reset_initialized_cache()`, which also closes
    any pooled connections so the next `connect()` opens against the new path.
    """
    path = db_path or get_db_path()
    _ensure_initialized(path)
    yield _get_or_open_connection(path)


def reset_initialized_cache() -> None:
    """Test helper: clear init cache + close pooled connections.

    Must be called when switching `$CLUSTERTRACE_DB` between tests, otherwise
    the pooled connection points at the old path and writes go nowhere.
    """
    with _lock:
        _initialized.clear()
    pool = getattr(_local, "connections", None)
    if pool:
        for conn in pool.values():
            try:
                conn.close()
            except Exception:
                pass
        pool.clear()


def _max_payload_bytes() -> int:
    """Per-field cap on serialized JSON. Override via $CLUSTERTRACE_MAX_PAYLOAD_BYTES."""
    raw = os.environ.get("CLUSTERTRACE_MAX_PAYLOAD_BYTES")
    if not raw:
        return 32_768  # 32 KB default — enough for normal LLM I/O, caps the pathological cases
    try:
        return max(1024, int(raw))
    except ValueError:
        return 32_768


def _sanitize_nonfinite(o: Any) -> Any:
    """Walk a structure replacing NaN/Inf floats with None.

    Python's json.dumps emits these as non-standard literals (NaN, Infinity)
    that JSON.parse + RFC-strict encoders (e.g. FastAPI's default) reject.
    Stored data should be valid JSON or it crashes the dashboard on readback.
    """
    if isinstance(o, float):
        if o != o or o == float("inf") or o == float("-inf"):
            return None
        return o
    if isinstance(o, dict):
        return {k: _sanitize_nonfinite(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_sanitize_nonfinite(x) for x in o]
    return o


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    # First attempt: strict JSON (rejects NaN/Inf). If the payload contains
    # non-finite floats we walk it and replace them with null, then re-serialize.
    try:
        s = json.dumps(obj, default=_fallback_serializer, allow_nan=False)
    except ValueError:
        try:
            s = json.dumps(_sanitize_nonfinite(obj), default=_fallback_serializer, allow_nan=False)
        except (TypeError, ValueError):
            s = json.dumps({"__repr__": repr(obj)[:1000]})
    except TypeError:
        s = json.dumps({"__repr__": repr(obj)[:1000]})
    cap = _max_payload_bytes()
    if len(s) > cap:
        # Truncate the JSON text and wrap it in a marker — keeps the field readable
        # without trying to parse a partial JSON value back.
        return json.dumps(
            {
                "__truncated": True,
                "original_bytes": len(s),
                "preview": s[: cap - 200],
            }
        )
    return s


def _fallback_serializer(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        try:
            return o.model_dump()
        except Exception:
            pass
    if hasattr(o, "dict") and callable(o.dict):
        try:
            return o.dict()
        except Exception:
            pass
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items() if not k.startswith("_")}
    return repr(o)[:1000]


def insert_trace(trace_id: str, name: str, started_at: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO traces(id, name, started_at, status) VALUES(?, ?, ?, 'running')",
            (trace_id, name, started_at),
        )


def finish_trace(
    trace_id: str,
    ended_at: float,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE traces SET ended_at = ?, status = ?, error_type = ?, error_message = ? WHERE id = ?",
            (ended_at, status, error_type, error_message, trace_id),
        )
    # Compute the structural signature for clustering. Lazy import to avoid
    # a static cycle between storage <-> cluster. Best-effort; never raise.
    try:
        from clustertrace import cluster

        cluster.compute_and_store_signature(trace_id)
    except Exception:
        pass
    # Roll up child-span costs into the trace.
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM spans WHERE trace_id = ? AND cost_usd IS NOT NULL",
                (trace_id,),
            ).fetchone()
        if row and row[0] > 0:
            set_trace_cost(trace_id, round(float(row[0]), 6))
    except Exception:
        pass


def set_trace_signature(trace_id: str, signature: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE traces SET signature = ? WHERE id = ?", (signature, trace_id))


def add_trace_tag(trace_id: str, key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO trace_tags(trace_id, key, value) VALUES(?, ?, ?)",
            (trace_id, key, value),
        )


def get_trace_tags(trace_id: str) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM trace_tags WHERE trace_id = ?", (trace_id,)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_metric(trace_id: str, name: str, value: float, recorded_at: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO trace_metrics(trace_id, name, value, recorded_at) VALUES(?, ?, ?, ?)",
            (trace_id, name, value, recorded_at),
        )


def get_trace_metrics(trace_id: str) -> dict[str, float]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, value FROM trace_metrics WHERE trace_id = ?", (trace_id,)
        ).fetchall()
    return {r["name"]: r["value"] for r in rows}


def set_span_cost(span_id: str, cost_usd: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE spans SET cost_usd = ? WHERE id = ?", (cost_usd, span_id))


def set_trace_cost(trace_id: str, cost_usd: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE traces SET cost_usd = ? WHERE id = ?", (cost_usd, trace_id))


def insert_span(
    span_id: str,
    trace_id: str,
    parent_id: str | None,
    name: str,
    kind: str,
    started_at: float,
    input_data: Any = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO spans(id, trace_id, parent_id, name, kind, started_at, status, input_json, attrs_json)
               VALUES(?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (
                span_id,
                trace_id,
                parent_id,
                name,
                kind,
                started_at,
                _dumps(input_data),
                _dumps(attrs),
            ),
        )


def finish_span(
    span_id: str,
    ended_at: float,
    status: str,
    output_data: Any = None,
    error_type: str | None = None,
    error_message: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    # Auto-cost: if this looks like an LLM call (has model + tokens in attrs)
    # estimate its USD cost and persist alongside the rest of the finalization.
    auto_cost: float | None = None
    if attrs:
        try:
            from clustertrace import cost as _cost

            auto_cost = _cost.estimate_span_cost(attrs)
        except Exception:
            auto_cost = None
    with connect() as conn:
        if attrs is not None:
            conn.execute(
                """UPDATE spans
                   SET ended_at = ?, status = ?, output_json = ?, error_type = ?,
                       error_message = ?, attrs_json = ?
                   WHERE id = ?""",
                (
                    ended_at,
                    status,
                    _dumps(output_data),
                    error_type,
                    error_message,
                    _dumps(attrs),
                    span_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE spans
                   SET ended_at = ?, status = ?, output_json = ?, error_type = ?, error_message = ?
                   WHERE id = ?""",
                (
                    ended_at,
                    status,
                    _dumps(output_data),
                    error_type,
                    error_message,
                    span_id,
                ),
            )
    if auto_cost is not None:
        try:
            set_span_cost(span_id, auto_cost)
        except Exception:
            pass
