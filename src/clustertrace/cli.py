"""clustertrace CLI — launches the dashboard and runs maintenance/export commands."""
from __future__ import annotations

import json
import sys

import click

from clustertrace import storage


@click.group()
@click.version_option()
def main() -> None:
    """clustertrace — local-first LLM agent tracing."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=7777, show_default=True, type=int)
@click.option("--reload", is_flag=True)
def dashboard(host: str, port: int, reload: bool) -> None:
    """Launch the local dashboard on http://127.0.0.1:7777 (default)."""
    import uvicorn

    click.echo(f"clustertrace dashboard -> http://{host}:{port}")
    click.echo(f"reading traces from: {storage.get_db_path()}")
    uvicorn.run(
        "clustertrace.dashboard.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


@main.command()
@click.option("--port", default=7777, show_default=True, type=int)
@click.option("--no-browser", is_flag=True, help="Don't open the browser automatically.")
def demo(port: int, no_browser: bool) -> None:
    """One-step trial: import bundled demo data and launch the dashboard.

    No API key needed. 60 real traces of three agents (research, rag,
    tool_use) showing failure clustering, search, cost, and metrics.
    """
    import importlib.resources
    import os
    import tempfile
    import threading
    import time
    import webbrowser

    import uvicorn

    from clustertrace import export

    # Use a temp DB so we don't clobber the user's main store.
    tmp = tempfile.NamedTemporaryFile(prefix="clustertrace-demo-", suffix=".db", delete=False)
    tmp.close()
    os.environ["CLUSTERTRACE_DB"] = tmp.name
    storage.reset_initialized_cache()

    try:
        data_file = importlib.resources.files("clustertrace").joinpath("data/demo-traces.jsonl")
        with data_file.open("r", encoding="utf-8") as f:
            imported, skipped = export.import_lines(f)
    except Exception as e:
        raise click.ClickException(f"could not load bundled demo data: {e}") from e

    click.echo(f"loaded {imported} demo traces (skipped {skipped}) into {tmp.name}")

    # Best-effort: ensure backfilled signatures/costs are present.
    try:
        from clustertrace import cluster, cost
        cluster.backfill_signatures()
        cost.backfill()
    except Exception:
        pass

    url = f"http://127.0.0.1:{port}"
    click.echo(f"clustertrace demo -> {url}")
    click.echo("-> start with /clusters to see the failure-pattern view")

    if not no_browser:
        def _open():
            time.sleep(0.7)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "clustertrace.dashboard.app:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


@main.command("db-path")
def db_path() -> None:
    """Print the SQLite database path clustertrace is using."""
    click.echo(str(storage.get_db_path()))


@main.command("backfill-cost")
def backfill_cost() -> None:
    """Compute and cache USD cost for every LLM call in the DB."""
    from clustertrace import cost

    n, total = cost.backfill()
    click.echo(f"priced {n} spans, total ${total:.4f}")


@main.command("backfill-signatures")
def backfill_signatures() -> None:
    """Compute and store structural signatures for traces missing them."""
    from clustertrace import cluster

    n = cluster.backfill_signatures()
    click.echo(f"signed {n} traces")


@main.command("snapshot")
@click.argument("trace_id")
@click.option("--out", "-o", type=click.Path(), default=None,
              help="Output HTML file. Default: stdout.")
def snapshot(trace_id: str, out: str | None) -> None:
    """Render a self-contained shareable HTML for one trace."""
    from clustertrace import snapshot as snap

    html = snap.render(trace_id)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        click.echo(f"wrote {out}", err=True)
    else:
        sys.stdout.write(html)


@main.command("export")
@click.argument("trace_id", required=False)
@click.option("--all", "all_", is_flag=True, help="Export every trace.")
@click.option("--limit", type=int, default=None, help="Cap on --all output.")
def export_cmd(trace_id: str | None, all_: bool, limit: int | None) -> None:
    """Export trace(s) as JSON Lines (one trace per line) to stdout."""
    from clustertrace import export as exp

    if all_:
        n = exp.export_all(sys.stdout, limit=limit)
        click.echo(f"exported {n} traces", err=True)
    elif trace_id:
        n = exp.export_trace(trace_id, sys.stdout)
        click.echo(f"exported {n} trace(s)", err=True)
    else:
        raise click.UsageError("provide a trace_id or --all")


@main.command(
    "import",
    help=(
        "Import traces from another tool's export.\n\n"
        "Supported sources:\n"
        "  native    — clustertrace JSONL (default; backward-compat)\n"
        "  langfuse  — Langfuse JSON / JSONL export\n"
        "  phoenix   — Arize Phoenix / OpenInference span export\n"
        "  langsmith — LangSmith run export\n"
        "  otel      — OTLP/JSON span data\n\n"
        "Reads stdin unless --file is given. Existing trace IDs are skipped."
    ),
)
@click.option(
    "--from",
    "source",
    default="native",
    show_default=True,
    help="Source format. One of: native, langfuse, phoenix, langsmith, otel.",
)
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=None,
    help="Read from a file instead of stdin.",
)
def import_cmd(source: str, file_path: str | None) -> None:
    """Import traces from a competitor tool or clustertrace's own JSONL."""
    from clustertrace.importers import SOURCES, SUPPORTED

    if source == "native":
        from clustertrace import export as exp

        stream = open(file_path, encoding="utf-8") if file_path else sys.stdin
        try:
            imported, skipped = exp.import_lines(stream)
        finally:
            if file_path:
                stream.close()
        click.echo(f"imported {imported} traces, skipped {skipped} lines")
        return

    if source not in SOURCES:
        click.echo(f"unknown source: {source}; supported: {SUPPORTED}", err=True)
        sys.exit(2)

    importer = SOURCES[source]
    stream = open(file_path, encoding="utf-8") if file_path else sys.stdin
    try:
        imported, skipped = importer(stream)
    finally:
        if file_path:
            stream.close()
    click.echo(f"imported {imported} traces, skipped {skipped} lines")


