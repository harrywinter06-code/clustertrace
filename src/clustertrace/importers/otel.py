"""OTLP/JSON importer.

NOT to be confused with `clustertrace/otel.py` — that module is the OTel
*exporter* (a `SpanExporter` that accepts in-memory OTel spans). This file
parses the OTLP/JSON wire format (per opentelemetry-proto) so we can ingest
exports written by anything that speaks OTLP, regardless of language.

Reuses the same attribute mapping logic as `ClustertraceSpanExporter`:
`gen_ai.*` and `llm.*` flag a span as `llm_call`, `tool.*` flags it as
`tool_call`, otherwise `function`. Token counts and model names normalize
onto `attrs["model"]`, `attrs["input_tokens"]`, `attrs["output_tokens"]`.

OTLP/JSON shape (truncated):
    {
      "resourceSpans": [{
        "resource": { "attributes": [...] },
        "scopeSpans": [{
          "scope": {...},
          "spans": [{
            "traceId": "<32-hex>",
            "spanId":  "<16-hex>",
            "parentSpanId": "<16-hex or empty>",
            "name": "...",
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano":   "1700000000123000000",
            "kind": 1,
            "attributes": [{"key":"k", "value":{"stringValue":"v"}}, ...],
            "events":     [{"name":"exception", "attributes":[...]}],
            "status": {"code": 0 | 1 | 2, "message": "..."}
          }]
        }]
      }]
    }

We accept both camelCase (`traceId`, `startTimeUnixNano`) and snake_case
(`trace_id`, `start_time_unix_nano`) because various exporters disagree.

OTLP/protobuf decoding is out of scope: install
`clustertrace[otel-import]` to add `opentelemetry-proto` if you need it; this
phase only handles OTLP/JSON.
"""
from __future__ import annotations

from typing import IO, Any

from clustertrace import storage
from clustertrace.importers._common import (
    finalize_trace,
    iter_records,
    prefix_id,
    read_stream,
    trace_exists,
)

SOURCE = "otel"

# Mirrors the table in `clustertrace/otel.py::ClustertraceSpanExporter`.
_LLM_MAPPING = [
    ("gen_ai.usage.input_tokens", "input_tokens"),
    ("gen_ai.usage.prompt_tokens", "input_tokens"),
    ("gen_ai.usage.output_tokens", "output_tokens"),
    ("gen_ai.usage.completion_tokens", "output_tokens"),
    ("gen_ai.request.model", "model"),
    ("gen_ai.response.model", "model"),
    ("llm.model", "model"),
    ("llm.model_name", "model"),
    ("llm.usage.prompt_tokens", "input_tokens"),
    ("llm.usage.completion_tokens", "output_tokens"),
]


def _decode_attr_value(v: Any) -> Any:
    """OTLP attribute values are `{"stringValue":...}` / `{"intValue":...}` etc.

    We flatten them into native Python values; if the shape is already
    plain (some non-conforming exporters do this), we pass it through.
    """
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in v:
            val = v[key]
            if key == "intValue" and isinstance(val, str):
                # OTLP encodes int64 as decimal string to survive JSON.
                try:
                    return int(val)
                except ValueError:
                    return val
            return val
    if "arrayValue" in v:
        arr = v["arrayValue"]
        if isinstance(arr, dict) and "values" in arr:
            return [_decode_attr_value(x) for x in arr["values"]]
    if "kvlistValue" in v:
        kv = v["kvlistValue"]
        if isinstance(kv, dict) and "values" in kv:
            return {item.get("key"): _decode_attr_value(item.get("value")) for item in kv["values"]}
    return v


def _flatten_attrs(attrs: list[Any] | dict[str, Any] | None) -> dict[str, Any]:
    """Accept either OTLP `[{"key":..,"value":..}, ...]` or a plain dict."""
    if not attrs:
        return {}
    if isinstance(attrs, dict):
        return {k: _decode_attr_value(v) for k, v in attrs.items()}
    out: dict[str, Any] = {}
    for item in attrs:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        out[key] = _decode_attr_value(item.get("value"))
    return out


def _nano_to_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        # OTLP transmits these as either string (canonical) or number.
        return float(int(value)) / 1e9 if isinstance(value, str) else float(value) / 1e9
    except (TypeError, ValueError):
        return None


def _get(obj: dict[str, Any], *keys: str) -> Any:
    """Return the first present key — handles camelCase vs snake_case mismatch."""
    for k in keys:
        if k in obj:
            return obj[k]
    return None


def _status_of(span: dict[str, Any], events_attrs: dict[str, Any]) -> tuple[str, str | None, str | None]:
    status = _get(span, "status") or {}
    if not isinstance(status, dict):
        status = {}
    code = status.get("code")
    # OTLP/JSON status codes: 0=UNSET, 1=OK, 2=ERROR. Some exporters emit the
    # canonical string name instead.
    is_error = (
        code == 2
        or code == "STATUS_CODE_ERROR"
        or code == "ERROR"
        or (isinstance(code, str) and "ERROR" in code.upper())
    )
    if not is_error:
        return "ok", None, None
    err_type = events_attrs.get("exception.type")
    err_msg = events_attrs.get("exception.message") or status.get("message")
    return "error", err_type, str(err_msg)[:500] if err_msg else None


