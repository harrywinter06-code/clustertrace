"""Tests for `clustertrace inspect`.

We render the trace into an in-memory string buffer via the `rich` Console
`file=` and `width=` parameters — gives us deterministic output without
touching a terminal.
"""
from __future__ import annotations

import io

import pytest
from click.testing import CliRunner

import clustertrace
from clustertrace import inspect as ct_inspect
from clustertrace import storage
from clustertrace.cli import main


def _seed_one_ok() -> str:
    @clustertrace.trace
    def research():
        clustertrace.tag("agent", "research")
        with clustertrace.span("fetch_paper"):
            clustertrace.tool_call("lookup", args={"q": "x"}, result={"hits": 3})
        with clustertrace.span("verify_claim"):
            clustertrace.metric("relevance", 0.92)
        return "done"

    research()
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT id FROM traces ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return row["id"]


def _seed_one_failed() -> str:
    @clustertrace.trace
    def crashes():
        clustertrace.tag("agent", "research")
        with clustertrace.span("fetch_paper"):
            raise ValueError("could not parse PDF metadata")

    try:
        crashes()
    except ValueError:
        pass
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT id FROM traces WHERE status='error' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# resolve_trace_id
# ---------------------------------------------------------------------------


def test_resolve_latest_returns_most_recent_trace():
    tid = _seed_one_ok()
    assert ct_inspect.resolve_trace_id(None, latest=True) == tid


def test_resolve_failed_picks_failed_over_latest():
    _ok_id = _seed_one_ok()
    failed_id = _seed_one_failed()
    # The failed trace was seeded last, but even if there's a later OK one
    # the --failed flag must filter on status.
    _ok_id2 = _seed_one_ok()
    assert ct_inspect.resolve_trace_id(None, latest=False, failed=True) == failed_id


def test_resolve_no_args_raises():
    _seed_one_ok()
    with pytest.raises(ct_inspect.InspectError):
        ct_inspect.resolve_trace_id(None)


def test_resolve_unknown_id_raises():
    _seed_one_ok()
    with pytest.raises(ct_inspect.InspectError):
        ct_inspect.resolve_trace_id("does-not-exist")


def test_resolve_failed_with_no_failed_traces_raises():
    _seed_one_ok()
    with pytest.raises(ct_inspect.InspectError):
        ct_inspect.resolve_trace_id(None, failed=True)


# ---------------------------------------------------------------------------
# render_to_console
# ---------------------------------------------------------------------------


def test_render_includes_trace_id_name_status_and_span_names():
    tid = _seed_one_ok()
    buf = io.StringIO()
    ct_inspect.render_to_console(tid, width=120, no_color=True, file=buf)
    out = buf.getvalue()
    assert tid[:12] in out
    assert "research" in out
    assert "fetch_paper" in out
    assert "verify_claim" in out
    # status icon for ok rows
    assert "ok" in out


def test_render_failed_trace_shows_error_type_and_message():
    tid = _seed_one_failed()
    buf = io.StringIO()
    ct_inspect.render_to_console(tid, width=120, no_color=True, file=buf)
    out = buf.getvalue()
    assert "ValueError" in out
    assert "could not parse PDF metadata" in out


def test_render_at_80_columns_and_200_columns_does_not_crash():
    tid = _seed_one_ok()
    for w in (80, 200):
        buf = io.StringIO()
        ct_inspect.render_to_console(tid, width=w, no_color=True, file=buf)
        # Every line fits within `w` characters (allowing trailing whitespace).
        # rich may include ANSI even with no_color=False; we passed no_color
        # so plain text is what's emitted.
        for line in buf.getvalue().splitlines():
            assert len(line) <= w + 8, (
                f"line wider than width={w}: {line!r} ({len(line)} chars)"
            )


def test_render_expand_spans_dumps_input_output_json():
    tid = _seed_one_ok()
    with storage.connect() as conn:
        spans = conn.execute(
            "SELECT id, name FROM spans WHERE trace_id=?",
            (tid,),
        ).fetchall()
    # Find the tool_call span (kind=tool_call) — it has structured input.
    target_id = None
    for s in spans:
        if "lookup" in s["name"]:
            target_id = s["id"]
            break
    assert target_id is not None
    buf = io.StringIO()
    ct_inspect.render_to_console(
        tid,
        expand_span_ids={target_id},
        width=160,
        no_color=True,
        file=buf,
    )
    out = buf.getvalue()
    # The input args {"q": "x"} should appear when expanded
    assert "lookup" in out
    assert '"q"' in out or "q:" in out


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_inspect_latest_runs_clean():
    tid = _seed_one_ok()
    runner = CliRunner()
    result = runner.invoke(main, ["inspect", "--latest", "--no-color"])
    assert result.exit_code == 0, result.output
    assert tid[:12] in result.output
    assert "fetch_paper" in result.output


def test_cli_inspect_failed_runs_clean():
    _ok = _seed_one_ok()
    failed_id = _seed_one_failed()
    runner = CliRunner()
    result = runner.invoke(main, ["inspect", "--failed", "--no-color"])
    assert result.exit_code == 0, result.output
    assert failed_id[:12] in result.output
    assert "ValueError" in result.output


def test_cli_inspect_with_explicit_id_runs_clean():
    tid = _seed_one_ok()
    runner = CliRunner()
    result = runner.invoke(main, ["inspect", tid, "--no-color"])
    assert result.exit_code == 0, result.output
    assert tid[:12] in result.output


def test_cli_inspect_without_args_errors():
    _seed_one_ok()
    runner = CliRunner()
    result = runner.invoke(main, ["inspect", "--no-color"])
    assert result.exit_code != 0


def test_cli_inspect_no_failed_traces_errors():
    _seed_one_ok()
    runner = CliRunner()
    result = runner.invoke(main, ["inspect", "--failed", "--no-color"])
    assert result.exit_code != 0
    assert "no failed traces" in result.output.lower()


def test_cli_inspect_expand_shows_io():
    tid = _seed_one_ok()
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM spans WHERE trace_id=? AND name LIKE 'lookup%'",
            (tid,),
        ).fetchall()
    assert rows
    span_id = rows[0]["id"]
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["inspect", tid, "--no-color", "--expand", span_id, "--width", "160"],
    )
    assert result.exit_code == 0, result.output
    assert "lookup" in result.output
    # JSON dump should contain the input arg key.
    assert '"q"' in result.output or "q:" in result.output
