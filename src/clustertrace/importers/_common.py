"""Shared helpers used by every competitor importer.

Kept deliberately tiny: stream reading, ID prefixing, safe-int coercion,
trace upsert + finalization. Each importer composes these around its own
format-specific mapping logic.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import IO, Any

from clustertrace import storage


def read_stream(stream: IO[str] | Iterable[str]) -> str:
    """Read a file-like or any iterable of strings into one big string.

    Importers accept either a stream (sys.stdin, an open file) or an iterable
    of lines. JSON-vs-JSONL detection happens in the per-source parser.
    """
    if hasattr(stream, "read"):
        return stream.read()  # type: ignore[union-attr]
    return "".join(stream)


def iter_records(text: str) -> Iterator[Any]:
    """Yield JSON records from a blob that is either a single JSON value or JSONL.

    Resolution order:
      1. If the whole blob is one valid JSON value -> yield it once.
      2. Otherwise split into non-empty lines and yield each one that parses.

    Lines that don't parse are silently skipped here — counting them as
    "skipped" is the importer's responsibility (it owns the return value).
    """
    text = text.strip()
    if not text:
        return
    try:
        obj = json.loads(text)
    except ValueError:
        pass
    else:
        yield obj
        return
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except ValueError:
            continue


def prefix_id(source: str, raw_id: Any) -> str:
    """Namespace a source trace/span id so two tools' UUIDs can't collide.

    `langfuse:abc123` is distinct from `phoenix:abc123` even if both upstream
    systems happened to mint the same UUID. Returns a stable string suitable
    for SQL primary keys.
    """
    s = str(raw_id) if raw_id is not None else ""
    return f"{source}:{s}" if s else ""


def safe_int(v: Any) -> int | None:
    """Coerce to int, returning None for anything that doesn't convert cleanly."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def trace_exists(trace_id: str) -> bool:
    with storage.connect() as conn:
        row = conn.execute("SELECT 1 FROM traces WHERE id = ?", (trace_id,)).fetchone()
    return row is not None


def finalize_trace(
    trace_id: str,
    ended_at: float,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    """Wrapper around storage.finish_trace so importers don't import storage directly
    for the most common boilerplate. (They still import it for insert_trace etc.)"""
    storage.finish_trace(
        trace_id,
        ended_at,
        status,
        error_type=error_type,
        error_message=error_message,
    )
