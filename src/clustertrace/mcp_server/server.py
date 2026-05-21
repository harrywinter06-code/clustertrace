"""MCP server implementation.

The server exposes six read-only tools that mirror the dashboard's JSON
endpoints. The MCP framing is a thin wrapper around six pure-Python functions
(`tool_*`); the same functions are called directly by the test suite without
spinning up an MCP runtime.

Read-only by design. v0.9 has no mutation tools — list/search/get is enough
to validate the surface before we expose annotate/assert.
"""
from __future__ import annotations

import json
from typing import Any

from clustertrace import cluster, drift, maintenance, storage

# ---------------------------------------------------------------------------
# Pure-Python tool implementations. Each returns a JSON-serializable dict.
# ---------------------------------------------------------------------------


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def tool_list_clusters(
    limit: int = 20,
    mode: str = "ordered",
    threshold: int | None = None,
) -> dict[str, Any]:
    """Return execution clusters, one row per distinct signature.

    Mirrors `GET /api/clusters`. The AI assistant uses this to enumerate the
    distinct execution patterns and spot the ones with high error rates.
    """
    limit = max(1, min(int(limit), 200))
    if mode not in ("ordered", "set", "tree_edit"):
        mode = "ordered"
    try:
        cluster.backfill_signatures()
    except Exception:
        pass
    rows = cluster.list_clusters(limit=limit, mode=mode, threshold=threshold)
    return {
        "mode": mode,
        "limit": limit,
        "clusters": [
            {
                "signature": c.signature,
                "sig_hash": c.sig_hash,
                "count": c.count,
                "errors": c.error_count,
                "error_rate": c.error_rate,
                "avg_duration_ms": c.avg_duration_ms,
                "representative_trace_id": c.representative_trace_id,
                "pattern": [{"name": n, "status": s} for n, s in c.pattern],
            }
            for c in rows
        ],
    }


def tool_get_trace(trace_id: str) -> dict[str, Any]:
    """Return one trace's full record + spans + tags.

    Mirrors `GET /api/trace/{trace_id}`. Returns `{"error": ...}` on miss
    rather than raising so the AI assistant gets a structured response.
    """
    if not trace_id or not isinstance(trace_id, str):
        return {"error": "trace_id is required"}
    with storage.connect() as conn:
        trace = conn.execute(
            "SELECT * FROM traces WHERE id = ?", (trace_id,)
        ).fetchone()
        if trace is None:
            return {"error": "trace not found", "trace_id": trace_id}
        spans = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at ASC",
            (trace_id,),
        ).fetchall()
        tag_rows = conn.execute(
            "SELECT key, value FROM trace_tags WHERE trace_id = ?", (trace_id,)
        ).fetchall()
    spans_out = []
    for s in spans:
        d = _row_to_dict(s)
        d["input"] = _decode_json(d.pop("input_json", None))
        d["output"] = _decode_json(d.pop("output_json", None))
        d["attrs"] = _decode_json(d.pop("attrs_json", None))
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


def tool_search(query: str, limit: int = 20) -> dict[str, Any]:
    """FTS5 full-text search over span name + input + output + error_message.

    Mirrors `GET /api/search`. Returns zero results (not an error) on parse
    failure so the AI assistant can recover gracefully from bad input.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return {"results": [], "query": query}
    limit = max(1, min(int(limit), 200))
    fts_query = query.strip()
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
            return {"results": [], "query": query, "error": "search parse error"}
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
        "query": query,
    }


def tool_failure_summary(group_by_tag: str = "agent") -> dict[str, Any]:
    """Aggregated failure-pattern view across all clusters.

    Mirrors `GET /api/failure-summary`.
    """
    try:
        cluster.backfill_signatures()
    except Exception:
        pass
    return cluster.failure_summary(group_by_tag=group_by_tag or None)


def tool_recent_failed(limit: int = 10) -> dict[str, Any]:
    """Return the N most recent trace IDs with status='error'.

    Newly added (not a dashboard endpoint mirror) so an AI assistant can
    answer "show me the latest failure" with one tool call instead of
    list-then-filter.
    """
    limit = max(1, min(int(limit), 200))
    with storage.connect() as conn:
        rows = conn.execute(
            """SELECT id, name, started_at, ended_at, status,
                      error_type, error_message, signature, cost_usd
               FROM traces
               WHERE status = 'error'
               ORDER BY started_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d.get("ended_at") and d.get("started_at"):
            d["duration_ms"] = round((d["ended_at"] - d["started_at"]) * 1000, 1)
        else:
            d["duration_ms"] = None
        out.append(d)
    return {"traces": out, "limit": limit}


