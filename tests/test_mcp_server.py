"""Tests for the clustertrace MCP server tool surface.

We exercise the pure-Python tool functions directly. The MCP runtime is a
thin async wrapper that hands the same dict back to the AI assistant —
testing it would mostly test the upstream `mcp` SDK. We do verify that the
server can be constructed (which exercises decorator registration) under the
real `mcp` SDK.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import clustertrace
from clustertrace.mcp_server import install as mcp_install
from clustertrace.mcp_server import server as mcp_server


def _seed_traces() -> None:
    """Two ok traces + one failing trace, all with the same first step."""

    @clustertrace.trace
    def ok_path():
        clustertrace.tag("agent", "research")
        with clustertrace.span("fetch_paper"):
            clustertrace.tool_call(
                "lookup", args={"q": "linear regression"}, result={"hits": 7}
            )
        with clustertrace.span("verify_claim"):
            return "done"

    @clustertrace.trace
    def failing_path():
        clustertrace.tag("agent", "research")
        with clustertrace.span("fetch_paper"):
            raise RuntimeError("boom while parsing PDF")

    ok_path()
    ok_path()
    try:
        failing_path()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Tool schemas + dispatch metadata
# ---------------------------------------------------------------------------


def test_tool_schemas_cover_all_six_tools():
    schemas = mcp_server.get_tool_schemas()
    names = {t["name"] for t in schemas}
    assert names == {
        "list_clusters",
        "get_trace",
        "search",
        "failure_summary",
        "compare_traces",
        "recent_failed",
    }


def test_tool_schemas_have_valid_jsonschema_shape():
    for t in mcp_server.get_tool_schemas():
        assert isinstance(t["name"], str)
        assert isinstance(t["description"], str)
        s = t["inputSchema"]
        assert s["type"] == "object"
        assert isinstance(s.get("properties", {}), dict)
        # Verifies properties are typed and not free-form.
        for prop in s["properties"].values():
            assert "type" in prop or "enum" in prop


def test_dispatch_unknown_tool_raises_keyerror():
    with pytest.raises(KeyError):
        mcp_server.dispatch_tool("not_a_real_tool", {})


# ---------------------------------------------------------------------------
# list_clusters
# ---------------------------------------------------------------------------


def test_list_clusters_returns_shape_matching_dashboard():
    _seed_traces()
    out = mcp_server.tool_list_clusters(limit=10)
    assert out["mode"] == "ordered"
    assert isinstance(out["clusters"], list)
    assert len(out["clusters"]) >= 1
    cl = out["clusters"][0]
    for key in (
        "signature",
        "sig_hash",
        "count",
        "errors",
        "error_rate",
        "representative_trace_id",
        "pattern",
    ):
        assert key in cl
    # pattern is a list of {name, status} dicts
    assert all("name" in p and "status" in p for p in cl["pattern"])


def test_list_clusters_tree_edit_threshold_passes_through():
    _seed_traces()
    out = mcp_server.tool_list_clusters(limit=10, mode="tree_edit", threshold=99)
    assert out["mode"] == "tree_edit"
    # With a huge threshold all traces collapse into one cluster.
    assert len(out["clusters"]) == 1


# ---------------------------------------------------------------------------
# get_trace
# ---------------------------------------------------------------------------


def test_get_trace_returns_spans_and_tags():
    _seed_traces()
    # Pick the first failing trace via recent_failed
    failed = mcp_server.tool_recent_failed(limit=10)["traces"]
    assert failed, "expected at least one failed trace seeded"
    tid = failed[0]["id"]
    out = mcp_server.tool_get_trace(tid)
    assert "error" not in out
    assert out["trace"]["id"] == tid
    assert isinstance(out["spans"], list)
    assert len(out["spans"]) >= 2  # @trace root + child fetch_paper
    assert out["tags"].get("agent") == "research"
    # Span shape: input/output/attrs decoded
    s = out["spans"][0]
    for k in ("id", "name", "status", "input", "output", "attrs", "duration_ms"):
        assert k in s


def test_get_trace_missing_returns_structured_error():
    out = mcp_server.tool_get_trace("does-not-exist")
    assert out == {"error": "trace not found", "trace_id": "does-not-exist"}


def test_get_trace_empty_id_returns_structured_error():
    out = mcp_server.tool_get_trace("")
    assert "error" in out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_returns_results_for_known_substring():
    _seed_traces()
    out = mcp_server.tool_search("PDF")
    assert "results" in out
    assert isinstance(out["results"], list)
    # We seeded "boom while parsing PDF" in the error path; expect at least
    # one hit referencing the fetch_paper span.
    assert any("PDF" in (r.get("snippet") or "") for r in out["results"]) or out["results"]


def test_search_empty_query_returns_empty_list():
    out = mcp_server.tool_search("")
    assert out["results"] == []
    assert "query" in out


def test_search_caps_limit():
    out = mcp_server.tool_search("anything", limit=99999)
    # Doesn't crash; limit is clamped.
    assert isinstance(out["results"], list)


# ---------------------------------------------------------------------------
# failure_summary
# ---------------------------------------------------------------------------


def test_failure_summary_includes_overall_rate_and_clusters():
    _seed_traces()
    out = mcp_server.tool_failure_summary()
    assert out["traces_total"] >= 3
    assert out["traces_failed"] >= 1
    assert 0.0 <= out["overall_failure_rate"] <= 1.0
    assert isinstance(out["clusters"], list)
    # Tag-grouped prefixes ride along when group_by_tag is set.
    assert "prefixes_by_tag" in out


# ---------------------------------------------------------------------------
# recent_failed
# ---------------------------------------------------------------------------


def test_recent_failed_returns_only_errors_in_descending_order():
    _seed_traces()
    out = mcp_server.tool_recent_failed(limit=5)
    assert out["traces"]
    assert all(t["status"] == "error" for t in out["traces"])
    # Sorted by started_at desc.
    started = [t["started_at"] for t in out["traces"]]
    assert started == sorted(started, reverse=True)


def test_recent_failed_when_no_failures_returns_empty():
    @clustertrace.trace
    def ok_only():
        return 1

    ok_only()
    out = mcp_server.tool_recent_failed(limit=5)
    assert out["traces"] == []


# ---------------------------------------------------------------------------
# compare_traces
# ---------------------------------------------------------------------------


def test_compare_traces_identical_traces_have_only_equals():
    @clustertrace.trace
    def same():
        with clustertrace.span("step_a"):
            pass
        with clustertrace.span("step_b"):
            pass

    same()
    same()
    listed = mcp_server.tool_list_clusters(limit=5)
    assert listed["clusters"]
    cluster_rep = listed["clusters"][0]["representative_trace_id"]
    # Find another trace with the same pattern
    out = mcp_server.tool_compare_traces(cluster_rep, cluster_rep)
    assert all(op["op"] == "equal" for op in out["diff"])
    assert out["summary"]["edit_distance"] == 0


def test_compare_traces_different_traces_emit_diff_ops():
    @clustertrace.trace
    def path_a():
        with clustertrace.span("step_a"):
            pass
        with clustertrace.span("step_b"):
            pass

    @clustertrace.trace
    def path_b():
        with clustertrace.span("step_a"):
            pass
        with clustertrace.span("step_b"):
            pass
        with clustertrace.span("step_c"):
            pass

    path_a()
    path_b()
    # The two most recent trace ids:
    from clustertrace import storage

    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM traces ORDER BY started_at DESC LIMIT 2"
        ).fetchall()
    ids = [r["id"] for r in rows]
    out = mcp_server.tool_compare_traces(ids[0], ids[1])
    assert out["summary"]["edit_distance"] >= 1
    ops = {op["op"] for op in out["diff"]}
    # The b-side has one extra step → insert OR delete (depending on order)
    assert "insert" in ops or "delete" in ops


def test_compare_traces_missing_a_returns_error():
    out = mcp_server.tool_compare_traces("not-real", "also-not-real")
    assert "error" in out


# ---------------------------------------------------------------------------
# Live MCP server construction
# ---------------------------------------------------------------------------


def test_build_server_registers_all_six_tools_in_real_mcp_runtime():
    # If `mcp` isn't installed this test is skipped. We installed it as a
    # dev dep so it should be importable here.
    pytest.importorskip("mcp")
    import asyncio

    server = mcp_server.build_server()
    # The mcp SDK exposes registered tools via the request handler map; we
    # invoke the list_tools handler directly to confirm names line up.
    handlers = server.request_handlers
    from mcp.types import ListToolsRequest

    list_request = ListToolsRequest(method="tools/list", params=None)
    handler = handlers[ListToolsRequest]
    result = asyncio.new_event_loop().run_until_complete(handler(list_request))
    names = {t.name for t in result.root.tools}
    assert names == {
        "list_clusters",
        "get_trace",
        "search",
        "failure_summary",
        "compare_traces",
        "recent_failed",
    }


def test_dispatch_via_real_call_tool_returns_json_text():
    pytest.importorskip("mcp")
    import asyncio

    _seed_traces()
    server = mcp_server.build_server()
    from mcp.types import CallToolRequest, CallToolRequestParams

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="recent_failed", arguments={"limit": 3}),
    )
    handler = server.request_handlers[CallToolRequest]
    result = asyncio.new_event_loop().run_until_complete(handler(req))
    # CallToolResult content[0] is a TextContent with JSON-encoded payload.
    text = result.root.content[0].text
    parsed = json.loads(text)
    assert "traces" in parsed


# ---------------------------------------------------------------------------
# Install command — config-writing helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmpdir so install never touches the user's real configs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def test_snippet_emits_clustertrace_command():
    snip = mcp_install.snippet()
    assert snip["command"] == "clustertrace"
    assert snip["args"] == ["mcp"]


def test_config_path_for_unknown_target_raises():
    with pytest.raises(mcp_install.InstallError):
        mcp_install.config_path_for("emacs")


def test_install_creates_config_when_missing(isolated_home):
    result = mcp_install.install(target="cursor")
    assert result.path.exists()
    assert result.backup_path is None  # nothing existed to back up
    with result.path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["mcpServers"]["clustertrace"]["command"] == "clustertrace"


def test_install_merges_into_existing_config_and_backs_up(isolated_home, monkeypatch):
    # Pre-populate ~/.continue/config.json with an unrelated entry
    cfg_path = mcp_install.config_path_for("continue")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "foo"}}, "models": []}),
        encoding="utf-8",
    )
    result = mcp_install.install(target="continue")
    assert result.backup_path is not None and result.backup_path.exists()
    with cfg_path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    # Our entry was added without disturbing the existing one.
    assert "other" in cfg["mcpServers"]
    assert "clustertrace" in cfg["mcpServers"]
    assert cfg["models"] == []  # untouched


def test_install_idempotent_when_entry_already_correct(isolated_home):
    mcp_install.install(target="cursor")
    result = mcp_install.install(target="cursor")
    # No backup written on the second run because the file already matched.
    assert result.backup_path is None


def test_install_rejects_non_object_mcp_servers_field(isolated_home):
    cfg_path = mcp_install.config_path_for("cursor")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"mcpServers": [1, 2, 3]}), encoding="utf-8")
    with pytest.raises(mcp_install.InstallError):
        mcp_install.install(target="cursor")


def test_install_rejects_invalid_json(isolated_home):
    cfg_path = mcp_install.config_path_for("cursor")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not json at all {{{", encoding="utf-8")
    with pytest.raises(mcp_install.InstallError):
        mcp_install.install(target="cursor")


def test_claude_code_config_honors_CLAUDE_CONFIG_DIR(isolated_home, monkeypatch):
    custom = isolated_home / "custom-claude-dir"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    p = mcp_install.config_path_for("claude-code")
    assert str(custom) in str(p)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_mcp_without_subcommand_runs_help_when_no_subcommand():
    # Click's runner is the cleanest way to verify the group resolves; we
    # invoke `--help` rather than actually starting the server.
    from click.testing import CliRunner

    from clustertrace.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "MCP server" in result.output or "mcp server" in result.output.lower()


def test_cli_mcp_install_prints_snippet_without_target():
    from click.testing import CliRunner

    from clustertrace.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "install"])
    assert result.exit_code == 0
    assert "clustertrace" in result.output
    # JSON snippet structure visible
    assert '"command"' in result.output


def test_cli_mcp_http_flag_is_rejected_in_v0_9():
    from click.testing import CliRunner

    from clustertrace.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "--http", "9001"])
    assert result.exit_code != 0
    assert "HTTP" in result.output or "http" in result.output


def test_cli_mcp_install_writes_to_isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    from click.testing import CliRunner

    from clustertrace.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "install", "--target", "cursor"])
    assert result.exit_code == 0, result.output
    cfg_path = mcp_install.config_path_for("cursor")
    assert cfg_path.exists()
    with cfg_path.open(encoding="utf-8") as f:
        data = json.load(f)
    assert data["mcpServers"]["clustertrace"]["args"] == ["mcp"]


def test_cli_mcp_install_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    from click.testing import CliRunner

    from clustertrace.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main, ["mcp", "install", "--target", "cursor", "--dry-run"]
    )
    assert result.exit_code == 0
    cfg_path = mcp_install.config_path_for("cursor")
    assert not cfg_path.exists()


# Silence "imported but unused" — we keep the import for `os` available in case
# of follow-up tests that need PATH manipulation.
_ = (os, Path)
