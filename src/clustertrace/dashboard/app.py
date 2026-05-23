"""FastAPI dashboard for clustertrace.

Routes:
  GET /                        recent traces list
  GET /trace/{trace_id}        single-trace timeline
  GET /failures                aggregate failure-pattern visualization
  GET /api/traces              JSON: recent traces
  GET /api/trace/{trace_id}    JSON: spans for one trace
  GET /api/failure-graph       JSON: nodes + transitions for the viz
  GET /api/stats               JSON: top-level counters
  POST /v1/traces              OTLP/JSON span ingestion (TS / browser / any OTel SDK)
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from clustertrace import annotations as _ann
from clustertrace import assertions as _ass
from clustertrace import cluster, drift, maintenance, storage
from clustertrace.otel import ClustertraceSpanExporter

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC_DIR = _HERE / "static"

app = FastAPI(title="clustertrace", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _decode_json(value: str | None):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Landing page is the pattern view — that IS the pitch (cluster failing
    # traces by execution pattern). The flat trace list moved to /traces so
    # visitors don't land on the generic-table page that contradicts the pitch.
    return _TEMPLATES.TemplateResponse(request, "clusters.html", {})


@app.get("/traces", response_class=HTMLResponse)
async def traces_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "index.html", {})


@app.get("/trace/{trace_id}", response_class=HTMLResponse)
async def trace_detail(request: Request, trace_id: str):
    with storage.connect() as conn:
        trace = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
    return _TEMPLATES.TemplateResponse(
        request,
        "trace.html",
        {"trace_id": trace_id, "trace_name": trace["name"]},
    )


@app.get("/failures", response_class=HTMLResponse)
async def failures(request: Request):
    return _TEMPLATES.TemplateResponse(request, "failures.html", {})


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "search.html", {})


@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "metrics.html", {})


@app.get("/clusters", response_class=HTMLResponse)
async def clusters_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "clusters.html", {})


@app.get("/drift", response_class=HTMLResponse)
async def drift_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "drift.html", {})


@app.get("/api/cluster-drift")
async def api_cluster_drift(window: str = "24h", compare: str = "24h"):
    """Cluster failure-rate change between two adjacent time windows.

    `window` is the current window (anchored at now), `compare` is the
    immediately-preceding window of the same or different length. Accepts
    `24h`, `7d`, `30d`, `60s`, etc. — same syntax as `clustertrace cleanup`.
    """
    try:
        window_s = maintenance.parse_duration(window)
        compare_s = maintenance.parse_duration(compare)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        cluster.backfill_signatures()
    except Exception:
        pass
    return drift.compute_drift(window_seconds=window_s, compare_seconds=compare_s)


@app.get("/api/clusters")
async def api_clusters(
    limit: int = 50,
    offset: int = 0,
    mode: str = "ordered",
    backfill: bool = True,
    threshold: int | None = None,
):
    """List execution clusters.

    mode='ordered' (default) uses the stored signature (cheap, indexed).
    mode='set' recomputes on the fly so reorderings and retries collapse.
    mode='tree_edit' groups traces by Wagner-Fischer edit distance — one
        extra retry or reordering no longer splits a cluster. `threshold`
        overrides the auto-computed value (max(2, 0.1 × median length)).
    """
    if backfill:
        try:
            cluster.backfill_signatures()
        except Exception:
            pass
    out = []
    if mode not in ("ordered", "set", "tree_edit"):
        mode = "ordered"
    # Pass threshold through as a function argument rather than mutating a
    # module-level global — under concurrent FastAPI requests the global
    # would race between the set/read/reset sequence.
    clusters = cluster.list_clusters(
        limit=limit, offset=offset, mode=mode, threshold=threshold
    )
    sig_hashes = [cl.sig_hash for cl in clusters]
    annotations_map = {a["sig_hash"]: a for a in _ann.all_annotations()
                       if a["sig_hash"] in set(sig_hashes)}
    judgments_map = storage.latest_judgments_by_sig_hash(sig_hashes)
    for cl in clusters:
        out.append(
            {
                "signature": cl.signature,
                "sig_hash": cl.sig_hash,
                "count": cl.count,
                "errors": cl.error_count,
                "error_rate": cl.error_rate,
                "avg_duration_ms": cl.avg_duration_ms,
                "representative_trace_id": cl.representative_trace_id,
                "pattern": [{"name": n, "status": s} for n, s in cl.pattern],
                "annotation": annotations_map.get(cl.sig_hash),
                "latest_judgment": judgments_map.get(cl.sig_hash),
            }
        )
    return {"clusters": out, "mode": mode, "limit": limit, "offset": offset}


@app.get("/api/failure-summary")
async def api_failure_summary(group_by_tag: str = "agent"):
    """Failure-pattern summary. Annotated `expected-failure` clusters drop out
    of the headline counts (with a `+N expected` note), so a known-broken
    cluster doesn't dominate the page forever.
    """
    try:
        cluster.backfill_signatures()
    except Exception:
        pass
    summary = cluster.failure_summary(group_by_tag=group_by_tag or None)

    # Drop expected-failure clusters from the headline `traces_failed` and the
    # ranked cluster list, but keep them visible under their own counters so
    # the UI can show the footnote.
    expected_hashes = _ann.expected_failure_sig_hashes()
    annotations_map = {a["sig_hash"]: a for a in _ann.all_annotations()}
    expected_traces_count = 0
    surviving_clusters: list[dict] = []
    for c in summary["clusters"]:
        ann = annotations_map.get(c["sig_hash"])
        if ann is not None:
            c = {**c, "annotation": ann}
        if c["sig_hash"] in expected_hashes:
            expected_traces_count += int(c["errors"])
            # surface it but flag it — the dashboard renders these greyed out
            c = {**c, "expected_failure": True}
        surviving_clusters.append(c)

    headline_failed = max(0, summary["traces_failed"] - expected_traces_count)
    total = summary["traces_total"]
    summary["traces_failed_after_annotations"] = headline_failed
    summary["expected_failure_traces"] = expected_traces_count
    summary["overall_failure_rate_after_annotations"] = (
        (headline_failed / total) if total else 0.0
    )
    summary["clusters"] = surviving_clusters
    return summary


@app.get("/api/cluster-judgments/{sig_hash}")
async def api_cluster_judgments(sig_hash: str):
    """Return the latest cluster judgment (or None) for a given sig_hash."""
    return {
        "sig_hash": sig_hash,
        "latest": storage.get_latest_cluster_judgment(sig_hash),
    }


@app.post("/api/cluster-annotations")
async def api_post_cluster_annotation(request: Request):
    """Create or update an annotation. Body: `{sig_hash, status?, note?, tag?}`."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected JSON object")
    sig_hash = payload.get("sig_hash")
    if not sig_hash or not isinstance(sig_hash, str):
        raise HTTPException(status_code=400, detail="sig_hash required")
    status = payload.get("status")
    note = payload.get("note")
    tag = payload.get("tag")
    if status == "clear":
        removed = _ann.clear_annotation(sig_hash)
        return {"sig_hash": sig_hash, "cleared": removed}
    if status is None and note is None and tag is None:
        raise HTTPException(
            status_code=400,
            detail="at least one of status / note / tag must be provided",
        )
    try:
        out = _ann.annotate_cluster(sig_hash, status=status, note=note, tag=tag)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return out


