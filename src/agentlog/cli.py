"""agentlog CLI — launches the dashboard and runs maintenance/export commands."""
from __future__ import annotations

import sys

import click

from agentlog import storage


@click.group()
@click.version_option()
def main() -> None:
    """agentlog — local-first LLM agent tracing."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=7777, show_default=True, type=int)
@click.option("--reload", is_flag=True)
def dashboard(host: str, port: int, reload: bool) -> None:
    """Launch the local dashboard on http://127.0.0.1:7777 (default)."""
    import uvicorn

    click.echo(f"agentlog dashboard → http://{host}:{port}")
    click.echo(f"reading traces from: {storage.get_db_path()}")
    uvicorn.run(
        "agentlog.dashboard.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


@main.command("db-path")
def db_path() -> None:
    """Print the SQLite database path agentlog is using."""
    click.echo(str(storage.get_db_path()))


@main.command("backfill-cost")
def backfill_cost() -> None:
    """Compute and cache USD cost for every LLM call in the DB."""
    from agentlog import cost

    n, total = cost.backfill()
    click.echo(f"priced {n} spans · total ${total:.4f}")


@main.command("backfill-signatures")
def backfill_signatures() -> None:
    """Compute and store structural signatures for traces missing them."""
    from agentlog import cluster

    n = cluster.backfill_signatures()
    click.echo(f"signed {n} traces")


@main.command("snapshot")
@click.argument("trace_id")
@click.option("--out", "-o", type=click.Path(), default=None,
              help="Output HTML file. Default: stdout.")
def snapshot(trace_id: str, out: str | None) -> None:
    """Render a self-contained shareable HTML for one trace."""
    from agentlog import snapshot as snap

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
    from agentlog import export as exp

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
    from agentlog import export as exp

    imported, skipped = exp.import_lines(sys.stdin)
    click.echo(f"imported {imported}, skipped {skipped}")


@main.command("replay")
@click.argument("trace_id")
@click.option("--entry", required=True,
              help="Entrypoint as 'module:function' (e.g. examples.agents:research_agent).")
def replay_cmd(trace_id: str, entry: str) -> None:
    """Re-run a stored trace by re-calling its entrypoint with the same args."""
    from agentlog import replay as rp

    new_id = rp.replay(trace_id, entry)
    if new_id:
        click.echo(f"replayed → new trace id: {new_id}")
    else:
        click.echo("replay completed but no new trace id was captured", err=True)


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
