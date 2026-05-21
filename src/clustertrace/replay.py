"""Re-run a stored trace by re-importing its entrypoint and re-calling it.

Limitations (intentional — kept simple):
  - The entrypoint module must be importable in the current Python env.
  - The original trace must have captured input args under the standard
    `{"args": [...], "kwargs": {...}}` shape (which @clustertrace.trace records).
  - Non-JSON-serializable args (file handles, sockets) don't round-trip.

The replay produces a new trace tagged `replay_of=<original_id>`, which the
dashboard surfaces with a "replay of" link from both sides.
"""
from __future__ import annotations

import importlib
import json
from typing import Any

import clustertrace
from clustertrace import storage


def _resolve_entry(entry: str) -> Any:
    """entry = 'module.path:object_name'."""
    if ":" not in entry:
        raise ValueError(f"entry must be 'module:function' (got {entry!r})")
    mod_path, attr = entry.split(":", 1)
    mod = importlib.import_module(mod_path)
    obj = mod
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def _load_root_input(trace_id: str) -> tuple[list, dict]:
    with storage.connect() as conn:
        row = conn.execute(
            """SELECT input_json FROM spans
               WHERE trace_id = ? AND parent_id IS NULL
               ORDER BY started_at LIMIT 1""",
            (trace_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"no root span found for trace {trace_id}")
    raw = row["input_json"]
    if not raw:
        return [], {}
    payload = json.loads(raw)
    if isinstance(payload, dict) and payload.get("__truncated"):
        raise ValueError("root input was truncated — cannot reliably replay")
    args = payload.get("args", []) if isinstance(payload, dict) else []
    kwargs = payload.get("kwargs", {}) if isinstance(payload, dict) else {}
    return args, kwargs


def replay(trace_id: str, entry: str) -> str:
    """Re-run the trace's entrypoint with its original arguments.

    Returns the new trace_id. The new trace is tagged `replay_of=<original>`.
    """
    fn = _resolve_entry(entry)
    args, kwargs = _load_root_input(trace_id)

    @clustertrace.trace(tags={"replay_of": trace_id, "replay_entry": entry})
    def _wrapper(_args, _kwargs):
        return fn(*_args, **_kwargs)

    # Capture the trace id of the wrapper invocation by reading the most recent
    # trace right after it returns. Simpler than threading the id out.
    try:
        _wrapper(args, kwargs)
    except Exception:
        pass

    with storage.connect() as conn:
        new_id = conn.execute(
            """SELECT id FROM traces WHERE id IN
               (SELECT trace_id FROM trace_tags WHERE key='replay_of' AND value=?)
               ORDER BY started_at DESC LIMIT 1""",
            (trace_id,),
        ).fetchone()
    return new_id["id"] if new_id else ""
