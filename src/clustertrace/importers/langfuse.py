"""Langfuse export importer.

Langfuse exports a JSON document containing a list of traces; each trace
carries `observations` (their span equivalent). We accept either a single
JSON object with a top-level `traces` array (the documented export shape)
or JSONL — one trace object per line — and detect automatically.

Mapping table (source → clustertrace):
    trace.id                        -> traces.id           (prefixed `langfuse:`)
    trace.name                      -> traces.name
    trace.timestamp / startTime     -> traces.started_at
    trace.endTime                   -> traces.ended_at
    trace.tags (list[str])          -> trace_tags(key=tag, value="1")
    trace.metadata (dict)           -> trace_tags(key=meta.<k>, value=<v>)
    observation.id                  -> spans.id            (prefixed `langfuse:`)
    observation.parentObservationId -> spans.parent_id     (prefixed `langfuse:`)
    observation.type=="GENERATION"  -> spans.kind=llm_call
    observation.type=="SPAN"        -> spans.kind=function
    observation.type=="EVENT"       -> spans.kind=function
    observation.model               -> attrs["model"]
    observation.usage.input         -> attrs["input_tokens"]
    observation.usage.output        -> attrs["output_tokens"]
    observation.usage.promptTokens  -> attrs["input_tokens"]      (legacy field)
    observation.usage.completionTokens -> attrs["output_tokens"]  (legacy field)
    observation.input               -> spans.input_json
    observation.output              -> spans.output_json
    observation.level=="ERROR"      -> spans.status=error
    observation.statusMessage       -> spans.error_message

Streaming flag is not part of the documented Langfuse schema, so it's omitted
unless the source records it explicitly as `observation.streaming=true`.

Return value: `(imported_traces, skipped)`. A trace is "skipped" when its
prefixed ID already exists, when it has no usable ID, or when a JSONL line
fails to parse.
"""
from __future__ import annotations

from typing import IO, Any

from clustertrace import storage
from clustertrace.importers._common import (
    finalize_trace,
    iter_records,
    prefix_id,
    read_stream,
    safe_int,
    trace_exists,
)

SOURCE = "langfuse"


def _parse_timestamp(value: Any) -> float | None:
    """Langfuse uses ISO-8601 strings; clustertrace stores POSIX seconds."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        try:
            # fromisoformat handles "2026-05-21T10:00:00+00:00" and "...Z" in 3.11+
            s = value.replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


def _kind_for(obs_type: str | None) -> str:
    t = (obs_type or "").upper()
    if t == "GENERATION":
        return "llm_call"
    return "function"


def _normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Pull token counts out of the Langfuse usage block under whichever names."""
    out: dict[str, Any] = {}
    # Current schema
    if "input" in usage:
        v = safe_int(usage.get("input"))
        if v is not None:
            out["input_tokens"] = v
    if "output" in usage:
        v = safe_int(usage.get("output"))
        if v is not None:
            out["output_tokens"] = v
    # Legacy names still emitted by older Langfuse versions
    if "input_tokens" not in out:
        v = safe_int(usage.get("promptTokens") or usage.get("prompt_tokens"))
        if v is not None:
            out["input_tokens"] = v
    if "output_tokens" not in out:
        v = safe_int(usage.get("completionTokens") or usage.get("completion_tokens"))
        if v is not None:
            out["output_tokens"] = v
    return out


