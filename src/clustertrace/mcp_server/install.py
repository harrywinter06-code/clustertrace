"""Wire the clustertrace MCP server into a supported editor's config.

We never overwrite an existing config wholesale — we read, merge, and write
back atomically with a `.bak-<timestamp>` backup alongside. If the target
file doesn't exist yet, we create it (and its parent dirs).

Schema: each editor uses an `mcpServers` object keyed by server name. We
write our entry under that key, with `command: "clustertrace"` and
`args: ["mcp"]` so the editor spawns the locally-installed CLI. No absolute
path — we rely on whatever resolves first on the user's `$PATH`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InstallError(RuntimeError):
    """User-facing error during MCP install (unsupported target, write failed, etc.)."""


# ---------------------------------------------------------------------------
# Per-editor config paths.
#
# These are the well-known locations as of the MCP-config schemas documented
# by each editor in early 2026. They may move; if so, the install command
# fails loudly rather than silently writing to the wrong place.
# ---------------------------------------------------------------------------


def _claude_code_config_path() -> Path:
    """Claude Code stores MCP servers under ~/.claude.json (top-level mcpServers).

    Honors $CLAUDE_CONFIG_DIR if set (matches Claude Code's own convention).
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def _cursor_config_path() -> Path:
    """Cursor reads ~/.cursor/mcp.json (project-level files exist too, we use user-global)."""
    return Path.home() / ".cursor" / "mcp.json"


def _continue_config_path() -> Path:
    """Continue stores its config at ~/.continue/config.json (top-level mcpServers)."""
    return Path.home() / ".continue" / "config.json"


_TARGETS: dict[str, Any] = {
    "claude-code": _claude_code_config_path,
    "cursor": _cursor_config_path,
    "continue": _continue_config_path,
}


def config_path_for(target: str) -> Path:
    """Return the editor's MCP config path. Raises if `target` is unknown."""
    fn = _TARGETS.get(target)
    if fn is None:
        raise InstallError(
            f"unknown target: {target!r}. supported: {sorted(_TARGETS)}"
        )
    return fn()


def snippet(name: str = "clustertrace") -> dict[str, Any]:
    """The JSON value (one server entry) to write under `mcpServers[name]`.

    We invoke the CLI rather than `python -m clustertrace.mcp_server` so the
    user only needs `clustertrace` on PATH. If the user's editor runs from a
    different shell than the install, they may need to add the venv's bin
    dir to PATH or replace `command` with an absolute path.
    """
    return {
        "command": "clustertrace",
        "args": ["mcp"],
    }


@dataclass
class InstallResult:
    path: Path
    backup_path: Path | None


def install(target: str, name: str = "clustertrace") -> InstallResult:
    """Merge our MCP entry into the target editor's config file.

    - Creates the file and parent dirs if missing.
    - If the file exists, makes a timestamped backup (.bak-YYYYMMDD-HHMMSS)
      before writing. If our entry already matches what we'd write, returns
      without touching anything.
    - Atomically replaces the file via os.replace on a tempfile.
    """
    cfg_path = config_path_for(target)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                existing = json.load(f) or {}
            if not isinstance(existing, dict):
                raise InstallError(
                    f"{cfg_path} does not contain a JSON object; cannot merge MCP server entry"
                )
        except json.JSONDecodeError as e:
            raise InstallError(f"{cfg_path} is not valid JSON: {e}") from e

    servers = existing.get("mcpServers")
    if servers is None:
        servers = {}
        existing["mcpServers"] = servers
    elif not isinstance(servers, dict):
        raise InstallError(
            f"{cfg_path}: 'mcpServers' is not a JSON object (found {type(servers).__name__})"
        )

    desired = snippet(name=name)
    if servers.get(name) == desired:
        # Nothing to do — already exactly what we'd write.
        return InstallResult(path=cfg_path, backup_path=None)

    backup_path: Path | None = None
    if cfg_path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = cfg_path.with_name(cfg_path.name + f".bak-{ts}")
        try:
            shutil.copy2(cfg_path, backup_path)
        except OSError as e:
            raise InstallError(
                f"could not back up {cfg_path} to {backup_path}: {e}"
            ) from e

    servers[name] = desired
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, cfg_path)
    except OSError as e:
        # Try to clean up the temp file; ignore failure.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise InstallError(f"could not write {cfg_path}: {e}") from e

    return InstallResult(path=cfg_path, backup_path=backup_path)


# `sys` is imported defensively for future use (e.g. emitting environment hints).
# Keep the reference to silence "unused import" without bothering tooling.
_ = sys
