"""Arize Phoenix / OpenInference importer.

Phoenix exports OpenInference-flavored spans. The OpenInference convention
uses `llm.*` / `openinference.*` / `tool.*` attributes layered on top of the
OTel span shape, so this importer is structurally similar to the OTLP one
but consumes Phoenix's flatter JSON export rather than OTLP envelopes.

Accepts either:
  - a single JSON document `{"spans": [...]}` or `[...spans...]`
  - JSONL with one Phoenix span object per line

Mapping table (source → clustertrace):
    span.context.trace_id / trace_id           -> traces.id     (prefixed `phoenix:`)
    span.context.span_id  / span_id            -> spans.id      (prefixed `phoenix:`)
    span.parent_id                              -> spans.parent_id (prefixed `phoenix:`)
    span.name                                   -> spans.name / trace name
    span.start_time / start_time_ns             -> spans.started_at
    span.end_time   / end_time_ns               -> spans.ended_at
    span.status_code == "ERROR"                 -> status=error
    span.attributes["openinference.span.kind"] == "LLM"
                                                -> kind=llm_call
    span.attributes["openinference.span.kind"] == "TOOL"
                                                -> kind=tool_call
    other span.kind values / no kind            -> kind=function
    attributes["llm.model_name" | "llm.model"
              | "gen_ai.request.model"]         -> attrs["model"]
    attributes["llm.token_count.prompt"
              | "gen_ai.usage.prompt_tokens"
              | "gen_ai.usage.input_tokens"]    -> attrs["input_tokens"]
    attributes["llm.token_count.completion"
              | "gen_ai.usage.completion_tokens"
              | "gen_ai.usage.output_tokens"]   -> attrs["output_tokens"]
    attributes["input.value"]                   -> spans.input_json
    attributes["output.value"]                  -> spans.output_json
    attributes (everything else)                -> attrs[k]
    Phoenix project tag (`session.id` etc.)     -> trace_tags

Phoenix records `start_time`/`end_time` as ISO strings or nanoseconds; both
are accepted.
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

SOURCE = "phoenix"

_LLM_MODEL_KEYS = ("llm.model_name", "llm.model", "gen_ai.request.model", "gen_ai.response.model")
_INPUT_TOKEN_KEYS = (
    "llm.token_count.prompt",
    "gen_ai.usage.prompt_tokens",
    "gen_ai.usage.input_tokens",
    "llm.usage.prompt_tokens",
)
_OUTPUT_TOKEN_KEYS = (
    "llm.token_count.completion",
    "gen_ai.usage.completion_tokens",
    "gen_ai.usage.output_tokens",
    "llm.usage.completion_tokens",
)


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        v = float(value)
        # Heuristic: anything > 1e12 is plausibly ns (years 33658+ in seconds);
        # > 1e10 is ms. Below that, assume seconds.
        if v > 1e15:
            return v / 1e9
        if v > 1e12:
            return v / 1e3
        return v
    if isinstance(value, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _kind_for(attrs: dict[str, Any]) -> str:
    span_kind = attrs.get("openinference.span.kind") or attrs.get("span.kind")
    if isinstance(span_kind, str):
        k = span_kind.upper()
        if k == "LLM":
            return "llm_call"
        if k == "TOOL":
            return "tool_call"
        if k in {"CHAIN", "AGENT", "RETRIEVER", "EMBEDDING", "RERANKER", "GUARDRAIL", "EVALUATOR"}:
            return "function"
    if any(k.startswith("llm.") for k in attrs):
        return "llm_call"
    if any(k.startswith("tool.") for k in attrs):
        return "tool_call"
    return "function"


def _extract_attrs(raw_attrs: dict[str, Any]) -> dict[str, Any]:
    """Normalize Phoenix/OpenInference attrs onto clustertrace's standard keys.

    Keep the originals too so downstream tooling can still introspect them.
    """
    out: dict[str, Any] = dict(raw_attrs)
    for k in _LLM_MODEL_KEYS:
        if k in raw_attrs and "model" not in out:
            out["model"] = raw_attrs[k]
            break
    for k in _INPUT_TOKEN_KEYS:
        if k in raw_attrs:
            v = safe_int(raw_attrs[k])
            if v is not None:
                out["input_tokens"] = v
                break
    for k in _OUTPUT_TOKEN_KEYS:
        if k in raw_attrs:
            v = safe_int(raw_attrs[k])
            if v is not None:
                out["output_tokens"] = v
                break
    if raw_attrs.get("llm.invocation_parameters.stream") is True:
        out["streaming"] = True
    return out


def _ids_from(span: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extract (trace_id, span_id, parent_id) regardless of nesting style."""
    ctx = span.get("context") or {}
    trace_id = ctx.get("trace_id") or span.get("trace_id")
    span_id = ctx.get("span_id") or span.get("span_id") or span.get("id")
    parent_id = span.get("parent_id") or span.get("parent_span_id")
    return (
        str(trace_id) if trace_id is not None else None,
        str(span_id) if span_id is not None else None,
        str(parent_id) if parent_id else None,
    )