@main.command("replay")
@click.argument("trace_id")
@click.option("--entry", required=True,
              help="Entrypoint as 'module:function' (e.g. examples.agents:research_agent).")
def replay_cmd(trace_id: str, entry: str) -> None:
    """Re-run a stored trace by re-calling its entrypoint with the same args."""
    from clustertrace import replay as rp

    new_id = rp.replay(trace_id, entry)
    if new_id:
        click.echo(f"replayed -> new trace id: {new_id}")
    else:
        click.echo("replay completed but no new trace id was captured", err=True)


@main.command("repro")
@click.argument("sig_hash_or_trace_id")
@click.option("--entry", required=True,
              help="Entrypoint as 'module:function' (e.g. examples.agents:research_agent).")
@click.option("--mode", type=click.Choice(["positive", "negative"]), default="positive",
              show_default=True,
              help="positive=assert no raise; negative=assert original error_type raises.")
@click.option("--out", "-o", type=click.Path(), default=None,
              help="Write to this file instead of stdout. "
                   "Convention: tests/test_repro_<short_hash>.py.")
def repro_cmd(sig_hash_or_trace_id: str, entry: str, mode: str, out: str | None) -> None:
    """Generate a pytest reproduction from a captured trace.

    Pass either a trace_id or a sig_hash (from /clusters). For a sig_hash the
    most recent failing trace in that cluster is used as the seed.
    """
    from clustertrace import repro as _repro

    try:
        src = _repro.generate_repro(sig_hash_or_trace_id, entry, mode=mode)
    except _repro.ReproError as e:
        raise click.ClickException(str(e)) from e
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(src)
        click.echo(f"wrote {out}", err=True)
    else:
        sys.stdout.write(src)


@main.command()
@click.option("--stale-after", default="5m", show_default=True,
              help="How long a 'running' trace must sit before being flipped to 'incomplete'.")
def cleanup(stale_after: str) -> None:
    """Finalize orphan traces left in 'running' state (after a crash, kill, etc.)."""
    from clustertrace import maintenance

    secs = maintenance.parse_duration(stale_after)
    n = maintenance.cleanup_orphans(stale_after_seconds=secs)
    click.echo(f"cleaned up {n} orphan trace(s)")


@main.command()
@click.option("--older-than", default="30d", show_default=True,
              help="Delete traces older than this (e.g. 7d, 24h, 30m).")
@click.option("--dry-run", is_flag=True, help="Report what would be deleted; make no changes.")
def vacuum(older_than: str, dry_run: bool) -> None:
    """Delete old traces and reclaim disk space.

    The DB grows until you vacuum. ON DELETE CASCADE handles spans, tags, metrics.
    """
    from clustertrace import maintenance

    secs = maintenance.parse_duration(older_than)
    n, freed = maintenance.vacuum(older_than_seconds=secs, dry_run=dry_run)
    if dry_run:
        click.echo(f"would delete {n} trace(s)")
    else:
        mb = freed / (1024 * 1024)
        click.echo(f"deleted {n} trace(s), reclaimed {mb:.2f} MB")


