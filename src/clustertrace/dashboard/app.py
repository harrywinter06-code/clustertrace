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


# ---------------------------------------------------------------------------
# Idle-shutdown: when invoked under the on-demand hook flow, the dashboard
# exits if no request hits it for `CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS`. Keeps
# RAM/CPU at zero between Claude Code sessions. Disabled (0) by default so
# manual `clustertrace dashboard` invocations don't get rugged.
# ---------------------------------------------------------------------------

import os as _os  # noqa: E402  (kept local to keep top imports clean)
import time as _time  # noqa: E402

_IDLE_SHUTDOWN_SECONDS = int(_os.environ.get("CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS", "0") or "0")
_last_activity_ts = _time.time()


@app.middleware("http")
async def _bump_activity(request, call_next):
    """Every HTTP hit resets the idle timer. Browser tab polling and OTLP
    POSTs both count — so 'someone is using clustertrace' keeps it alive."""
    global _last_activity_ts
    _last_activity_ts = _time.time()
    return await call_next(request)


@app.on_event("startup")
async def _start_idle_watcher():
    """Background task that checks the idle window every 60s and self-exits
    when it's been quiet too long. No-op when CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS
    is 0 or unset (the default for manual `clustertrace dashboard` use)."""
    if _IDLE_SHUTDOWN_SECONDS <= 0:
        return

    import asyncio as _asyncio
    import logging as _logging

    log = _logging.getLogger("clustertrace.idle")

    async def _watcher():
        while True:
            await _asyncio.sleep(60)
            idle = _time.time() - _last_activity_ts
            if idle >= _IDLE_SHUTDOWN_SECONDS:
                log.info(
                    "idle for %.0fs (>= %ds), shutting down", idle, _IDLE_SHUTDOWN_SECONDS
                )
                # os._exit so we bypass uvicorn's graceful shutdown which
                # can hang on a pool that the storage layer keeps warm.
                _os._exit(0)

    _asyncio.create_task(_watcher())


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
        # Pull a user-prompt-derived title so the page H1 reads
        # "Refactor src/foo.py to use the new API" instead of
        # "claude_code.interaction" for every Claude Code trace.
        up_row = conn.execute(
            "SELECT json_extract(attrs_json, '$.user_prompt') AS up "
            "FROM spans WHERE trace_id = ? "
            "  AND json_extract(attrs_json, '$.user_prompt') IS NOT NULL "
            "ORDER BY started_at ASC LIMIT 1",
            (trace_id,),
        ).fetchone()
    user_prompt = up_row["up"] if up_row else None
    display_name = _smart_title(trace["name"], user_prompt)
    return _TEMPLATES.TemplateResponse(
        request,
        "trace.html",
        {"trace_id": trace_id, "trace_name": display_name},
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


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "review.html", {})


@app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "prompts.html", {})


# ---------------------------------------------------------------------------
# Prompt-engineering help — analysis + templates + on-demand LLM
# ---------------------------------------------------------------------------


_DEAD_END_STOP_REASONS = ("max_tokens", "refusal", "pause_turn")


def _extract_prompt_from_span_row(
    kind: str, input_json: str | None, attrs_json: str | None
) -> str | None:
    """Pure: given one span row's columns, return the user prompt text if any.

    Sources, in priority order:
      1. `user_prompt` attribute (Claude Code OTel emits this on
         `claude_code.interaction` when OTEL_LOG_USER_PROMPTS=1).
      2. The last `role:"user"` message inside an `llm_call` span's
         `input_json` (the Anthropic / OpenAI SDK wrapper path).
    """
    if attrs_json:
        try:
            attrs = json.loads(attrs_json)
            if isinstance(attrs, dict) and isinstance(attrs.get("user_prompt"), str):
                p = attrs["user_prompt"]
                return p if p.strip() else None
        except (ValueError, TypeError):
            pass
    if kind != "llm_call" or not input_json:
        return None
    try:
        data = json.loads(input_json)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        msgs = data.get("messages")
        # Tighter check than `or`: empty messages means "no messages", not
        # "look at input next". Only fall through if the key isn't present.
        if not isinstance(msgs, list):
            msgs = data.get("input") if "input" in data else None
    elif isinstance(data, list):
        msgs = data
    else:
        msgs = None
    if not isinstance(msgs, list):
        return None
    for msg in reversed(msgs):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content if content.strip() else None
        if isinstance(content, list):
            text_blocks = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(text_blocks).strip()
            return joined or None
    return None