def _status_of(span: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Return (status, error_type, error_message)."""
    raw = span.get("status_code") or span.get("status") or "OK"
    if isinstance(raw, dict):
        raw = raw.get("code") or raw.get("status_code") or "OK"
    s = str(raw).upper()
    if "ERROR" in s or s == "FAIL":
        err_type = None
        err_msg = span.get("status_message") or span.get("error_message")
        for event in span.get("events") or []:
            if not isinstance(event, dict):
                continue
            if event.get("name") == "exception":
                eattrs = event.get("attributes") or {}
                err_type = err_type or eattrs.get("exception.type")
                err_msg = err_msg or eattrs.get("exception.message")
        return "error", err_type or "PhoenixError", str(err_msg)[:500] if err_msg else None
    return "ok", None, None


def _import_one_span(span: dict[str, Any]) -> tuple[str | None, bool]:
    """Import one Phoenix span. Returns (trace_id, created_new_trace).

    Spans share traces; we upsert the trace row on the first span we see for
    a given trace_id, exactly like the OTel exporter does.
    """
    raw_trace, raw_span, raw_parent = _ids_from(span)
    if not raw_trace or not raw_span:
        return None, False

    tid = prefix_id(SOURCE, raw_trace)
    sid = prefix_id(SOURCE, raw_span)
    parent_sid = prefix_id(SOURCE, raw_parent) if raw_parent else None

    raw_attrs = span.get("attributes") or {}
    if not isinstance(raw_attrs, dict):
        raw_attrs = {}
    attrs = _extract_attrs(raw_attrs)

    name = str(span.get("name") or "phoenix.span")
    started = _parse_time(
        span.get("start_time") or span.get("start_time_ns") or span.get("started_at")
    ) or 0.0
    ended = _parse_time(
        span.get("end_time") or span.get("end_time_ns") or span.get("ended_at")
    ) or started

    status, err_type, err_msg = _status_of(span)
    kind = _kind_for(raw_attrs)

    created_trace = False
    if not trace_exists(tid):
        storage.insert_trace(tid, name, started)
        created_trace = True
        # Session id / user id surface as tags for filtering in the dashboard.
        for tag_key in ("session.id", "user.id"):
            if tag_key in raw_attrs:
                storage.add_trace_tag(tid, tag_key, str(raw_attrs[tag_key])[:200])

    storage.insert_span(
        span_id=sid,
        trace_id=tid,
        parent_id=parent_sid,
        name=name,
        kind=kind,
        started_at=started,
        input_data=raw_attrs.get("input.value"),
        attrs=attrs,
    )
    storage.finish_span(
        sid,
        ended,
        status,
        output_data=raw_attrs.get("output.value"),
        error_type=err_type,
        error_message=err_msg,
        attrs=attrs,
    )
    return tid, created_trace


def import_phoenix(stream: IO[str]) -> tuple[int, int]:
    """Read a Phoenix/OpenInference export from stream. Returns (imported, skipped).

    `imported` counts NEW traces (first span we saw for a previously unseen
    trace_id). `skipped` counts spans we couldn't place — bad/missing IDs,
    parse failures, or spans that belong to a trace ID that already existed
    in the DB before this import.
    """
    text = read_stream(stream)
    imported = 0
    skipped = 0
    seen_traces: dict[str, str] = {}  # tid -> final_status
    seen_traces_end: dict[str, float] = {}
    span_errors: dict[str, tuple[str, str | None, str | None]] = {}
    pre_existing: set[str] = set()

    for record in iter_records(text):
        spans = _extract_spans(record)
        if not spans:
            skipped += 1
            continue
        for span in spans:
            try:
                tid, created = _import_one_span(span)
            except Exception:
                tid, created = None, False
            if tid is None:
                skipped += 1
                continue
            if created:
                imported += 1
                seen_traces[tid] = "ok"
            elif tid not in seen_traces:
                # Trace pre-existed in DB before this run; span attached but
                # don't double-count the trace as newly imported.
                pre_existing.add(tid)
            # Track latest end time + any error to roll into the trace status.
            status, err_type, err_msg = _status_of(span)
            ended = _parse_time(
                span.get("end_time") or span.get("end_time_ns") or span.get("ended_at")
            )
            if ended is not None and ended > seen_traces_end.get(tid, 0.0):
                seen_traces_end[tid] = ended
            if status == "error" and tid not in span_errors:
                span_errors[tid] = (status, err_type, err_msg)

    # Finalize every trace we touched. Errors anywhere in the trace promote it
    # to "error" — mirrors what ClustertraceSpanExporter does for OTel.
    for tid in seen_traces:
        status = "ok"
        err_type: str | None = None
        err_msg: str | None = None
        if tid in span_errors:
            status, err_type, err_msg = span_errors[tid]
        finalize_trace(tid, seen_traces_end.get(tid, 0.0), status, err_type, err_msg)

    return imported, skipped


def _extract_spans(record: Any) -> list[dict[str, Any]]:
    if isinstance(record, dict):
        if "spans" in record and isinstance(record["spans"], list):
            return [s for s in record["spans"] if isinstance(s, dict)]
        # Looks like a single span: has name + (context|span_id|trace_id)
        if "name" in record and any(
            k in record or k in (record.get("context") or {})
            for k in ("span_id", "trace_id", "id")
        ):
            return [record]
        return []
    if isinstance(record, list):
        return [s for s in record if isinstance(s, dict)]
    return []