def _import_one_trace(trace: dict[str, Any]) -> bool:
    raw_id = trace.get("id")
    tid = prefix_id(SOURCE, raw_id)
    if not tid or tid == f"{SOURCE}:":
        return False
    if trace_exists(tid):
        return False

    started = (
        _parse_timestamp(trace.get("timestamp"))
        or _parse_timestamp(trace.get("startTime"))
        or _parse_timestamp(trace.get("createdAt"))
        or 0.0
    )
    name = str(trace.get("name") or "langfuse.trace")
    storage.insert_trace(tid, name, started)

    # Tags: Langfuse stores tags as a list of strings.
    for t in trace.get("tags") or []:
        if t is None:
            continue
        storage.add_trace_tag(tid, str(t), "1")

    # Metadata: dict-of-anything -> prefixed tag rows.
    meta = trace.get("metadata") or {}
    if isinstance(meta, dict):
        for k, v in meta.items():
            if v is None:
                continue
            storage.add_trace_tag(tid, f"meta.{k}", str(v)[:200])

    observations = trace.get("observations") or []
    last_end = started
    final_status = "ok"
    final_err_type: str | None = None
    final_err_msg: str | None = None

    for obs in observations:
        if not isinstance(obs, dict):
            continue
        raw_sid = obs.get("id")
        if not raw_sid:
            continue
        sid = prefix_id(SOURCE, raw_sid)
        parent_raw = obs.get("parentObservationId")
        parent = prefix_id(SOURCE, parent_raw) if parent_raw else None

        kind = _kind_for(obs.get("type"))
        attrs: dict[str, Any] = {}
        if obs.get("model"):
            attrs["model"] = obs["model"]
        usage = obs.get("usage") or {}
        if isinstance(usage, dict):
            attrs.update(_normalize_usage(usage))
        if obs.get("streaming") is True:
            attrs["streaming"] = True

        span_started = (
            _parse_timestamp(obs.get("startTime"))
            or _parse_timestamp(obs.get("timestamp"))
            or started
        )
        span_ended = _parse_timestamp(obs.get("endTime")) or span_started
        if span_ended > last_end:
            last_end = span_ended

        level = (obs.get("level") or "").upper()
        if level == "ERROR":
            status = "error"
            err_msg = obs.get("statusMessage") or obs.get("output") or "langfuse error"
            err_type = "LangfuseError"
            final_status = "error"
            if final_err_msg is None:
                final_err_msg = str(err_msg)[:500]
                final_err_type = err_type
        else:
            status = "ok"
            err_msg = None
            err_type = None

        span_name = obs.get("name") or f"langfuse.{kind}"
        storage.insert_span(
            span_id=sid,
            trace_id=tid,
            parent_id=parent,
            name=str(span_name),
            kind=kind,
            started_at=span_started,
            input_data=obs.get("input"),
            attrs=attrs,
        )
        storage.finish_span(
            sid,
            span_ended,
            status,
            output_data=obs.get("output"),
            error_type=err_type,
            error_message=str(err_msg)[:500] if err_msg else None,
            attrs=attrs,
        )

    finalize_trace(
        tid,
        _parse_timestamp(trace.get("endTime")) or last_end,
        final_status,
        error_type=final_err_type,
        error_message=final_err_msg,
    )
    return True


def import_langfuse(stream: IO[str]) -> tuple[int, int]:
    """Read a Langfuse JSON or JSONL export from stream and import every trace.

    Accepts:
      - one big JSON document `{"traces": [...]}` or `[ ...traces... ]`
      - JSONL: one trace JSON object per line
      - a single JSON trace object

    Returns (imported, skipped).
    """
    text = read_stream(stream)
    imported = 0
    skipped = 0
    for record in iter_records(text):
        traces = _extract_traces(record)
        if not traces:
            skipped += 1
            continue
        for tr in traces:
            try:
                ok = _import_one_trace(tr)
            except Exception:
                ok = False
            if ok:
                imported += 1
            else:
                skipped += 1
    return imported, skipped


def _extract_traces(record: Any) -> list[dict[str, Any]]:
    """Pull a list of trace dicts out of whatever shape the line decoded into."""
    if isinstance(record, dict):
        if "traces" in record and isinstance(record["traces"], list):
            return [t for t in record["traces"] if isinstance(t, dict)]
        # A single trace dict (no envelope)
        if "id" in record:
            return [record]
        return []
    if isinstance(record, list):
        return [t for t in record if isinstance(t, dict)]
    return []