@app.get("/api/prompts/patterns")
async def api_prompts_patterns(
    window: str = Query("7d", pattern="^(7d|30d|all)$"),
    with_samples: bool = Query(False),
    sample_limit: int = Query(10, ge=1, le=30),
):
    """Compare prompts in succeeded vs dead-ended sessions over the window.

    Single SQL query joins traces + spans so we walk each row once rather
    than running 2N+1 lookups per trace. `with_samples=true` returns up to
    `sample_limit` truncated prompt examples in each bucket, used by the
    Deepen-with-Claude flow so the LLM gets actual prompts rather than just
    aggregated rates.
    """
    from clustertrace import prompt_help

    cutoff = _window_cutoff(window)
    sql = (
        "SELECT t.id AS trace_id, t.status AS trace_status, "
        "       s.kind AS span_kind, s.input_json, s.attrs_json, "
        "       json_extract(s.attrs_json, '$.stop_reason') AS stop_reason "
        "FROM traces t LEFT JOIN spans s ON s.trace_id = t.id"
    )
    args: tuple = ()
    if cutoff is not None:
        sql += " WHERE t.started_at >= ?"
        args = (cutoff,)
    sql += " ORDER BY t.id"

    with storage.connect() as c:
        rows = c.execute(sql, args).fetchall()

    # Walk rows grouped by trace_id. For each trace, collect:
    #   - the trace-level status
    #   - whether any span has a dead-end stop_reason
    #   - the set of prompt strings extracted from each span
    succeeded: list[str] = []
    dead_ended: list[str] = []

    current_id: str | None = None
    current_status: str | None = None
    current_dead: bool = False
    current_prompts: list[str] = []

    def _finalize() -> None:
        if current_id is None:
            return
        if not current_prompts:
            return
        # Skip status='running' or anything not ok/error.
        if current_status not in ("ok", "error"):
            return
        is_dead = current_dead or current_status == "error"
        bucket = dead_ended if is_dead else succeeded
        bucket.extend(current_prompts)

    for row in rows:
        tid = row["trace_id"]
        if tid != current_id:
            _finalize()
            current_id = tid
            current_status = row["trace_status"]
            current_dead = False
            current_prompts = []
        if row["stop_reason"] in _DEAD_END_STOP_REASONS:
            current_dead = True
        # The LEFT JOIN means a trace with zero spans yields one row with all
        # span columns NULL — skip the prompt extraction in that case.
        if row["span_kind"] is None:
            continue
        prompt = _extract_prompt_from_span_row(
            row["span_kind"], row["input_json"], row["attrs_json"]
        )
        if prompt is not None:
            current_prompts.append(prompt)
    _finalize()

    report = prompt_help.patterns_from_traces(succeeded, dead_ended)
    report["window"] = window

    if with_samples:
        # Truncate each sample to keep response (and downstream LLM context)
        # bounded; the LLM-deepen endpoint will also cap, but doing it here
        # means the JSON wire payload doesn't carry the whole corpus.
        report["sample_succeeded"] = [p[:600] for p in succeeded[:sample_limit]]
        report["sample_dead_ended"] = [p[:600] for p in dead_ended[:sample_limit]]
    return report


@app.post("/api/prompts/critique")
async def api_prompts_critique(request: Request):
    """Rule-based critique of a single pasted prompt."""
    from clustertrace import prompt_help

    payload = await request.json()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 10000:
        raise HTTPException(status_code=400, detail="text too long (max 10000 chars)")
    return prompt_help.analyse_prompt(text)


@app.get("/api/prompts/templates")
async def api_prompts_templates_list():
    """All saved templates, most-recently-updated first."""
    with storage.connect() as c:
        rows = c.execute(
            "SELECT id, created_at, updated_at, name, body, tags_json, use_count "
            "FROM prompt_templates ORDER BY updated_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        try:
            tags = json.loads(r["tags_json"]) if r["tags_json"] else []
        except (ValueError, TypeError):
            tags = []
        out.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "name": r["name"],
                "body": r["body"],
                "tags": tags,
                "use_count": int(r["use_count"]),
            }
        )
    return {"templates": out}


