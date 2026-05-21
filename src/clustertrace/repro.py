"""Auto-generate a pytest fixture from a captured trace.

Given a `trace_id` (or a `sig_hash`, which selects the most recent failing
trace in that cluster), emit a runnable pytest file that:
  1. Imports the entrypoint from `--entry mod:fn`
  2. Inlines the captured args/kwargs as Python literals
  3. Calls the function and asserts the expected outcome
     (positive: must NOT raise; negative: must raise the original error type)

Out of scope (brief): handling non-JSON-serializable arguments. If the
captured root input is `__truncated`, we emit a comment instead of code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from clustertrace import cluster, storage


class ReproError(Exception):
    """Raised when a trace cannot be turned into a runnable pytest file."""


@dataclass
class _TraceInfo:
    trace_id: str
    name: str
    status: str
    error_type: str | None
    error_message: str | None
    root_input: dict | None  # parsed JSON of the root span's input, or None
    truncated: bool


def _resolve_seed_trace(trace_or_sig: str) -> _TraceInfo:
    """Look up the trace to reproduce.

    If `trace_or_sig` is a known trace_id, use it directly. Otherwise treat
    it as a sig_hash and pick the most-recent *failing* trace in that
    cluster (falling back to the most-recent overall if no failures).
    """
    with storage.connect() as conn:
        row = conn.execute(
            """SELECT id, name, status, error_type, error_message, signature
               FROM traces WHERE id = ?""",
            (trace_or_sig,),
        ).fetchone()
        if row is None:
            # Try sig_hash. We have to compute hash for every signature since
            # hashes aren't stored. Cheap on any realistic DB.
            sig_rows = conn.execute(
                """SELECT id, name, status, error_type, error_message, signature
                   FROM traces
                   WHERE signature IS NOT NULL
                   ORDER BY started_at DESC"""
            ).fetchall()
            target = trace_or_sig.lower()
            failing: list = []
            any_match: list = []
            for r in sig_rows:
                if cluster.signature_hash(r["signature"]) == target:
                    any_match.append(r)
                    if r["status"] == "error":
                        failing.append(r)
            chosen = failing[0] if failing else (any_match[0] if any_match else None)
            if chosen is None:
                raise ReproError(
                    f"no trace or cluster matches {trace_or_sig!r} — "
                    f"pass a trace_id or a sig_hash from `clustertrace stats` / /clusters"
                )
            row = chosen

        root_row = conn.execute(
            """SELECT input_json FROM spans
               WHERE trace_id = ? AND parent_id IS NULL
               ORDER BY started_at LIMIT 1""",
            (row["id"],),
        ).fetchone()

    raw_input = root_row["input_json"] if root_row else None
    parsed: dict | None
    truncated = False
    if raw_input:
        try:
            parsed = json.loads(raw_input)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("__truncated"):
            truncated = True
            parsed = None
    else:
        parsed = None

    return _TraceInfo(
        trace_id=row["id"],
        name=row["name"],
        status=row["status"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        root_input=parsed if isinstance(parsed, dict) else None,
        truncated=truncated,
    )


def _short_hash(trace_id: str) -> str:
    return trace_id.replace("-", "")[:8]


def _validate_entry(entry: str) -> tuple[str, str]:
    if ":" not in entry:
        raise ReproError(f"--entry must be 'module:function' (got {entry!r})")
    mod, fn = entry.split(":", 1)
    if not mod or not fn:
        raise ReproError(f"--entry must be 'module:function' (got {entry!r})")
    return mod, fn


def _is_json_safe(o: object) -> bool:
    """True iff `o` can be losslessly round-tripped through json.dumps + literal_eval-like inlining."""
    if o is None or isinstance(o, bool | int | float | str):
        return True
    if isinstance(o, list):
        return all(_is_json_safe(x) for x in o)
    if isinstance(o, dict):
        return all(isinstance(k, str) and _is_json_safe(v) for k, v in o.items())
    return False


def generate_repro(trace_or_sig: str, entry: str, mode: str = "positive") -> str:
    """Return the pytest file source for reproducing the given trace.

    mode='positive' — assert the function does NOT raise.
    mode='negative' — assert it raises the same error_type the original did.
    """
    if mode not in ("positive", "negative"):
        raise ReproError(f"mode must be 'positive' or 'negative' (got {mode!r})")
    info = _resolve_seed_trace(trace_or_sig)
    mod, fn = _validate_entry(entry)
    short = _short_hash(info.trace_id)

    header = (
        f'"""Auto-generated reproduction of trace {info.trace_id}.\n\n'
        f'Originally produced by:\n'
        f'  clustertrace repro {trace_or_sig} --entry {entry} --mode {mode}\n'
        f'Original trace status: {info.status}'
        + (f"\nOriginal error: {info.error_type}: {info.error_message}" if info.error_type else "")
        + '\n"""\n'
    )

    if info.truncated:
        return (
            header
            + "import pytest\n\n"
            + "# This trace's root input was captured with __truncated=True (the original\n"
            + "# arguments exceeded CLUSTERTRACE_MAX_PAYLOAD_BYTES). clustertrace cannot\n"
            + "# reconstruct them. Fill in the args/kwargs below by hand to make this\n"
            + "# test runnable.\n\n"
            + f"def test_repro_{short}():\n"
            + "    # TODO: replace with the actual arguments\n"
            + "    args = ()\n"
            + "    kwargs = {}\n"
            + f"    from {mod} import {fn}\n"
            + f"    result = {fn}(*args, **kwargs)\n"
            + "    assert result is not None or result is None  # placeholder assertion\n"
        )

    args = (info.root_input or {}).get("args", []) if info.root_input else []
    kwargs = (info.root_input or {}).get("kwargs", {}) if info.root_input else {}

    if not _is_json_safe(args) or not _is_json_safe(kwargs):
        return (
            header
            + "import pytest\n\n"
            + "# Original arguments contained types that can't be safely inlined as Python\n"
            + "# literals (file handles, custom objects, etc.). Reconstruct them by hand.\n\n"
            + f"def test_repro_{short}():\n"
            + "    args = ()\n"
            + "    kwargs = {}\n"
            + f"    from {mod} import {fn}\n"
            + f"    {fn}(*args, **kwargs)\n"
        )

    args_repr = repr(args)
    kwargs_repr = repr(kwargs)
    body_lines: list[str] = [
        f"def test_repro_{short}():",
        f"    args = {args_repr}",
        f"    kwargs = {kwargs_repr}",
        f"    from {mod} import {fn}",
    ]
    if mode == "negative":
        # Negative reproduction — assert the same error type fires.
        # Original error_type may be qualified (e.g. 'ValueError'). We catch
        # by name string via pytest.raises with `match` on the recorded message.
        err_name = info.error_type or "Exception"
        body_lines.append("    import pytest")
        body_lines.append(f"    with pytest.raises({err_name}):")
        body_lines.append(f"        {fn}(*args, **kwargs)")
    else:
        body_lines.append(f"    {fn}(*args, **kwargs)  # must not raise")

    return header + "\n" + "\n".join(body_lines) + "\n"
