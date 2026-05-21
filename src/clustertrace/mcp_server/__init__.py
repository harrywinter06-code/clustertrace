"""clustertrace MCP server — exposes traces, clusters, and search to AI editors.

Optional install: `pip install "clustertrace[mcp]"`. Without the `mcp` SDK on
the path, importing this module raises; the CLI `clustertrace mcp` catches that
and prints a one-line install hint before exiting non-zero.
"""
from __future__ import annotations


def main() -> int:
    """Entrypoint — start the MCP server over stdio (or HTTP, see CLI).

    Returns the process exit code. The CLI subcommand `clustertrace mcp` is the
    public surface; this `main()` is kept thin so it can also be invoked
    programmatically by editors that look for a `python -m` entrypoint.
    """
    from clustertrace.mcp_server.server import run_stdio

    return run_stdio()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