@app.post("/api/prompts/templates")
async def api_prompts_templates_create(request: Request):
    import time

    payload = await request.json()
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    tags = payload.get("tags") or []
    if not name or not body:
        raise HTTPException(status_code=400, detail="name and body are required")
    if len(name) > 200 or len(body) > 10000:
        raise HTTPException(status_code=400, detail="name<=200, body<=10000")
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list")
    now = time.time()
    with storage.connect() as c:
        cur = c.execute(
            "INSERT INTO prompt_templates (created_at, updated_at, name, body, tags_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, now, name, body, json.dumps(tags)),
        )
    return {"id": cur.lastrowid, "name": name, "body": body, "tags": tags, "use_count": 0}


@app.patch("/api/prompts/templates/{template_id}")
async def api_prompts_templates_update(template_id: int, request: Request):
    import time

    payload = await request.json()
    name = payload.get("name")
    body = payload.get("body")
    tags = payload.get("tags")
    with storage.connect() as c:
        existing = c.execute(
            "SELECT name, body, tags_json FROM prompt_templates WHERE id = ?",
            (template_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="template not found")
        new_name = (name or existing["name"]).strip()
        new_body = (body if body is not None else existing["body"]).strip()
        if not new_name or not new_body:
            raise HTTPException(status_code=400, detail="name and body cannot be empty")
        new_tags = (
            json.dumps(tags)
            if isinstance(tags, list)
            else existing["tags_json"] or "[]"
        )
        c.execute(
            "UPDATE prompt_templates SET name=?, body=?, tags_json=?, updated_at=? WHERE id=?",
            (new_name, new_body, new_tags, time.time(), template_id),
        )
    return {"id": template_id}


@app.delete("/api/prompts/templates/{template_id}")
async def api_prompts_templates_delete(template_id: int):
    with storage.connect() as c:
        cur = c.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="template not found")
    return {"deleted": template_id}


@app.post("/api/prompts/templates/{template_id}/use")
async def api_prompts_templates_use(template_id: int):
    """Increment use_count when the user copies a template. Best-effort."""
    with storage.connect() as c:
        c.execute(
            "UPDATE prompt_templates SET use_count = use_count + 1 WHERE id = ?",
            (template_id,),
        )
    return {"id": template_id}


# --- LLM deepen --------------------------------------------------------------

_DEEPEN_SYSTEM_PROMPTS = {
    "critique": (
        "You are a senior engineer doing a careful prompt review. "
        "Read the prompt below and give 3-5 specific, actionable improvements. "
        "Be direct. Refer to specific words / phrases in the prompt. "
        "If the prompt is already strong, say so and only flag what would push it from good to excellent."
    ),
    "patterns": (
        "You are analysing two corpora of prompts the user wrote: "
        "those that led to successful sessions, and those that dead-ended. "
        "Find the load-bearing differences. "
        "Report the 2-3 most-discriminating patterns, then one concrete change the user should commit to next week."
    ),
    "template": (
        "Review the prompt template below. "
        "Suggest 2-4 concrete improvements: missing parameters, clearer scoping, better placeholders, etc. "
        "Be brief — one short line per suggestion."
    ),
}