@app.get("/api/cluster-annotations")
async def api_list_cluster_annotations():
    return {"annotations": _ann.all_annotations()}


@app.get("/api/cluster-assertions")
async def api_list_cluster_assertions():
    return {"assertions": _ass.list_assertions()}


@app.get("/api/tags")
async def api_tags():
    """All known tag keys + their value counts. Powers the filter dropdown."""
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT key, value, COUNT(*) c FROM trace_tags GROUP BY key, value ORDER BY key, c DESC"
        ).fetchall()
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append({"value": r["value"], "count": r["c"]})
    return {"tags": by_key}


@app.get("/api/search")
async def api_search(q: str, limit: int = 50):
    """Full-text search over span name + input + output + error_message."""
    if not q or not q.strip():
        return {"results": [], "query": q}
    limit = max(1, min(limit, 200))
    # FTS5 query: quote to allow phrases; otherwise let users use FTS5 operators.
    fts_query = q.strip()
    with storage.connect() as conn:
        try:
            rows = conn.execute(
                """SELECT spans_fts.trace_id, spans_fts.span_id, spans_fts.name,
                          snippet(spans_fts, 3, '<mark>', '</mark>', '...', 16) AS snippet,
                          t.status AS trace_status
                   FROM spans_fts
                   JOIN traces t ON t.id = spans_fts.trace_id
                   WHERE spans_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        except Exception:
            # Treat parse errors as zero results so the UI doesn't crash on odd input.
            return {"results": [], "query": q, "error": "search parse error"}
    return {
        "results": [
            {
                "trace_id": r["trace_id"],
                "span_id": r["span_id"],
                "name": r["name"],
                "snippet": r["snippet"],
                "trace_status": r["trace_status"],
            }
            for r in rows
        ],
        "query": q,
    }


@app.get("/api/metrics")
async def api_metrics():
    """All metric names + per-name counts and recent values for charting."""
    with storage.connect() as conn:
        agg = conn.execute(
            """SELECT name, COUNT(*) AS n, AVG(value) AS avg_v, MIN(value) AS min_v, MAX(value) AS max_v
               FROM trace_metrics GROUP BY name ORDER BY name"""
        ).fetchall()
        series_rows = conn.execute(
            """SELECT m.name, m.value, m.recorded_at, t.status
               FROM trace_metrics m JOIN traces t ON t.id = m.trace_id
               ORDER BY m.recorded_at ASC"""
        ).fetchall()
    by_name: dict[str, list[dict]] = {}
    for r in series_rows:
        by_name.setdefault(r["name"], []).append(
            {"value": r["value"], "at": r["recorded_at"], "trace_status": r["status"]}
        )
    return {
        "aggregate": [
            {
                "name": r["name"],
                "n": r["n"],
                "avg": round(r["avg_v"], 4) if r["avg_v"] is not None else None,
                "min": r["min_v"],
                "max": r["max_v"],
            }
            for r in agg
        ],
        "series": by_name,
    }


@app.get("/api/stats")
async def api_stats():
    with storage.connect() as conn:
        traces_total = conn.execute("SELECT COUNT(*) AS c FROM traces").fetchone()["c"]
        traces_error = conn.execute(
            "SELECT COUNT(*) AS c FROM traces WHERE status = 'error'"
        ).fetchone()["c"]
        spans_total = conn.execute("SELECT COUNT(*) AS c FROM spans").fetchone()["c"]
        spans_error = conn.execute(
            "SELECT COUNT(*) AS c FROM spans WHERE status = 'error'"
        ).fetchone()["c"]
        total_cost = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM traces"
        ).fetchone()[0] or 0.0
        recent = conn.execute(
            """SELECT name, status, started_at FROM traces ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
    return {
        "traces_total": traces_total,
        "traces_error": traces_error,
        "traces_error_rate": (traces_error / traces_total) if traces_total else 0.0,
        "spans_total": spans_total,
        "spans_error": spans_error,
        "total_cost_usd": round(total_cost, 4),
        "latest_trace": _row_to_dict(recent) if recent else None,
    }


@app.get("/api/traces")
async def api_traces(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    q: str | None = Query(None, description="case-insensitive name match"),
    tag: list[str] | None = Query(None, description="repeat as ?tag=key=value"),
):
    limit = max(1, min(limit, 500))
    where: list[str] = []
    params: list = []
    if status in ("ok", "error", "running"):
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("LOWER(name) LIKE ?")
        params.append(f"%{q.lower()}%")
    join_tag = ""
    if tag:
        # AND-style filter: trace must have ALL specified tags.
        for i, kv in enumerate(tag):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            alias = f"tt{i}"
            join_tag += f" INNER JOIN trace_tags {alias} ON {alias}.trace_id = traces.id AND {alias}.key = ? AND {alias}.value = ?"
            params.extend([k, v])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT traces.id, traces.name, traces.started_at, traces.ended_at,
                     traces.status, traces.error_type, traces.error_message,
                     traces.signature, traces.cost_usd
              FROM traces {join_tag}
              {where_sql}
              ORDER BY traces.started_at DESC
              LIMIT ? OFFSET ?"""
    params.extend([limit, offset])
    with storage.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        # also count total matching (without limit) for pagination
        count_sql = f"SELECT COUNT(*) FROM traces {join_tag} {where_sql}"
        total = conn.execute(count_sql, params[:-2]).fetchone()[0]
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d.get("ended_at") and d.get("started_at"):
            d["duration_ms"] = round((d["ended_at"] - d["started_at"]) * 1000, 1)
        else:
            d["duration_ms"] = None
        out.append(d)
    return {"traces": out, "limit": limit, "offset": offset, "total": total}


@app.get("/api/trace/{trace_id}")
async def api_trace(trace_id: str):
    with storage.connect() as conn:
        trace = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        spans = conn.execute(
            """SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at ASC""",
            (trace_id,),
        ).fetchall()
        tag_rows = conn.execute(
            "SELECT key, value FROM trace_tags WHERE trace_id = ?", (trace_id,)
        ).fetchall()
    spans_out = []
    for s in spans:
        d = _row_to_dict(s)
        d["input"] = _decode_json(d.pop("input_json"))
        d["output"] = _decode_json(d.pop("output_json"))
        d["attrs"] = _decode_json(d.pop("attrs_json"))
        if d.get("ended_at") and d.get("started_at"):
            d["duration_ms"] = round((d["ended_at"] - d["started_at"]) * 1000, 2)
        else:
            d["duration_ms"] = None
        spans_out.append(d)
    return {
        "trace": _row_to_dict(trace),
        "spans": spans_out,
        "tags": {r["key"]: r["value"] for r in tag_rows},
    }


@app.get("/api/failure-graph")
async def api_failure_graph():
    """Aggregate per-span-name stats + step-transition counts across all traces.

    The dashboard renders this as: (1) a per-span failure-rate bar chart and
    (2) a call-graph showing transitions between span names, edges weighted by
    transition count and nodes colored by failure rate. Sankey would force a
    DAG; agents loop, so a graph is more honest.
    """
    with storage.connect() as conn:
        # per-span stats
        stat_rows = conn.execute(
            """SELECT name,
                      COUNT(*)                                  AS total,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)  AS errors,
                      AVG(CASE WHEN ended_at IS NOT NULL THEN (ended_at - started_at) * 1000 END) AS avg_ms
               FROM spans
               GROUP BY name
               ORDER BY total DESC
               LIMIT 40"""
        ).fetchall()
        # transitions: ordered spans within each trace
        span_rows = conn.execute(
            """SELECT trace_id, name, status, started_at, parent_id
               FROM spans
               ORDER BY trace_id, started_at"""
        ).fetchall()
        # which span fails when its trace fails
        failed_trace_ids = [
            r["id"]
            for r in conn.execute("SELECT id FROM traces WHERE status='error'").fetchall()
        ]

    by_trace: dict[str, list[dict]] = defaultdict(list)
    for r in span_rows:
        by_trace[r["trace_id"]].append(
            {"name": r["name"], "status": r["status"], "parent_id": r["parent_id"]}
        )

    transitions: Counter = Counter()
    for spans in by_trace.values():
        for i in range(len(spans) - 1):
            transitions[(spans[i]["name"], spans[i + 1]["name"])] += 1

    fail_step_index: Counter = Counter()
    failure_node: Counter = Counter()
    for tid in failed_trace_ids:
        # exclude the trace-root span — it always errors with its child, double-counting
        child_spans = [s for s in by_trace.get(tid, []) if s["parent_id"] is not None]
        for i, s in enumerate(child_spans):
            if s["status"] == "error":
                fail_step_index[i] += 1
                failure_node[s["name"]] += 1
                break

    nodes = []
    for r in stat_rows:
        total = r["total"]
        errors = r["errors"] or 0
        nodes.append(
            {
                "name": r["name"],
                "total": total,
                "errors": errors,
                "error_rate": (errors / total) if total else 0.0,
                "avg_ms": round(r["avg_ms"], 2) if r["avg_ms"] is not None else None,
                "failed_traces": failure_node.get(r["name"], 0),
            }
        )
    edges = [
        {"source": a, "target": b, "count": c}
        for (a, b), c in transitions.most_common(120)
    ]
    fail_steps = [{"step": k, "count": v} for k, v in sorted(fail_step_index.items())]
    return {"nodes": nodes, "edges": edges, "fail_steps": fail_steps}


# ---------------------------------------------------------------------------
# OTLP/JSON ingestion
# ---------------------------------------------------------------------------

# CORS headers exposed only on /v1/traces — keeps the rest of the dashboard
# same-origin while permitting browser-based agents to POST spans.
_OTLP_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}


def _attrs_to_dict(otlp_attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Convert OTLP/JSON KeyValue list into a flat python dict.

    OTLP encodes attribute values inside an AnyValue object with one of:
    stringValue, intValue, doubleValue, boolValue, arrayValue.
    intValue is encoded as a string in protobuf-JSON (int64 fits no JS number).
    """
    out: dict[str, Any] = {}
    if not otlp_attrs:
        return out
    for kv in otlp_attrs:
        key = kv.get("key")
        if not key:
            continue
        v = kv.get("value") or {}
        if "stringValue" in v:
            out[key] = v["stringValue"]
        elif "intValue" in v:
            raw = v["intValue"]
            try:
                out[key] = int(raw)
            except (TypeError, ValueError):
                out[key] = raw
        elif "doubleValue" in v:
            out[key] = float(v["doubleValue"])
        elif "boolValue" in v:
            out[key] = bool(v["boolValue"])
        elif "arrayValue" in v:
            arr = (v["arrayValue"] or {}).get("values", []) or []
            out[key] = [_attrs_to_dict([{"key": "_", "value": item}]).get("_") for item in arr]
    return out


