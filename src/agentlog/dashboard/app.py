"""FastAPI dashboard for agentlog.

Routes:
  GET /                        recent traces list
  GET /trace/{trace_id}        single-trace timeline
  GET /failures                aggregate failure-pattern visualization
  GET /api/traces              JSON: recent traces
  GET /api/trace/{trace_id}    JSON: spans for one trace
  GET /api/failure-graph       JSON: nodes + transitions for the viz
  GET /api/stats               JSON: top-level counters
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agentlog import cluster, storage

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC_DIR = _HERE / "static"

app = FastAPI(title="agentlog", docs_url=None, redoc_url=None, openapi_url=None)
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


@app.get("/clusters", response_class=HTMLResponse)
async def clusters_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "clusters.html", {})


@app.get("/api/clusters")
async def api_clusters(limit: int = 50, backfill: bool = True):
    if backfill:
        try:
            cluster.backfill_signatures()
        except Exception:
            pass
    out = []
    for cl in cluster.list_clusters(limit=limit):
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
            }
        )
    return {"clusters": out}


@app.get("/api/failure-summary")
async def api_failure_summary():
    try:
        cluster.backfill_signatures()
    except Exception:
        pass
    return cluster.failure_summary()


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
        recent = conn.execute(
            """SELECT name, status, started_at FROM traces ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
    return {
        "traces_total": traces_total,
        "traces_error": traces_error,
        "traces_error_rate": (traces_error / traces_total) if traces_total else 0.0,
        "spans_total": spans_total,
        "spans_error": spans_error,
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
                     traces.status, traces.error_type, traces.error_message, traces.signature
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


@app.exception_handler(HTTPException)
async def http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
