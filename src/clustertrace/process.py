"""Tiny helpers for on-demand dashboard spawn.

Used by the `clustertrace ensure-dashboard` command (called from the Claude
Code SessionStart hook). Goal: return in <100ms in both branches (already
running / not running) so a hook never blocks the editor.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys


def port_responsive(host: str = "127.0.0.1", port: int = 7777, timeout_s: float = 0.3) -> bool:
    """Return True iff something is accepting TCP on host:port. Cheap (one
    socket connect with a short timeout)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((host, port))
        return True
    except (TimeoutError, OSError):
        return False
    finally:
        s.close()


def _pythonw_for(python_exe: str) -> str:
    """Prefer pythonw.exe (console-less) on Windows so the spawned dashboard
    has no flashing console window. Falls back to the supplied python.exe."""
    if not python_exe.lower().endswith("python.exe"):
        return python_exe
    candidate = python_exe[: -len("python.exe")] + "pythonw.exe"
    return candidate if os.path.exists(candidate) else python_exe


def spawn_dashboard_detached(
    port: int = 7777,
    idle_shutdown_seconds: int = 15 * 60,
) -> int:
    """Spawn `clustertrace.cli dashboard --port N` as a fully detached background
    process. Returns the child's PID. Does NOT wait for the dashboard to bind.

    Env: sets CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS so the spawned process
    self-exits after the configured idle window.
    """
    python_exe = sys.executable
    env = os.environ.copy()
    env["CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS"] = str(idle_shutdown_seconds)

    cmd = [python_exe, "-m", "clustertrace.cli", "dashboard", "--port", str(port)]

    if sys.platform == "win32":
        # Use pythonw so there's no console window. Detach so the child
        # outlives the parent shell / hook process.
        cmd[0] = _pythonw_for(python_exe)
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            env=env,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        # POSIX: new session so we're not in the hook's process group.
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    return proc.pid