def _ns_from(raw: Any) -> int:
    """OTLP timestamps are strings (int64) in protobuf-JSON. Be permissive."""
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


class _OtlpSpanAdapter:
    """Adapts an OTLP/JSON span dict to the duck-typed shape `_export_one` reads.

    `ClustertraceSpanExporter._export_one` expects an object with:
      .get_span_context() -> obj with .trace_id (int) and .span_id (int)
      .parent             -> None OR obj with .span_id (int)
      .start_time         -> int (ns)
      .end_time           -> int (ns) or None
      .status             -> obj with .status_code.name in {"OK","ERROR","UNSET"}
      .name               -> str
      .attributes         -> dict
      .kind               -> obj with .name
      .events             -> list of objs with .name and .attributes
    """

    class _Ctx:
        __slots__ = ("trace_id", "span_id")

        def __init__(self, trace_id: int, span_id: int) -> None:
            self.trace_id = trace_id
            self.span_id = span_id

    class _StatusCode:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    class _Status:
        __slots__ = ("status_code",)

        def __init__(self, name: str) -> None:
            self.status_code = _OtlpSpanAdapter._StatusCode(name)

    class _Kind:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    class _Event:
        __slots__ = ("name", "attributes")

        def __init__(self, name: str, attributes: dict[str, Any]) -> None:
            self.name = name
            self.attributes = attributes

    # OTLP SpanKind enum → string. Default to INTERNAL for unknown.
    _KIND_NAMES = {
        0: "INTERNAL",  # SPAN_KIND_UNSPECIFIED
        1: "INTERNAL",
        2: "SERVER",
        3: "CLIENT",
        4: "PRODUCER",
        5: "CONSUMER",
    }
    # OTLP StatusCode: 0=UNSET, 1=OK, 2=ERROR
    _STATUS_NAMES = {0: "UNSET", 1: "OK", 2: "ERROR"}

    def __init__(self, span_dict: dict[str, Any]) -> None:
        self._raw = span_dict
        trace_id_hex = span_dict.get("traceId") or ""
        span_id_hex = span_dict.get("spanId") or ""
        parent_id_hex = span_dict.get("parentSpanId") or ""
        if not trace_id_hex or not span_id_hex:
            raise ValueError("span missing traceId or spanId")
        # int(...,16) raises on invalid hex — caller catches and skips this span.
        self._trace_id_int = int(trace_id_hex, 16)
        self._span_id_int = int(span_id_hex, 16)
        self._parent_span_id_int = int(parent_id_hex, 16) if parent_id_hex else None

        self.name = span_dict.get("name") or "otel.span"
        self.start_time = _ns_from(span_dict.get("startTimeUnixNano"))
        end_ns = _ns_from(span_dict.get("endTimeUnixNano"))
        self.end_time = end_ns if end_ns > 0 else None
        self.attributes = _attrs_to_dict(span_dict.get("attributes"))

        status_obj = span_dict.get("status") or {}
        status_code = status_obj.get("code", 0)
        self.status = self._Status(self._STATUS_NAMES.get(status_code, "UNSET"))

        kind_code = span_dict.get("kind", 1)
        self.kind = self._Kind(self._KIND_NAMES.get(kind_code, "INTERNAL"))

        self.events = [
            self._Event(
                name=(e.get("name") or ""),
                attributes=_attrs_to_dict(e.get("attributes")),
            )
            for e in (span_dict.get("events") or [])
        ]

    def get_span_context(self) -> Any:
        return self._Ctx(self._trace_id_int, self._span_id_int)

    @property
    def parent(self) -> Any:
        if self._parent_span_id_int is None:
            return None
        # _export_one only reads parent.span_id
        return self._Ctx(self._trace_id_int, self._parent_span_id_int)


