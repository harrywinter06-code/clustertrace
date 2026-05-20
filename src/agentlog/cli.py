"""agentlog CLI — `agentlog dashboard` launches the local UI."""
from __future__ import annotations

import click

from agentlog import storage


@click.group()
@click.version_option()
def main() -> None:
    """agentlog — local-first LLM agent tracing."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind.")
@click.option("--port", default=7777, show_default=True, type=int, help="Port to listen on.")
@click.option("--reload", is_flag=True, help="Enable autoreload for development.")
def dashboard(host: str, port: int, reload: bool) -> None:
    """Launch the local dashboard on http://127.0.0.1:7777 (default)."""
    import uvicorn

    db = storage.get_db_path()
    click.echo(f"agentlog dashboard → http://{host}:{port}")
    click.echo(f"reading traces from: {db}")
    uvicorn.run(
        "agentlog.dashboard.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


@main.command()
def db_path() -> None:
    """Print the SQLite database path agentlog is using."""
    click.echo(str(storage.get_db_path()))
