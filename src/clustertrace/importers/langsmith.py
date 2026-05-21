"""LangSmith export importer.

LangSmith calls a recorded execution a "run". A top-level run with
`parent_run_id == null` is the root of a trace; child runs are spans.

Accepts:
  - a single JSON document `{"runs": [...]}` or `[ ...runs... ]`
  - JSONL with one run per line (any order — children before parents is fine)
  - a single run object

Mapping table (source → clustertrace):
    root_run.id                     -> traces.id            (prefixed `langsmith:`)
    run.id                          -> spans.id             (prefixed `langsmith:`)
    run.parent_run_id               -> spans.parent_id      (prefixed `langsmith:`)
    run.trace_id (if explicit)      -> overrides which trace this row belongs to
    run.name                        -> spans.name / trace name
    run.start_time                  -> started_at
    run.end_time                    -> ended_at
    run.run_type == "llm"           -> kind=llm_call
    run.run_type == "tool"          -> kind=tool_call
    other / null                    -> kind=function
    run.inputs                      -> spans.input_json
    run.outputs                     -> spans.output_json
    run.tags (list[str])            -> trace_tags(key=tag, value="1") on the
                                       owning trace
    run.extra.invocation_params.model
        / run.extra.metadata.ls_model_name
        / run.serialized.name        -> attrs["model"]
    run.prompt_tokens                -> attrs["input_tokens"]
    run.completion_tokens            -> attrs["output_tokens"]
    run.error / run.status=="error"  -> spans.status=error + propagated to trace

Non-hierarchical nesting: when a child run names a `parent_run_id` whose
parent we never see (export was filtered or split), the child is still
imported under the run's `trace_id` if present, otherwise the child's own
ID is treated as its trace ID. The hard rule is "never crash on incomplete
data" — LangSmith exports do drop ancestry occasionally.
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

SOURCE = "langsmith"


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _kind_for(run_type: str | None) -> str:
    t = (run_type or "").lower()
    if t == "llm":
        return "llm_call"
    if t == "tool":
        return "tool_call"
    return "function"


def _extract_model(run: dict[str, Any]) -> Any:
    extra = run.get("extra") or {}
    if isinstance(extra, dict):
        invoke = extra.get("invocation_params") or {}
        if isinstance(invoke, dict) and invoke.get("model"):
            return invoke["model"]
        meta = extra.get("metadata") or {}
        if isinstance(meta, dict):
            for k in ("ls_model_name", "model", "model_name"):
                if k in meta:
                    return meta[k]
    serialized = run.get("serialized") or {}
    if isinstance(serialized, dict) and serialized.get("name"):
        return serialized["name"]
    return run.get("model")


def _build_attrs(run: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    model = _extract_model(run)
    if model is not None:
        attrs["model"] = model
    pt = safe_int(run.get("prompt_tokens"))
    if pt is not None:
        attrs["input_tokens"] = pt
    ct = safe_int(run.get("completion_tokens"))
    if ct is not None:
        attrs["output_tokens"] = ct
    # Some LangSmith versions emit a nested usage block on the LLM run's outputs.
    outputs = run.get("outputs") or {}
    if isinstance(outputs, dict):
        usage = outputs.get("llm_output", {}).get("token_usage") if isinstance(outputs.get("llm_output"), dict) else None
        if isinstance(usage, dict):
            if "input_tokens" not in attrs:
                v = safe_int(usage.get("prompt_tokens"))
                if v is not None:
                    attrs["input_tokens"] = v
            if "output_tokens" not in attrs:
                v = safe_int(usage.get("completion_tokens"))
                if v is not None:
                    attrs["output_tokens"] = v
    extra = run.get("extra") or {}
    if isinstance(extra, dict):
        invoke = extra.get("invocation_params") or {}
        if isinstance(invoke, dict) and invoke.get("stream") is True:
            attrs["streaming"] = True
    return attrs


def _trace_id_for(run: dict[str, Any]) -> str:
    """Resolve which clustertrace trace this run belongs to.

    Priority: explicit `trace_id` -> parent chain root (best effort, single
    hop only — we don't traverse) -> own id. The own-id fallback means an
    orphaned child still produces a self-rooted trace rather than being lost.
    """
    if run.get("trace_id"):
        return str(run["trace_id"])
    # If there's no parent, this IS the root.
    if not run.get("parent_run_id"):
        return str(run.get("id") or "")
    # Otherwise treat the parent as the trace root (single-hop assumption).
    return str(run.get("parent_run_id") or run.get("id") or "")


def _import_one_run(
    run: dict[str, Any], trace_started: dict[str, float], trace_status: dict[str, tuple[str, str | None, str | None]],
    trace_end: dict[str, float],
) -> tuple[bool, str | None]:
    raw_id = run.get("id")
    if not raw_id:
        return False, None

    raw_trace = _trace_id_for(run)
    tid = prefix_id(SOURCE, raw_trace)
    sid = prefix_id(SOURCE, raw_id)
    parent_raw = run.get("parent_run_id")
    parent_sid = prefix_id(SOURCE, parent_raw) if parent_raw else None

    name = str(run.get("name") or "langsmith.run")
    started = _parse_time(run.get("start_time")) or 0.0
    ended = _parse_time(run.get("end_time")) or started

    created_trace = False
    if not trace_exists(tid):
        storage.insert_trace(tid, name, started)
        created_trace = True
        # Apply trace-level tags only when the trace row is created. Child runs
        # carry their own tags, but LangSmith treats `tags` as trace-wide.
        for t in run.get("tags") or []:
            if t is None:
                continue
            storage.add_trace_tag(tid, str(t), "1")

    attrs = _build_attrs(run)
    kind = _kind_for(run.get("run_type"))

    err = run.get("error")
    status_field = (run.get("status") or "").lower()
    if err or status_field == "error":
        span_status = "error"
        err_msg = str(err) if err else (run.get("error_message") or "langsmith error")
        err_type = "LangSmithError"
    else:
        span_status = "ok"
        err_msg = None
        err_type = None

    storage.insert_span(
        span_id=sid,
        trace_id=tid,
        parent_id=parent_sid,
        name=name,
        kind=kind,
        started_at=started,
        input_data=run.get("inputs"),
        attrs=attrs,
    )
    storage.finish_span(
        sid,
        ended,
        span_status,
        output_data=run.get("outputs"),
        error_type=err_type,
        error_message=err_msg,
        attrs=attrs,
    )

    if span_status == "error" and tid not in trace_status:
        trace_status[tid] = ("error", err_type, err_msg)
    if ended > trace_end.get(tid, 0.0):
        trace_end[tid] = ended
    if tid not in trace_started:
        trace_started[tid] = started

    return created_trace, tid


def import_langsmith(stream: IO[str]) -> tuple[int, int]:
    """Read a LangSmith export from stream. Returns (imported_traces, skipped).

    `imported` counts new traces (not new spans). `skipped` counts runs we
    couldn't place (missing id, parse failure) and runs whose trace already
    existed in the DB before this import.
    """
    text = read_stream(stream)
    imported = 0
    skipped = 0
    trace_started: dict[str, float] = {}
    trace_status: dict[str, tuple[str, str | None, str | None]] = {}
    trace_end: dict[str, float] = {}
    created_here: set[str] = set()

    for record in iter_records(text):
        runs = _extract_runs(record)
        if not runs:
            skipped += 1
            continue
        for run in runs:
            try:
                created, tid = _import_one_run(run, trace_started, trace_status, trace_end)
            except Exception:
                created, tid = False, None
            if tid is None:
                skipped += 1
                continue
            if created:
                imported += 1
                created_here.add(tid)

    # Finalize every trace once we've seen ALL of its runs. Doing this in a
    # second pass is what makes child-error-before-root work — finalizing
    # eagerly on the root run would lock in "ok" before later children land.
    for tid in created_here:
        status, err_type, err_msg = trace_status.get(tid, ("ok", None, None))
        finalize_trace(tid, trace_end.get(tid, 0.0), status, err_type, err_msg)

    return imported, skipped


def _extract_runs(record: Any) -> list[dict[str, Any]]:
    if isinstance(record, dict):
        if "runs" in record and isinstance(record["runs"], list):
            return [r for r in record["runs"] if isinstance(r, dict)]
        if "id" in record:
            return [record]
        return []
    if isinstance(record, list):
        return [r for r in record if isinstance(r, dict)]
    return []