def tool_compare_traces(a_trace_id: str, b_trace_id: str) -> dict[str, Any]:
    """Structured diff of spans between two traces.

    Computes a Wagner-Fischer edit script (insertions/deletions/equals) over
    the span sequence keyed by `(normalized_name, status)`. The diff is
    structured (per-step) — the AI assistant formats it for the human.

    Returns:
      a: {trace_id, status, signature, pattern[]}
      b: {trace_id, status, signature, pattern[]}
      diff: ordered list of {op: "equal"|"insert"|"delete"|"replace",
                              a: {name,status}|None,
                              b: {name,status}|None}
      summary: {edit_distance, a_only_count, b_only_count, equal_count}
    """
    if not a_trace_id or not b_trace_id:
        return {"error": "a_trace_id and b_trace_id are required"}

    def _load(tid: str) -> dict[str, Any] | None:
        with storage.connect() as conn:
            trace = conn.execute(
                "SELECT id, status, signature FROM traces WHERE id = ?", (tid,)
            ).fetchone()
            if trace is None:
                return None
            spans = conn.execute(
                """SELECT name, status, parent_id, started_at
                   FROM spans WHERE trace_id = ?
                   ORDER BY started_at ASC""",
                (tid,),
            ).fetchall()
        # We use the same `signature_for_spans(mode='tree_edit')` token format
        # so the diff aligns with how the cluster system thinks about traces.
        sig_full = cluster.signature_for_spans(
            [dict(s) for s in spans], mode="tree_edit"
        )
        tokens = cluster._tokenize_signature(sig_full)
        return {
            "trace_id": trace["id"],
            "status": trace["status"],
            "signature": trace["signature"],
            "tokens": tokens,
            "pattern": [{"name": n, "status": s} for n, s in cluster._decode_pattern(sig_full)],
        }

    a = _load(a_trace_id)
    b = _load(b_trace_id)
    if a is None:
        return {"error": "a_trace_id not found", "trace_id": a_trace_id}
    if b is None:
        return {"error": "b_trace_id not found", "trace_id": b_trace_id}

    diff_ops = _diff_token_sequences(a["tokens"], b["tokens"])
    a_only = sum(1 for op in diff_ops if op["op"] == "delete")
    b_only = sum(1 for op in diff_ops if op["op"] == "insert")
    equal = sum(1 for op in diff_ops if op["op"] == "equal")
    replaces = sum(1 for op in diff_ops if op["op"] == "replace")
    return {
        "a": {k: a[k] for k in ("trace_id", "status", "signature", "pattern")},
        "b": {k: b[k] for k in ("trace_id", "status", "signature", "pattern")},
        "diff": diff_ops,
        "summary": {
            "edit_distance": a_only + b_only + replaces,
            "a_only_count": a_only,
            "b_only_count": b_only,
            "equal_count": equal,
            "replace_count": replaces,
        },
    }


def _token_to_pair(token: str) -> dict[str, str] | None:
    """Reverse the `name:status` encoding used by `signature_for_spans`."""
    if not token:
        return None
    pattern = cluster._decode_pattern(token)
    if not pattern:
        return None
    name, status = pattern[0]
    return {"name": name, "status": status}


def _diff_token_sequences(a: list[str], b: list[str]) -> list[dict[str, Any]]:
    """Wagner-Fischer with traceback to produce an edit script.

    Output ops:
      {"op": "equal",   "a": pair, "b": pair}
      {"op": "insert",  "a": None, "b": pair}
      {"op": "delete",  "a": pair, "b": None}
      {"op": "replace", "a": pair, "b": pair}
    """
    n, m = len(a), len(b)
    # Build full distance matrix — these sequences are at most a few hundred
    # tokens long, so the n*m memory cost is negligible and the traceback is
    # straightforward.
    dp: list[list[int]] = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ai == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )

    ops: list[dict[str, Any]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1]:
            ops.append(
                {"op": "equal", "a": _token_to_pair(a[i - 1]), "b": _token_to_pair(b[j - 1])}
            )
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            ops.append(
                {"op": "replace", "a": _token_to_pair(a[i - 1]), "b": _token_to_pair(b[j - 1])}
            )
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            ops.append({"op": "delete", "a": _token_to_pair(a[i - 1]), "b": None})
            i -= 1
        elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            ops.append({"op": "insert", "a": None, "b": _token_to_pair(b[j - 1])})
            j -= 1
        else:  # pragma: no cover — defensive
            break
    ops.reverse()
    return ops