def _iter_otlp_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the resourceSpans → scopeSpans → spans tree and return a flat list.

    Tolerates the older `instrumentationLibrarySpans` field for compatibility
    with OTel JS exporters that haven't migrated yet.
    """
    out: list[dict[str, Any]] = []
    for rs in (payload.get("resourceSpans") or []):
        scope_lists = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
        for ss in scope_lists:
            for span in (ss.get("spans") or []):
                out.append(span)
    return out


@app.options("/v1/traces")
async def options_traces() -> Response:
    """CORS preflight for browser-based OTel exporters."""
    return Response(status_code=204, headers=_OTLP_CORS_HEADERS)


_DEFAULT_OTLP_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB


def _otlp_max_bytes() -> int:
    """Body cap for /v1/traces. Read per-request so tests can monkeypatch."""
    import os

    raw = os.environ.get("CLUSTERTRACE_OTLP_MAX_BYTES")
    if not raw:
        return _DEFAULT_OTLP_MAX_BYTES
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_OTLP_MAX_BYTES
    return n if n > 0 else _DEFAULT_OTLP_MAX_BYTES


@app.post("/v1/traces")
async def ingest_traces(request: Request) -> JSONResponse:
    """OTLP/JSON span ingestion.

    Accepts an `ExportTraceServiceRequest` body, walks every span, and
    delegates each one to `ClustertraceSpanExporter._export_one`. Returns
    an empty `ExportTracePartialSuccess` body (the OTLP success shape).

    Body size is capped at `CLUSTERTRACE_OTLP_MAX_BYTES` (default 16 MiB) so
    a multi-GB POST cannot OOM the dashboard process. We honor a declared
    Content-Length up-front, then re-check after the body lands in case a
    chunked client omitted/lied about it.
    """
    max_bytes = _otlp_max_bytes()

    # Fast path: if the client advertised Content-Length, reject before
    # buffering. The bound is also an integer-overflow guard.
    cl_header = request.headers.get("content-length")
    if cl_header is not None:
        try:
            declared = int(cl_header)
        except ValueError:
            declared = -1
        if declared > max_bytes:
            return JSONResponse(
                {"error": f"body exceeds {max_bytes} bytes"},
                status_code=413,
                headers=_OTLP_CORS_HEADERS,
            )

    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        return JSONResponse(
            {"error": f"body exceeds {max_bytes} bytes"},
            status_code=413,
            headers=_OTLP_CORS_HEADERS,
        )

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        return JSONResponse(
            {"error": "invalid JSON body"},
            status_code=400,
            headers=_OTLP_CORS_HEADERS,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "expected OTLP ExportTraceServiceRequest object"},
            status_code=400,
            headers=_OTLP_CORS_HEADERS,
        )

    exporter = ClustertraceSpanExporter()
    rejected = 0
    for span_dict in _iter_otlp_spans(payload):
        try:
            adapter = _OtlpSpanAdapter(span_dict)
        except Exception:
            rejected += 1
            continue
        try:
            exporter._export_one(adapter)
        except Exception:
            # Match the exporter's batch semantics: one bad span never aborts the batch.
            rejected += 1

    body: dict[str, Any] = {"partialSuccess": {}}
    if rejected:
        body["partialSuccess"] = {
            "rejectedSpans": str(rejected),
            "errorMessage": f"{rejected} span(s) were malformed and skipped",
        }
    return JSONResponse(body, status_code=200, headers=_OTLP_CORS_HEADERS)


@app.exception_handler(HTTPException)
async def http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