@app.post("/api/prompts/llm-deepen")
async def api_prompts_llm_deepen(request: Request):
    """Send the analysis input to Anthropic for a deeper take.

    Body: {"kind": "critique" | "patterns" | "template", "payload": <kind-specific>}.
    Requires ANTHROPIC_API_KEY in the environment. Returns 503 with a clear
    install / config hint when the key or the SDK are missing.
    """
    import os

    payload = await request.json()
    kind = payload.get("kind")
    if kind not in _DEEPEN_SYSTEM_PROMPTS:
        raise HTTPException(status_code=400, detail="kind must be critique|patterns|template")
    body = payload.get("payload")
    if not body:
        raise HTTPException(status_code=400, detail="payload required")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return JSONResponse(
            {
                "error": "ANTHROPIC_API_KEY not set",
                "hint": "set ANTHROPIC_API_KEY in the dashboard's environment; "
                "deepen-with-LLM uses your own key (Haiku by default to keep costs low).",
            },
            status_code=503,
        )

    try:
        from anthropic import Anthropic
    except ImportError:
        return JSONResponse(
            {
                "error": "anthropic SDK not installed",
                "hint": "pip install 'clustertrace[anthropic]'",
            },
            status_code=503,
        )

    # Format user content per kind.
    if kind == "critique":
        user_content = f"Prompt to review:\n\n```\n{body}\n```"
    elif kind == "patterns":
        # body should be a dict with two lists.
        succ = body.get("succeeded") or []
        dead = body.get("dead_ended") or []
        # Cap each example to avoid sending megabytes.
        succ_snippets = "\n---\n".join(s[:600] for s in succ[:8])
        dead_snippets = "\n---\n".join(s[:600] for s in dead[:8])
        user_content = (
            f"Prompts that succeeded (sample of {len(succ)}):\n\n{succ_snippets}\n\n"
            f"=====\n\nPrompts that dead-ended (sample of {len(dead)}):\n\n{dead_snippets}"
        )
    else:  # template
        user_content = f"Template to review:\n\n```\n{body}\n```"

    model = os.environ.get("CLUSTERTRACE_DEEPEN_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=900,
            system=_DEEPEN_SYSTEM_PROMPTS[kind],
            messages=[{"role": "user", "content": user_content}],
        )
        text_blocks = [
            getattr(b, "text", "")
            for b in resp.content
            if getattr(b, "type", None) == "text"
        ]
        text = "\n".join(text_blocks).strip()
        usage = getattr(resp, "usage", None)
        return {
            "text": text,
            "model": model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"LLM call failed: {type(e).__name__}: {e}"},
            status_code=502,
        )


# ---------------------------------------------------------------------------
# Weekly review API
# ---------------------------------------------------------------------------

_WINDOW_SECONDS = {
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "all": None,
}


def _window_cutoff(window: str) -> float | None:
    """Convert a window label into an `started_at` cutoff (epoch seconds)."""
    import time

    secs = _WINDOW_SECONDS.get(window)
    if secs is None:
        return None
    return time.time() - secs


@app.get("/api/review/summary")
async def api_review_summary(window: str = Query("7d", pattern="^(7d|30d|all)$")):
    """Headline numbers for the review header: session count, total cost, window."""
    cutoff = _window_cutoff(window)
    with storage.connect() as c:
        if cutoff is None:
            row = c.execute(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(cost_usd), 0) AS cost FROM traces"
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(cost_usd), 0) AS cost "
                "FROM traces WHERE started_at >= ?",
                (cutoff,),
            ).fetchone()
    return {
        "window": window,
        "sessions": int(row["sessions"]),
        "total_cost_usd": float(row["cost"]),
    }


@app.get("/api/review/expensive")
async def api_review_expensive(
    window: str = Query("7d", pattern="^(7d|30d|all)$"),
    limit: int = Query(5, ge=1, le=50),
):
    """Q1: top sessions by cost in the window."""
    cutoff = _window_cutoff(window)
    sql_where = "cost_usd IS NOT NULL"
    args: tuple = ()
    if cutoff is not None:
        sql_where = "started_at >= ? AND " + sql_where
        args = (cutoff,)
    sql = (
        "SELECT id, name, started_at, ended_at, status, cost_usd, signature, "
        f"       {_USER_PROMPT_SUBQUERY} AS user_prompt "
        f"FROM traces WHERE {sql_where} ORDER BY cost_usd DESC LIMIT ?"
    )
    args = args + (limit,)
    with storage.connect() as c:
        rows = c.execute(sql, args).fetchall()
    return {
        "window": window,
        "traces": [
            {
                "id": r["id"],
                "name": r["name"],
                "display_name": _smart_title(r["name"], r["user_prompt"]),
                "started_at": r["started_at"],
                "duration_ms": (
                    int((r["ended_at"] - r["started_at"]) * 1000)
                    if r["ended_at"] is not None
                    else None
                ),
                "status": r["status"],
                "cost_usd": float(r["cost_usd"] or 0.0),
                "signature": r["signature"],
            }
            for r in rows
        ],
    }


