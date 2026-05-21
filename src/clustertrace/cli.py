"""clustertrace CLI — launches the dashboard and runs maintenance/export commands."""
from __future__ import annotations

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


@main.command("import")
def import_cmd() -> None:
    """Import traces from JSON Lines on stdin. Existing IDs are skipped."""
    from clustertrace import export as exp

    imported, skipped = exp.import_lines(sys.stdin)
    click.echo(f"imported {imported}, skipped {skipped}")


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
