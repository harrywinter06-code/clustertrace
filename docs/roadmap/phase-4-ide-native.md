# Phase 4 — IDE-native (MCP + terminal UI)

> **Self-contained brief.** Depends on Phase 1 (cluster API stability). Can be developed in parallel with Phase 3.

## What this is

Two surfaces that bring clustertrace into the developer's editor:

1. **`clustertrace-mcp` — a Model Context Protocol server** that exposes traces, clusters, and replay as tools to Claude Code, Cursor, Codex, Continue, and any other MCP-capable AI editor. Now "show me a failing trace of this pattern" or "diff this trace against a successful one" is a single AI assistant command.
2. **`clustertrace inspect <trace_id>` — a terminal UI** that renders a single trace as a rich Gantt + I/O viewer in the terminal. Good for SSH'd-in debugging where launching a browser isn't an option.

## Why it exists

Braintrust is winning a slice of the eval-tool market specifically because it has an MCP server that Cursor/Claude Code can query. IDE-native is the 2026 trend in dev tools — AI assistants are increasingly the primary tracing interface for power users. clustertrace already has the data model; exposing it via MCP is high leverage.

The terminal UI is the other side of the same coin — debugging on a server without a browser. `clustertrace inspect` should be as good as `lazygit` or `htop`.

## Pre-flight checks

1. **Read the MCP spec.** https://modelcontextprotocol.io/specification — focus on the "tools" surface; we don't need "resources" or "prompts" in v0.9
2. **Familiarize with the Python MCP SDK**: `pip install mcp` (the official one); list the existing community examples
3. **Read clustertrace's dashboard endpoints** — the MCP tools mostly mirror these
4. **Pick a terminal UI library**: `textual` (heavy, beautiful), `rich` (light, formatted output), or `urwid` (heavy, mature). Recommend `rich` for speed — `inspect` is not a long-lived TUI, it's a one-shot rendering.

## What ships (binary, all must pass)

### MCP server

- [ ] New package `src/clustertrace/mcp_server/` containing:
  - `__init__.py` — `main()` entrypoint
  - `server.py` — the MCP server with the tools
- [ ] New CLI command: `clustertrace mcp` starts the MCP server (stdio mode by default; `--http <port>` for HTTP)
- [ ] Tools exposed (in MCP terminology — these become callable by the AI assistant):
  - `list_clusters(limit=20, mode="ordered")` — returns the same shape as `/api/clusters`
  - `get_trace(trace_id)` — returns the full trace + spans (JSON)
  - `search(query, limit=20)` — FTS5 search
  - `failure_summary(group_by_tag="agent")` — current failure-summary endpoint
  - `compare_traces(a_trace_id, b_trace_id)` — NEW: returns a structured diff of spans between two traces (most useful when paired with cluster_drift output)
  - `recent_failed(limit=10)` — recent traces with status=error
- [ ] Tools follow MCP's argument schema convention (JSON schemas in the tool spec)
- [ ] Returns are structured JSON, not pretty-printed strings — the AI assistant formats for the human
- [ ] Optional install extra: `clustertrace[mcp]` with `mcp>=1.0`
- [ ] `mcp.json` configuration snippet for Claude Code in the README under "Use with AI editors"

### MCP install / setup

- [ ] `clustertrace mcp install --target claude-code` writes the right `mcp_servers` JSON entry to the user's Claude Code config
- [ ] Same for `--target cursor`, `--target continue`
- [ ] Without a target: prints the JSON snippet for the user to paste manually

### Terminal UI: `clustertrace inspect`

- [ ] CLI: `clustertrace inspect <trace_id>` renders to stdout (uses `rich`)
- [ ] Output shape:
  - Header: trace id, name, status, duration, cost, tags
  - Gantt chart (ASCII) of spans with proportional widths
  - For each span: indented tree showing parent-child relationships, with status icons (✓ for ok, ✗ for error, ◌ for running)
  - On status=error spans: full error_type + message
  - Optional `--expand <span_id>` shows that span's input/output JSON pretty-printed
- [ ] `--latest` flag picks the most recent trace if no ID given
- [ ] `--failed` filters to only show failed-trace IDs (paginated stub of recent_failed)
- [ ] Renders cleanly on a 80-column terminal AND a 200-column wide one

### Tests

- [ ] `tests/test_mcp_server.py` — fakes the MCP runtime and verifies each tool returns the expected shape
- [ ] `tests/test_inspect_cli.py` — runs `clustertrace inspect <id>` against a seeded trace and asserts the output contains expected substrings (using `capsys`)

## Hard rules

- **MCP is an optional install.** Adding it to `[mcp]` extra means `pip install clustertrace` doesn't pull in the `mcp` SDK. Without it, `clustertrace mcp` prints `pip install "clustertrace[mcp]"` and exits 2.
- **No mutation tools in v0.9.** The MCP tools are READ-ONLY. The AI assistant can list/search/get but not annotate or assert. Mutation tools land in v1.0 after we see how the read-only surface gets used.
- **`inspect` must work without a network connection.** Pure-local SQLite reads + terminal output.

## Tech stack

- `mcp>=1.0` (Python SDK from Anthropic) — optional extra
- `rich>=13` for the terminal UI — could be a hard dep since it's tiny and useful elsewhere; recommend hard dep
- No other new deps

## Decision boundaries

**Decide and commit:**
- HTTP transport for MCP yes/no in v0.9 (recommend: no — stdio only is sufficient for Claude Code / Cursor)
- Whether `compare_traces` returns a structured diff or a textual one (recommend: structured — let the AI format it)
- Exact terminal UI layout (it's CLI; iterate based on how it looks)

**Stop and write `BLOCKED.md` if:**
- The MCP SDK isn't available on PyPI yet (use an alternative; flag)
- Claude Code's MCP config schema changes between when you start and finish (read once, fix all references)

## What does NOT ship in this phase

- A full TUI dashboard (one-screen `inspect` is enough)
- Voice / mobile interfaces
- MCP "resources" or "prompts" (just tools)
- The `clustertrace.compare_traces` API outside MCP (could come later)
- Auto-installing MCP into the user's editor config without `mcp install`

## Time budget

8–12 hours wall clock.

## Operational rules

- Commit per surface (MCP server, MCP install, inspect TUI)
- Append to `STATUS.md` per commit
- Bump version to `0.9.0`
- Tag `v0.9.0`

## References

- MCP specification: https://modelcontextprotocol.io/specification
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- `rich` library: https://rich.readthedocs.io
- Existing dashboard endpoints (which MCP mirrors): [`src/clustertrace/dashboard/app.py`](../../src/clustertrace/dashboard/app.py)

## Definition of done

```bash
pip install "clustertrace[mcp]"
clustertrace mcp install --target claude-code  # configures the local MCP server
# In Claude Code, now ask: "show me failed traces in clustertrace"
# AI assistant calls list_clusters / recent_failed / get_trace
clustertrace inspect --latest    # rich terminal Gantt of most recent trace
clustertrace inspect --failed    # picks a recent failure
pytest -q                         # green
```