@app.get("/api/review/cache-rate")
async def api_review_cache_rate(
    window: str = Query("7d", pattern="^(7d|30d|all)$"),
    limit: int = Query(10, ge=1, le=50),
):
    """Q2: cache hit rate per pattern.

    rate = sum(cache_read_tokens) / sum(input_tokens + cache_read_tokens) across
    all llm_call spans of all traces sharing a signature in the window.
    Patterns with zero LLM-call tokens are omitted (no signal to read).
    """
    cutoff = _window_cutoff(window)
    sql_where = "s.kind = 'llm_call'"
    args: tuple = ()
    if cutoff is not None:
        sql_where += " AND t.started_at >= ?"
        args = (cutoff,)
    sql = (
        "SELECT t.signature AS signature, "
        "       COUNT(DISTINCT t.id) AS run_count, "
        "       COALESCE(SUM(CAST(json_extract(s.attrs_json, '$.cache_read_tokens') AS INTEGER)), 0) AS cache_read, "
        "       COALESCE(SUM(CAST(json_extract(s.attrs_json, '$.input_tokens') AS INTEGER)), 0) AS uncached_input "
        "FROM traces t JOIN spans s ON s.trace_id = t.id "
        f"WHERE {sql_where} "
        "GROUP BY t.signature "
        "HAVING (cache_read + uncached_input) > 0 "
        "ORDER BY run_count DESC LIMIT ?"
    )
    args = args + (limit,)
    with storage.connect() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        denom = (r["cache_read"] or 0) + (r["uncached_input"] or 0)
        rate = (r["cache_read"] or 0) / denom if denom > 0 else 0.0
        out.append(
            {
                "signature": r["signature"] or "(no signature)",
                "run_count": int(r["run_count"]),
                "cache_read_tokens": int(r["cache_read"] or 0),
                "input_tokens": int(r["uncached_input"] or 0),
                "cache_hit_rate": rate,
            }
        )
    return {"window": window, "patterns": out}


@app.get("/api/review/top-pattern")
async def api_review_top_pattern(window: str = Query("7d", pattern="^(7d|30d|all)$")):
    """Q3: most-frequent pattern in the window + a sample trace id."""
    cutoff = _window_cutoff(window)
    sql_where = "signature IS NOT NULL AND signature <> ''"
    args: tuple = ()
    if cutoff is not None:
        sql_where += " AND started_at >= ?"
        args = (cutoff,)
    sql = (
        "SELECT signature, COUNT(*) AS run_count, "
        "       (SELECT id FROM traces t2 WHERE t2.signature = t.signature "
        f"        AND t2.started_at >= COALESCE(?, 0) ORDER BY t2.started_at DESC LIMIT 1) AS sample_id "
        f"FROM traces t WHERE {sql_where} "
        "GROUP BY signature ORDER BY run_count DESC LIMIT 1"
    )
    args = (cutoff if cutoff is not None else 0,) + args
    with storage.connect() as c:
        row = c.execute(sql, args).fetchone()
    if row is None or not row["signature"]:
        return {"window": window, "pattern": None}
    return {
        "window": window,
        "pattern": {
            "signature": row["signature"],
            "run_count": int(row["run_count"]),
            "sample_trace_id": row["sample_id"],
        },
    }


@app.get("/api/review/dead-ends")
async def api_review_dead_ends(
    window: str = Query("7d", pattern="^(7d|30d|all)$"),
    limit: int = Query(10, ge=1, le=50),
):
    """Q4: sessions that hit max_tokens / refusal / pause_turn, or status=error."""
    cutoff = _window_cutoff(window)
    where = "(t.status = 'error' OR EXISTS (SELECT 1 FROM spans s2 WHERE s2.trace_id = t.id AND s2.kind = 'llm_call' AND json_extract(s2.attrs_json, '$.stop_reason') IN ('max_tokens', 'refusal', 'pause_turn')))"
    args: tuple = ()
    if cutoff is not None:
        where = "t.started_at >= ? AND " + where
        args = (cutoff,)
    sql = (
        "SELECT t.id, t.name, t.started_at, t.status, t.error_type, t.error_message, "
        "       (SELECT json_extract(s.attrs_json, '$.stop_reason') FROM spans s "
        "        WHERE s.trace_id = t.id AND s.kind = 'llm_call' "
        "        AND json_extract(s.attrs_json, '$.stop_reason') IN ('max_tokens', 'refusal', 'pause_turn') "
        "        LIMIT 1) AS stop_reason "
        f"FROM traces t WHERE {where} "
        "ORDER BY t.started_at DESC LIMIT ?"
    )
    args = args + (limit,)
    with storage.connect() as c:
        rows = c.execute(sql, args).fetchall()
    return {
        "window": window,
        "traces": [
            {
                "id": r["id"],
                "name": r["name"],
                "started_at": r["started_at"],
                "status": r["status"],
                "error_type": r["error_type"],
                "error_message": (r["error_message"] or "")[:200],
                "stop_reason": r["stop_reason"],
            }
            for r in rows
        ],
    }


