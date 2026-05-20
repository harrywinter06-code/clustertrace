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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agentlog import storage

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
async def api_traces(limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 500))
    with storage.connect() as conn:
        rows = conn.execute(
            """SELECT id, name, started_at, ended_at, status, error_type, error_message
               FROM traces
               ORDER BY started_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d.get("ended_at") and d.get("started_at"):
            d["duration_ms"] = round((d["ended_at"] - d["started_at"]) * 1000, 1)
        else:
            d["duration_ms"] = None
        out.append(d)
    return {"traces": out, "limit": limit, "offset": offset}


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
    return {"trace": _row_to_dict(trace), "spans": spans_out}


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
            """SELECT trace_id, name, status, started_at
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
        by_trace[r["trace_id"]].append({"name": r["name"], "status": r["status"]})

    transitions: Counter = Counter()
    for spans in by_trace.values():
        for i in range(len(spans) - 1):
            transitions[(spans[i]["name"], spans[i + 1]["name"])] += 1

    fail_step_index: Counter = Counter()
    failure_node: Counter = Counter()
    for tid in failed_trace_ids:
        spans = by_trace.get(tid, [])
        for i, s in enumerate(spans):
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