# ---------------------------------------------------------------------------
# Tool registry — names + JSON schemas + dispatch.
#
# Keeping the schemas in one place means both the MCP `list_tools` handler and
# the test suite can verify them; the registry is the single source of truth.
# ---------------------------------------------------------------------------


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_clusters",
        "description": (
            "List execution-pattern clusters across all stored traces. Each "
            "cluster groups traces with the same span sequence. Use this to "
            "find the dominant failure patterns. mode='ordered' is the fast "
            "default; 'set' ignores reorderings; 'tree_edit' uses edit-distance "
            "so one extra retry doesn't split a cluster."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                "mode": {
                    "type": "string",
                    "enum": ["ordered", "set", "tree_edit"],
                    "default": "ordered",
                },
                "threshold": {
                    "type": ["integer", "null"],
                    "default": None,
                    "description": "tree_edit mode only — override the auto distance threshold.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_list_clusters,
    },
    {
        "name": "get_trace",
        "description": (
            "Return one trace's full record: trace row, all spans (with I/O), "
            "and tags. Use this after list_clusters / recent_failed to drill "
            "into a representative trace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "Trace ID."},
            },
            "required": ["trace_id"],
            "additionalProperties": False,
        },
        "handler": tool_get_trace,
    },
    {
        "name": "search",
        "description": (
            "Full-text search over span name + input + output + error_message. "
            "Accepts SQLite FTS5 syntax (phrases in quotes, AND/OR, NEAR()). "
            "Returns matching spans with a snippet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "FTS5 query."},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": tool_search,
    },
    {
        "name": "failure_summary",
        "description": (
            "Aggregate failure-pattern view: overall failure rate, top failing "
            "clusters, common failure prefixes globally and per-tag (default "
            "tag is 'agent')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group_by_tag": {
                    "type": "string",
                    "default": "agent",
                    "description": "Tag key to compute per-group failure prefixes. Empty disables.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_failure_summary,
    },
    {
        "name": "compare_traces",
        "description": (
            "Structured diff of span sequences between two traces. Returns an "
            "edit script (equal/insert/delete/replace) keyed by (span_name, "
            "status). Most useful with one OK trace and one failing trace of "
            "the same agent, or with the two representative traces from a "
            "cluster_drift result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "a_trace_id": {"type": "string"},
                "b_trace_id": {"type": "string"},
            },
            "required": ["a_trace_id", "b_trace_id"],
            "additionalProperties": False,
        },
        "handler": tool_compare_traces,
    },
    {
        "name": "recent_failed",
        "description": (
            "Return the N most recent traces with status='error'. Use this to "
            "answer 'show me a failing trace' without having to list-then-filter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "handler": tool_recent_failed,
    },
]


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return the MCP-shaped tool list (no handlers — JSON-serializable)."""
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOLS
    ]


def dispatch_tool(name: str, arguments: dict[str, Any] | None) -> Any:
    """Call the handler for `name` with `arguments` (a dict, possibly empty).

    Raises KeyError on unknown tool name. Validation of the argument shape is
    delegated to the handler signature; MCP's `Server.call_tool(validate_input=True)`
    will have already validated against the inputSchema in the live server.
    """
    arguments = arguments or {}
    for t in TOOLS:
        if t["name"] == name:
            return t["handler"](**arguments)
    raise KeyError(f"unknown tool: {name}")


# ---------------------------------------------------------------------------
# MCP runtime glue — only loaded when the SDK is present.
# ---------------------------------------------------------------------------


def build_server() -> Any:
    """Construct an `mcp.server.Server` with all tools registered.

    Imported lazily so the rest of this module is usable (and testable) even
    when the `mcp` SDK isn't installed.
    """
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    from clustertrace import __version__

    server: Server = Server("clustertrace", version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOLS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = dispatch_tool(name, arguments)
        except KeyError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except Exception as e:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"{type(e).__name__}: {e}"}),
                )
            ]
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def run_stdio() -> int:
    """Block on the MCP stdio transport until the client disconnects."""
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server()
    init_opts = server.create_initialization_options()

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_opts)

    anyio.run(_serve)
    return 0


# Quiet a linter warning — `maintenance` and `drift` are imported for the
# side-effect of warming the module graph when this module is loaded by the
# CLI; the tools above only call `cluster` and `storage` directly. Keep the
# imports so future tools (e.g. drift exposure) don't need to re-add them.
_ = (drift, maintenance)