@app.get("/api/review/commitments")
async def api_review_commitments_list(limit: int = Query(20, ge=1, le=200)):
    """Past commitments, newest first."""
    with storage.connect() as c:
        rows = c.execute(
            "SELECT id, created_at, window_label, text, outcome "
            "FROM review_commitments ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {
        "commitments": [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "window_label": r["window_label"],
                "text": r["text"],
                "outcome": r["outcome"],
            }
            for r in rows
        ]
    }


@app.post("/api/review/commitments")
async def api_review_commitments_add(request: Request):
    """Add a new commitment. Body: {"text": "...", "window": "7d"}."""
    import time

    payload = await request.json()
    text = (payload.get("text") or "").strip()
    window = payload.get("window") or "7d"
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long (max 2000 chars)")
    if window not in _WINDOW_SECONDS:
        window = "7d"
    with storage.connect() as c:
        cur = c.execute(
            "INSERT INTO review_commitments (created_at, window_label, text) VALUES (?, ?, ?)",
            (time.time(), window, text),
        )
        new_id = cur.lastrowid
    return {"id": new_id, "text": text, "window_label": window}


@app.post("/api/review/commitments/{commitment_id}/outcome")
async def api_review_commitment_outcome(commitment_id: int, request: Request):
    """Mark whether a prior commitment landed. Body: {"outcome": "kept" | "missed" | "partial"}."""
    payload = await request.json()
    outcome = (payload.get("outcome") or "").strip().lower()
    if outcome not in ("kept", "missed", "partial", ""):
        raise HTTPException(status_code=400, detail="outcome must be kept|missed|partial|''")
    with storage.connect() as c:
        cur = c.execute(
            "UPDATE review_commitments SET outcome = ? WHERE id = ?",
            (outcome or None, commitment_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="commitment not found")
    return {"id": commitment_id, "outcome": outcome or None}


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


def _smart_title(name: str | None, user_prompt: str | None, max_chars: int = 70) -> str:
    """Pick a readable title for a trace.

    Most Claude Code traces ship `claude_code.interaction` / `claude_code.llm_request`
    as the name, which collapses every row in the trace list into the same string.
    We prefer the first user prompt when present (Claude Code with
    `OTEL_LOG_USER_PROMPTS=1`, or the wrap_anthropic decorator), truncated to a
    word boundary, falling back to the span name otherwise.
    """
    p = (user_prompt or "").strip()
    if p:
        # Strip newlines + collapse whitespace so the title doesn't span lines.
        p = " ".join(p.split())
        if len(p) <= max_chars:
            return p
        # Cut at the last word boundary inside the budget.
        cut = p[:max_chars].rsplit(" ", 1)[0]
        return cut + "…" if cut else p[:max_chars] + "…"
    return name or "(unnamed)"


# SQL subquery returning the first user-prompt-shaped string we can find on a
# trace's spans. Used by /api/traces, /api/trace/{id}, and /api/review/expensive
# so they all surface the same readable title.
_USER_PROMPT_SUBQUERY = """
    (
      SELECT
        COALESCE(
          json_extract(s.attrs_json, '$.user_prompt'),
          -- Anthropic-SDK wrap path: input_json holds the messages array.
          -- We can't json_extract a nested array cleanly without overcomplicating
          -- the SQL, so this branch is left to a later iteration. The COALESCE
          -- still works because the column will be NULL on that path and
          -- _smart_title falls back to the trace name.
          NULL
        )
      FROM spans s
      WHERE s.trace_id = traces.id
        AND json_extract(s.attrs_json, '$.user_prompt') IS NOT NULL
      ORDER BY s.started_at ASC
      LIMIT 1
    )
"""


@app.get("/api/trend")
async def api_trend(days: int = Query(14, ge=1, le=90)):
    """Per-day rollups for the last N days. Powers the landing-page sparklines.

    Returns a complete `days`-long series (filling missing days with zeros)
    so the consumer doesn't have to worry about gaps. Day keys are ISO date
    strings in the SERVER's local timezone — fine for a single-user
    local-first tool; would need TZ-explicit handling for multi-user.
    """
    import time
    from datetime import datetime, timedelta

    cutoff = time.time() - days * 86400
    with storage.connect() as c:
        rows = c.execute(
            "SELECT date(started_at, 'unixepoch') AS day, "
            "       COUNT(*) AS sessions, "
            "       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
            "       COALESCE(SUM(cost_usd), 0) AS cost, "
            "       COUNT(DISTINCT signature) AS patterns "
            "FROM traces WHERE started_at >= ? "
            "GROUP BY day ORDER BY day",
            (cutoff,),
        ).fetchall()
    by_day = {r["day"]: r for r in rows}

    out_days: list[str] = []
    sessions: list[int] = []
    errors: list[int] = []
    cost: list[float] = []
    patterns: list[int] = []
    today = datetime.now(tz=datetime.now().astimezone().tzinfo).date()  # local TZ, matches SQLite's date() default
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        out_days.append(key)
        row = by_day.get(key)
        sessions.append(int(row["sessions"]) if row else 0)
        errors.append(int(row["errors"]) if row else 0)
        cost.append(float(row["cost"]) if row else 0.0)
        patterns.append(int(row["patterns"]) if row else 0)

    return {
        "days": out_days,
        "sessions": sessions,
        "errors": errors,
        "cost": cost,
        "patterns": patterns,
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
                     traces.signature, traces.cost_usd,
                     {_USER_PROMPT_SUBQUERY} AS user_prompt
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
        d["display_name"] = _smart_title(d.get("name"), d.pop("user_prompt", None))
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


def _decode_otlp_protobuf(raw_body: bytes) -> dict[str, Any]:
    """Decode an OTLP/HTTP/protobuf ExportTraceServiceRequest into the same
    dict shape OTLP/HTTP/JSON produces, so `_iter_otlp_spans` + `_OtlpSpanAdapter`
    can consume both paths without branching.

    Raises ImportError if `clustertrace[otel-import]` is not installed.
    Raises any protobuf decode error.

    The one subtlety: MessageToDict base64-encodes protobuf `bytes` fields
    (traceId, spanId, parentSpanId). OTLP/JSON encodes the same fields as
    hex, and `_OtlpSpanAdapter` parses them with `int(hex, 16)`. We rewrite
    each ID in place after the dict conversion.
    """
    import base64

    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(raw_body)
    payload = MessageToDict(req, preserving_proto_field_name=False)

    for rs in payload.get("resourceSpans") or []:
        for ss in rs.get("scopeSpans") or []:
            for span in ss.get("spans") or []:
                for k in ("traceId", "spanId", "parentSpanId"):
                    v = span.get(k)
                    if isinstance(v, str) and v:
                        try:
                            span[k] = base64.b64decode(v).hex()
                        except Exception:
                            # Leave as-is; _OtlpSpanAdapter will reject and the span
                            # gets counted in `rejected`.
                            pass
    return payload


@app.post("/v1/traces")
async def ingest_traces(request: Request) -> JSONResponse:
    """OTLP span ingestion (HTTP/JSON and HTTP/protobuf).

    Accepts an `ExportTraceServiceRequest` body, walks every span, and
    delegates each one to `ClustertraceSpanExporter._export_one`. Returns
    an empty `ExportTracePartialSuccess` body (the OTLP success shape).

    Protocol selection follows Content-Type. JSON is built-in; protobuf
    requires `pip install clustertrace[otel-import]` and returns 415 with a
    helpful message otherwise.

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

    content_type = (request.headers.get("content-type") or "").lower().split(";", 1)[0].strip()
    is_protobuf = content_type in ("application/x-protobuf", "application/protobuf")

    if is_protobuf:
        try:
            payload = _decode_otlp_protobuf(raw_body)
        except ImportError:
            return JSONResponse(
                {
                    "error": (
                        "protobuf body received but the protobuf decoder is not installed. "
                        "Install with `pip install clustertrace[otel-import]`, or set "
                        "OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the sender."
                    )
                },
                status_code=415,
                headers=_OTLP_CORS_HEADERS,
            )
        except Exception as e:
            return JSONResponse(
                {"error": f"protobuf decode failed: {e}"},
                status_code=400,
                headers=_OTLP_CORS_HEADERS,
            )
    else:
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