@main.group("mcp", invoke_without_command=True)
@click.option(
    "--http",
    "http_port",
    type=int,
    default=None,
    help="Reserved — HTTP transport is not yet shipped. v0.9 supports stdio only.",
)
@click.pass_context
def mcp_group(ctx: click.Context, http_port: int | None) -> None:
    """Start the clustertrace MCP server (stdio by default).

    With no subcommand, this starts the server reading JSON-RPC on stdin and
    writing on stdout — the shape Claude Code / Cursor / Continue expect.

    Run `clustertrace mcp install --target claude-code` to wire it up.
    """
    if ctx.invoked_subcommand is not None:
        return
    if http_port is not None:
        # Decision boundary: stdio only in v0.9 (see roadmap). HTTP arrives
        # when there's a concrete client that needs it.
        raise click.ClickException(
            "HTTP transport is not yet supported in v0.9 — use stdio "
            "(the default) with Claude Code / Cursor / Continue."
        )
    try:
        import mcp  # noqa: F401
    except ImportError:
        click.echo(
            "the MCP SDK is not installed.\n"
            'install it with: pip install "clustertrace[mcp]"',
            err=True,
        )
        sys.exit(2)
    from clustertrace.mcp_server import main as mcp_main

    sys.exit(mcp_main())


@mcp_group.command("install")
@click.option(
    "--target",
    type=click.Choice(["claude-code", "cursor", "continue"]),
    default=None,
    help="Editor whose MCP config should be edited. If omitted, prints the snippet.",
)
@click.option(
    "--name",
    "server_name",
    default="clustertrace",
    show_default=True,
    help="Server entry name (the key under mcpServers).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be written without modifying the editor config.",
)
def mcp_install(target: str | None, server_name: str, dry_run: bool) -> None:
    """Write an MCP-server entry into the target editor's config file."""
    from clustertrace.mcp_server import install as _install

    snippet = _install.snippet(name=server_name)
    if target is None:
        click.echo("# Paste this into your editor's MCP config (mcpServers map):")
        click.echo(json.dumps({server_name: snippet}, indent=2))
        return

    path = _install.config_path_for(target)
    if dry_run:
        click.echo(f"# would write to {path}:")
        click.echo(json.dumps({server_name: snippet}, indent=2))
        return
    try:
        result = _install.install(target=target, name=server_name)
    except _install.InstallError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"installed clustertrace MCP entry in {result.path}")
    if result.backup_path is not None:
        click.echo(f"backup: {result.backup_path}")


@main.command("inspect")
@click.argument("trace_id", required=False)
@click.option(
    "--latest",
    is_flag=True,
    help="Pick the most recent trace if no id is given.",
)
@click.option(
    "--failed",
    is_flag=True,
    help="Pick the most recent failed trace (overrides --latest).",
)
@click.option(
    "--expand",
    "expand",
    multiple=True,
    help="Span id(s) whose input/output JSON to show in full. Repeatable.",
)
@click.option(
    "--width",
    type=int,
    default=None,
    help="Output width (default: terminal width).",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable ANSI colors (useful when piping).",
)
def inspect_cmd(
    trace_id: str | None,
    latest: bool,
    failed: bool,
    expand: tuple[str, ...],
    width: int | None,
    no_color: bool,
) -> None:
    """Render a trace as a rich Gantt + I/O tree in the terminal.

    Works fully offline — pure-local SQLite reads. No network is touched.
    """
    from clustertrace import inspect as _inspect

    try:
        chosen_id = _inspect.resolve_trace_id(
            trace_id=trace_id, latest=latest, failed=failed
        )
    except _inspect.InspectError as e:
        raise click.ClickException(str(e)) from e

    _inspect.render_to_console(
        chosen_id,
        expand_span_ids=set(expand),
        width=width,
        no_color=no_color,
    )


@main.command("judge")
@click.option("--rubric", default=None,
              help="Plain-English rubric for the LLM judge. Omit to use the free "
                   "no-exceptions evaluator (no API calls).")
@click.option("--samples", "n_samples", default=3, show_default=True, type=int,
              help="How many traces per cluster to evaluate.")
