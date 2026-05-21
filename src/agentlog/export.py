"""JSON Lines export and import for traces.

Each line is one trace + its spans + tags + metrics. Importing into a fresh
DB reproduces the original state; importing into a populated DB merges
without clobbering (skips any trace id that already exists).
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import IO

from agentlog import storage

EXPORT_FORMAT_VERSION = 1


def _trace_to_dict(trace_id: str) -> dict | None:
    with storage.connect() as conn:
        trace = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
        if trace is None:
            return None
        spans = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at",
            (trace_id,),
        ).fetchall()
        tags = conn.execute(
            "SELECT key, value FROM trace_tags WHERE trace_id = ?", (trace_id,)
        ).fetchall()
        metrics = conn.execute(
            "SELECT name, value FROM trace_metrics WHERE trace_id = ?", (trace_id,)
        ).fetchall()
    return {
        "trace": dict(trace),
        "spans": [dict(s) for s in spans],
        "tags": {r["key"]: r["value"] for r in tags},
        "metrics": {r["name"]: r["value"] for r in metrics},
    }


def export_trace(trace_id: str, out: IO[str] = sys.stdout) -> int:
    payload = _trace_to_dict(trace_id)
    if payload is None:
        return 0
    out.write(json.dumps(payload) + "\n")
    return 1


def _write_header(out: IO[str]) -> None:
    import agentlog as _al

    with storage.connect() as conn:
        sv = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    header = {
        "_agentlog_header": True,
        "export_format_version": EXPORT_FORMAT_VERSION,
        "agentlog_version": getattr(_al, "__version__", "?"),
        "schema_version": int(sv[0]) if sv else None,
    }
    out.write(json.dumps(header) + "\n")


def export_all(out: IO[str] = sys.stdout, limit: int | None = None, header: bool = True) -> int:
    if header:
        _write_header(out)
    with storage.connect() as conn:
        ids_query = "SELECT id FROM traces ORDER BY started_at DESC"
        if limit:
            ids_query += f" LIMIT {int(limit)}"
        ids = [r["id"] for r in conn.execute(ids_query).fetchall()]
    n = 0
    for tid in ids:
        n += export_trace(tid, out)
    return n


def import_lines(lines: Iterable[str]) -> tuple[int, int]:
    """Read JSONL and merge each trace into the current DB.

    A leading `_agentlog_header` line (if present) is parsed and validated.
    Returns (imported, skipped). Existing trace ids are skipped.
    Header line and unrecognized lines count as skipped.
    """
    imported = 0
    skipped = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        # Skip header lines from versioned exports
        if isinstance(obj, dict) and obj.get("_agentlog_header"):
            fmt = obj.get("export_format_version")
            if fmt is not None and fmt > EXPORT_FORMAT_VERSION:
                # Newer format we don't understand — refuse the whole stream
                raise ValueError(
                    f"export_format_version={fmt} is newer than this agentlog supports "
                    f"({EXPORT_FORMAT_VERSION}). Upgrade agentlog."
                )
            skipped += 1
            continue
        if _import_one(obj):
            imported += 1
        else:
            skipped += 1
    return imported, skipped


def _import_one(payload: dict) -> bool:
    trace = payload.get("trace") or {}
    tid = trace.get("id")
    if not tid:
        return False
    with storage.connect() as conn:
        existing = conn.execute("SELECT id FROM traces WHERE id = ?", (tid,)).fetchone()
        if existing:
            return False
        # Insert trace
        cols = [c for c in trace.keys() if c in {
            "id", "name", "started_at", "ended_at", "status",
            "error_type", "error_message", "signature", "cost_usd",
        }]
        placeholders = ",".join("?" for _ in cols)
        col_names = ",".join(cols)
        conn.execute(
            f"INSERT INTO traces({col_names}) VALUES({placeholders})",
            [trace.get(c) for c in cols],
        )
        # Spans
        for s in payload.get("spans", []):
            scols = [c for c in s.keys() if c in {
                "id", "trace_id", "parent_id", "name", "kind",
                "started_at", "ended_at", "status",
                "error_type", "error_message",
                "input_json", "output_json", "attrs_json", "cost_usd",
            }]
            placeholders = ",".join("?" for _ in scols)
            col_names = ",".join(scols)
            conn.execute(
                f"INSERT INTO spans({col_names}) VALUES({placeholders})",
                [s.get(c) for c in scols],
            )
        # Tags
        for k, v in (payload.get("tags") or {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO trace_tags(trace_id, key, value) VALUES(?, ?, ?)",
                (tid, k, str(v)),
            )
        # Metrics
        for k, v in (payload.get("metrics") or {}).items():
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO trace_metrics(trace_id, name, value, recorded_at) VALUES(?, ?, ?, ?)",
                    (tid, k, float(v), trace.get("ended_at") or trace.get("started_at") or 0.0),
                )
            except (TypeError, ValueError):
                pass
    return True
