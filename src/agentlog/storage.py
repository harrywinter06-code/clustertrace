"""SQLite storage for agentlog traces. Local-first — no network I/O."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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
"""


_lock = threading.Lock()
_initialized: set[str] = set()


def get_db_path() -> Path:
    """Return the SQLite path. Honors $AGENTLOG_DB, defaults to ~/.agentlog/traces.db."""
    override = os.environ.get("AGENTLOG_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agentlog" / "traces.db"


def _ensure_initialized(path: Path) -> None:
    key = str(path.resolve())
    if key in _initialized:
        return
    with _lock:
        if key in _initialized:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            conn.commit()
        _initialized.add(key)


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection. Initializes schema on first call."""
    path = db_path or get_db_path()
    _ensure_initialized(path)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def reset_initialized_cache() -> None:
    """Test helper: clear the per-path initialization cache."""
    with _lock:
        _initialized.clear()


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return json.dumps(obj, default=_fallback_serializer)
    except (TypeError, ValueError):
        return json.dumps({"__repr__": repr(obj)[:1000]})


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