def _exception_attrs_from_events(events: list[Any] | None) -> dict[str, Any]:
    if not events:
        return {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("name") != "exception":
            continue
        return _flatten_attrs(event.get("attributes"))
    return {}


def _kind_for(attrs: dict[str, Any]) -> str:
    if any(k.startswith("gen_ai.") or k.startswith("llm.") for k in attrs):
        return "llm_call"
    if any(k.startswith("tool.") for k in attrs):
        return "tool_call"
    return "function"


def _normalize_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    out = dict(attrs)
    for src_key, our_key in _LLM_MAPPING:
        if src_key in attrs and our_key not in out:
            out[our_key] = attrs[src_key]
    if out.get("gen_ai.request.streaming") is True or attrs.get("llm.is_streaming") is True:
        out["streaming"] = True
    return out


def _ingest_span(
    span: dict[str, Any],
    trace_meta: dict[str, dict[str, Any]],
) -> tuple[str | None, bool]:
    raw_trace = _get(span, "traceId", "trace_id")
    raw_span = _get(span, "spanId", "span_id")
    raw_parent = _get(span, "parentSpanId", "parent_span_id") or None
    if not raw_trace or not raw_span:
        return None, False

    tid = prefix_id(SOURCE, raw_trace)
    sid = prefix_id(SOURCE, raw_span)
    parent_sid = prefix_id(SOURCE, raw_parent) if raw_parent else None

    raw_attrs = _flatten_attrs(span.get("attributes"))
    events_attrs = _exception_attrs_from_events(span.get("events"))
    attrs = _normalize_attrs(raw_attrs)

    name = str(span.get("name") or "otel.span")
    started = _nano_to_seconds(_get(span, "startTimeUnixNano", "start_time_unix_nano")) or 0.0
    ended = _nano_to_seconds(_get(span, "endTimeUnixNano", "end_time_unix_nano")) or started

    status, err_type, err_msg = _status_of(span, events_attrs)
    kind = _kind_for(raw_attrs)

    created = False
    if not trace_exists(tid):
        storage.insert_trace(tid, name, started)
        created = True

    storage.insert_span(
        span_id=sid,
        trace_id=tid,
        parent_id=parent_sid,
        name=name,
        kind=kind,
        started_at=started,
        input_data=None,
        attrs=attrs,
    )
    storage.finish_span(
        sid,
        ended,
        status,
        output_data=None,
        error_type=err_type,
        error_message=err_msg,
        attrs=attrs,
    )

    meta = trace_meta.setdefault(tid, {"status": "ok", "err_type": None, "err_msg": None, "end": 0.0, "has_root": False})
    if ended > meta["end"]:
        meta["end"] = ended
    if status == "error" and meta["status"] == "ok":
        meta["status"] = "error"
        meta["err_type"] = err_type
        meta["err_msg"] = err_msg
    if not raw_parent:
        meta["has_root"] = True

    return tid, created


def _iter_spans(record: Any) -> list[dict[str, Any]]:
    """Pull a flat list of span dicts from a record in any of OTLP's nestings.

    Accepts:
      - resourceSpans envelope (canonical OTLP/JSON)
      - {"spans":[...]}
      - a bare list of span dicts
      - a single span dict
    """
    out: list[dict[str, Any]] = []
    if isinstance(record, list):
        for item in record:
            out.extend(_iter_spans(item))
        return out
    if not isinstance(record, dict):
        return out
    rs = record.get("resourceSpans") or record.get("resource_spans")
    if isinstance(rs, list):
        for r in rs:
            for ss in (r.get("scopeSpans") or r.get("scope_spans") or []):
                spans = ss.get("spans") or []
                out.extend(s for s in spans if isinstance(s, dict))
        return out
    spans = record.get("spans")
    if isinstance(spans, list):
        out.extend(s for s in spans if isinstance(s, dict))
        return out
    if "name" in record and (
        "traceId" in record or "trace_id" in record or "spanId" in record or "span_id" in record
    ):
        out.append(record)
    return out


def import_otlp(stream: IO[str]) -> tuple[int, int]:
    """Read OTLP/JSON span data from stream. Returns (imported, skipped).

    Imported = new traces inserted (trace_meta entries created with no
    pre-existing DB row). Skipped = records that yielded zero parsable spans
    plus spans we couldn't place (missing IDs etc.).
    """
    text = read_stream(stream)
    imported = 0
    skipped = 0
    trace_meta: dict[str, dict[str, Any]] = {}
    seen_traces: set[str] = set()

    for record in iter_records(text):
        spans = _iter_spans(record)
        if not spans:
            skipped += 1
            continue
        for span in spans:
            try:
                tid, created = _ingest_span(span, trace_meta)
            except Exception:
                tid, created = None, False
            if tid is None:
                skipped += 1
                continue
            if created and tid not in seen_traces:
                imported += 1
                seen_traces.add(tid)

    for tid, meta in trace_meta.items():
        if tid not in seen_traces:
            # Trace already existed in DB before this import — don't touch its status.
            continue
        finalize_trace(
            tid,
            meta["end"],
            meta["status"],
            error_type=meta["err_type"],
            error_message=meta["err_msg"],
        )

    return imported, skipped