@click.option("--max-cost-usd", "max_cost_usd", default=0.50, show_default=True, type=float,
              help="Abort before any LLM calls if the predicted spend would exceed this.")
@click.option("--model", "model", default="claude-haiku-4-5-20251001", show_default=True,
              help="Judge model — only consulted when --rubric is set.")
@click.option("--mode", default="ordered", show_default=True,
              type=click.Choice(["ordered", "set", "tree_edit"]))
@click.option("--sig-hash", "single_sig_hash", default=None,
              help="Evaluate only this cluster (skips the budget check, since cost ~= 1×).")
def judge_cmd(
    rubric: str | None,
    n_samples: int,
    max_cost_usd: float,
    model: str,
    mode: str,
    single_sig_hash: str | None,
) -> None:
    """Run an evaluator on cluster representatives.

    Default: `no_exceptions_evaluator` (free, no API calls). Pass `--rubric`
    to run the LLM judge — requires `ANTHROPIC_API_KEY`.
    """
    from clustertrace import judge as _judge

    if rubric:
        evaluator = _judge.llm_judge_evaluator(rubric, model=model)
    else:
        evaluator = _judge.no_exceptions_evaluator

    if single_sig_hash:
        verdict = _judge.evaluate_cluster(
            single_sig_hash, evaluator, n_samples=n_samples, mode=mode
        )
        click.echo(_render_judgment_markdown([(single_sig_hash, verdict)]))
        return

    try:
        verdicts = _judge.evaluate_all_clusters(
            evaluator, mode=mode, n_samples=n_samples, max_cost_usd=max_cost_usd
        )
    except _judge.JudgeCostCapExceeded as e:
        raise click.ClickException(str(e)) from e
    click.echo(_render_judgment_markdown(list(verdicts.items())))


def _render_judgment_markdown(verdicts: list) -> str:
    """Brief asks for a markdown report. Keep it grep-friendly."""
    if not verdicts:
        return "# Judgment report\n\nNo clusters to judge.\n"
    lines = ["# Judgment report\n"]
    for sig_hash, v in verdicts:
        header = f"## Cluster {sig_hash} — {v.pass_count}/{v.samples_evaluated} pass"
        lines.append(header)
        lines.append(f"evaluator: `{v.evaluator_name}`")
        lines.append("")
        lines.append(v.notes)
        if v.representative_failures:
            lines.append("")
            lines.append("Representative failing traces:")
            for tid in v.representative_failures[:5]:
                lines.append(f"- `{tid}`")
        lines.append("")
    return "\n".join(lines)


@main.command("annotate")
@click.argument("sig_hash")
@click.option("--status", type=click.Choice(
    ["expected-failure", "wontfix", "priority", "acceptable", "clear"]),
    default=None, help="Set or `clear` an annotation status.")
@click.option("--note", default=None, help="Free-form note (up to 4096 chars).")
@click.option("--tag", "tag", default=None, help="Append a tag label.")
def annotate_cmd(
    sig_hash: str, status: str | None, note: str | None, tag: str | None
) -> None:
    """Attach status / note / tag to a cluster. Survives across runs."""
    from clustertrace import annotations as _ann

    if status == "clear" and note is None and tag is None:
        removed = _ann.clear_annotation(sig_hash)
        click.echo("cleared" if removed else "no annotation to clear")
        return
    try:
        out = _ann.annotate_cluster(
            sig_hash,
            status=None if status == "clear" else status,
            note=note,
            tag=tag,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"updated annotation for {sig_hash}: status={out['status']}, "
               f"note={(out['note'] or '')[:60]!r}, tags={out['tags']}")


@main.command("assert")
@click.argument("sig_hash")
@click.option("--success-rate-above", "success_rate_above", type=float, default=None)
@click.option("--avg-latency-below", "avg_latency_below_ms", type=float, default=None,
              help="ms")
@click.option("--avg-cost-below", "avg_cost_below_usd", type=float, default=None,
              help="USD")
@click.option("--no-new-traces", "no_new_traces", is_flag=True,
              help="Cluster must have no traces in the window.")
@click.option("--over-last", "over_last", default="100", show_default=True,
              help="Window. Bare number = N traces; '1d' / '24h' / '60s' = time. "
                   "no-new-traces always uses time.")
def assert_cmd(
    sig_hash: str,
    success_rate_above: float | None,
    avg_latency_below_ms: float | None,
    avg_cost_below_usd: float | None,
    no_new_traces: bool,
    over_last: str,
) -> None:
    """Persist a pass/fail rule against a cluster.

    Exactly one assertion type per invocation. Run `clustertrace check` to
    evaluate them all.
    """
    from clustertrace import assertions as _ass

    chosen = [b for b in (
        success_rate_above is not None,
        avg_latency_below_ms is not None,
        avg_cost_below_usd is not None,
        no_new_traces,
    ) if b]
    if len(chosen) != 1:
        raise click.UsageError(
            "pass exactly one of --success-rate-above, --avg-latency-below, "
            "--avg-cost-below, --no-new-traces"
        )

    over, unit = _parse_over_last(over_last, force_seconds=no_new_traces)

    rule: dict
    if success_rate_above is not None:
        rule = {
            "kind": "success_rate_above",
            "threshold": success_rate_above,
            "over_last": over,
            "unit": unit,
        }
    elif avg_latency_below_ms is not None:
        rule = {
            "kind": "avg_latency_below",
            "ms": avg_latency_below_ms,
            "over_last": over,
            "unit": unit,
        }
    elif avg_cost_below_usd is not None:
        rule = {
            "kind": "avg_cost_below",
            "usd": avg_cost_below_usd,
            "over_last": over,
            "unit": unit,
        }
    else:
        rule = {"kind": "no_new_traces", "over_last": over, "unit": "seconds"}

    try:
        new_id = _ass.add_assertion(sig_hash, rule)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"saved assertion #{new_id} for cluster {sig_hash}: {rule}")


def _parse_over_last(raw: str, *, force_seconds: bool) -> tuple[int, str]:
    """Bare integer -> traces; otherwise parse as a duration string -> seconds."""
    from clustertrace import maintenance

    s = raw.strip()
    if not force_seconds and s.isdigit():
        return int(s), "traces"
    return maintenance.parse_duration(s), "seconds"


@main.command("check")
@click.option("--format", "fmt", default="text",
              type=click.Choice(["text", "json"]), show_default=True,
              help="`json` is intended for CI consumption: `clustertrace check --format json | jq`")
@click.option("--include-expected-failures", is_flag=True,
              help="Count assertions on expected-failure clusters toward the exit code.")
def check_cmd(fmt: str, include_expected_failures: bool) -> None:
    """Evaluate every persisted assertion. Exit code 0 if all pass, 1 if any fail.

    Intended for CI: run as `clustertrace check --format json | jq` after a
    nightly eval. The exit code is what matters; the body is just for humans
    and dashboards.
    """
    from clustertrace import annotations as _ann
    from clustertrace import assertions as _ass

    results, all_passed = _ass.check(
        exclude_expected_failures=not include_expected_failures
    )
    expected = _ann.expected_failure_sig_hashes() if not include_expected_failures else set()
    failed_count = sum(
        1 for r in results if not r.passed and (include_expected_failures or r.sig_hash not in expected)
    )
    if fmt == "json":
        import json as _json
        payload = {
            "passed": all_passed,
            "n_assertions": len(results),
            "n_failed": failed_count,
            "include_expected_failures": include_expected_failures,
            "results": [
                {**r.to_dict(), "expected_failure": r.sig_hash in expected}
                for r in results
            ],
        }
        click.echo(_json.dumps(payload, indent=2))
    else:
        if not results:
            click.echo("no assertions registered — exit 0")
        else:
            for r in results:
                badge = "PASS" if r.passed else "FAIL"
                if not r.passed and r.sig_hash in expected:
                    badge = "FAIL (expected)"
                click.echo(f"[{badge}] cluster {r.sig_hash} — {r.reason}")
            click.echo("")
            click.echo(
                f"{len(results)} assertion(s) checked; {failed_count} binding failure(s)"
            )
    if not all_passed:
        sys.exit(1)


@main.command("stats")
def stats() -> None:
    """Print a one-screen summary of the DB."""
    with storage.connect() as c:
        traces = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        failed = c.execute("SELECT COUNT(*) FROM traces WHERE status='error'").fetchone()[0]
        spans = c.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        total_cost = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM traces").fetchone()[0] or 0.0
        v = c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
    click.echo(f"db: {storage.get_db_path()}")
    click.echo(f"schema version: {v}")
    click.echo(f"traces: {traces}  failed: {failed}")
    click.echo(f"spans: {spans}")
    click.echo(f"cached cost: ${total_cost:.4f}")
