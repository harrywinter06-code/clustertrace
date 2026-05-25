"""Coverage for `clustertrace claude-code-hook install/uninstall/status` and
the idle-shutdown wiring on the dashboard.

The hook installer mutates ~/.claude/settings.json — we monkeypatch
`_claude_settings_path` to a temp file so tests don't touch the real one.
"""
from __future__ import annotations

import importlib
import json
import socket

import pytest
from click.testing import CliRunner

from clustertrace import cli, process

# ---------------------------------------------------------------------------
# Hook installer: install / refresh / uninstall / status
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Point the hook installer at a temp settings.json."""
    settings_path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(cli, "_claude_settings_path", lambda: settings_path)
    return settings_path


def test_hook_install_respects_settings_path_override(tmp_path):
    """--settings-path overrides the default location entirely — for
    multi-account setups where claude1/claude2/claude3 each point at a
    different CLAUDE_CONFIG_DIR."""
    other_path = tmp_path / "alt-account" / "settings.json"
    r = CliRunner().invoke(
        cli.main,
        ["claude-code-hook", "install", "--settings-path", str(other_path)],
    )
    assert r.exit_code == 0, r.output
    assert other_path.exists()
    data = json.loads(other_path.read_text(encoding="utf-8"))
    cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd.startswith("clustertrace ensure-dashboard")


def test_hook_install_creates_file_when_missing(temp_settings):
    assert not temp_settings.exists()
    r = CliRunner().invoke(cli.main, ["claude-code-hook", "install"])
    assert r.exit_code == 0, r.output
    data = json.loads(temp_settings.read_text(encoding="utf-8"))
    blocks = data["hooks"]["SessionStart"]
    assert len(blocks) == 1
    cmd = blocks[0]["hooks"][0]["command"]
    assert cmd.startswith("clustertrace ensure-dashboard")
    assert "--port 7777" in cmd
    assert "--idle-shutdown-minutes 15" in cmd


def test_hook_install_preserves_existing_unrelated_hooks(temp_settings):
    """If the user already has hooks, install must merge — not overwrite."""
    temp_settings.parent.mkdir(parents=True, exist_ok=True)
    temp_settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}
            ],
            "Stop": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo bye"}]}
            ],
        }
    }), encoding="utf-8")
    r = CliRunner().invoke(cli.main, ["claude-code-hook", "install"])
    assert r.exit_code == 0, r.output
    data = json.loads(temp_settings.read_text(encoding="utf-8"))
    # Stop hook still there
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo bye"
    # SessionStart now has both echo + clustertrace blocks
    sessions = data["hooks"]["SessionStart"]
    commands = [
        h["command"] for block in sessions for h in (block.get("hooks") or [])
    ]
    assert "echo hi" in commands
    assert any(c.startswith("clustertrace ensure-dashboard") for c in commands)


def test_hook_install_is_idempotent_and_refreshes_options(temp_settings):
    """Running install twice doesn't duplicate; running with new --port refreshes."""
    runner = CliRunner()
    runner.invoke(cli.main, ["claude-code-hook", "install"])
    runner.invoke(cli.main, ["claude-code-hook", "install", "--port", "8123"])
    data = json.loads(temp_settings.read_text(encoding="utf-8"))
    sessions = data["hooks"]["SessionStart"]
    # Still exactly one clustertrace block
    ct_blocks = [
        b for b in sessions
        if any(
            (h.get("command") or "").startswith("clustertrace ensure-dashboard")
            for h in (b.get("hooks") or [])
        )
    ]
    assert len(ct_blocks) == 1
    cmd = ct_blocks[0]["hooks"][0]["command"]
    assert "--port 8123" in cmd  # the refresh landed


def test_hook_uninstall_removes_clustertrace_only(temp_settings):
    """Uninstall must not touch user's other hooks."""
    temp_settings.parent.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    # Seed with both clustertrace AND another hook
    temp_settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}
            ]
        }
    }), encoding="utf-8")
    runner.invoke(cli.main, ["claude-code-hook", "install"])
    runner.invoke(cli.main, ["claude-code-hook", "uninstall"])
    data = json.loads(temp_settings.read_text(encoding="utf-8"))
    sessions = data["hooks"]["SessionStart"]
    commands = [
        h["command"] for block in sessions for h in (block.get("hooks") or [])
    ]
    assert "echo hi" in commands
    assert not any(c.startswith("clustertrace ensure-dashboard") for c in commands)


def test_hook_uninstall_when_nothing_installed_is_noop(temp_settings):
    r = CliRunner().invoke(cli.main, ["claude-code-hook", "uninstall"])
    assert r.exit_code == 0
    assert "no clustertrace hook" in r.output.lower()


def test_hook_install_refuses_corrupt_settings(temp_settings):
    temp_settings.parent.mkdir(parents=True, exist_ok=True)
    temp_settings.write_text("not valid json{", encoding="utf-8")
    r = CliRunner().invoke(cli.main, ["claude-code-hook", "install"])
    assert r.exit_code != 0
    assert "not valid JSON" in r.output


def test_hook_status_reports_installed_state(temp_settings):
    runner = CliRunner()
    r = runner.invoke(cli.main, ["claude-code-hook", "status"])
    assert "NOT INSTALLED" in r.output
    runner.invoke(cli.main, ["claude-code-hook", "install"])
    r = runner.invoke(cli.main, ["claude-code-hook", "status"])
    assert "INSTALLED" in r.output
    # On a fresh test box no dashboard is listening on 7777
    assert "idle" in r.output or "RUNNING" in r.output


# ---------------------------------------------------------------------------
# process module: port_responsive
# ---------------------------------------------------------------------------


def test_port_responsive_false_on_closed_port():
    # Pick a port unlikely to be in use
    assert process.port_responsive(port=55123, timeout_s=0.2) is False


def test_port_responsive_true_when_socket_listening():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert process.port_responsive(port=port, timeout_s=0.5) is True
    finally:
        s.close()


# ---------------------------------------------------------------------------
# ensure-dashboard: idempotent fast path
# ---------------------------------------------------------------------------


def test_ensure_dashboard_idempotent_when_port_bound(monkeypatch):
    """If something is on the port we DO NOT spawn (idempotent)."""
    monkeypatch.setattr(process, "port_responsive", lambda **kw: True)
    spawn_called = {"n": 0}

    def fake_spawn(**kw):
        spawn_called["n"] += 1
        return 999

    monkeypatch.setattr(process, "spawn_dashboard_detached", fake_spawn)
    r = CliRunner().invoke(cli.main, ["ensure-dashboard"])
    assert r.exit_code == 0
    assert spawn_called["n"] == 0
    assert "already running" in r.output.lower()


def test_ensure_dashboard_spawns_when_port_free(monkeypatch):
    monkeypatch.setattr(process, "port_responsive", lambda **kw: False)
    spawn_called = {"n": 0, "args": None}

    def fake_spawn(**kw):
        spawn_called["n"] += 1
        spawn_called["args"] = kw
        return 4242

    monkeypatch.setattr(process, "spawn_dashboard_detached", fake_spawn)
    r = CliRunner().invoke(
        cli.main, ["ensure-dashboard", "--idle-shutdown-minutes", "10", "--port", "9999"]
    )
    assert r.exit_code == 0
    assert spawn_called["n"] == 1
    assert spawn_called["args"]["port"] == 9999
    assert spawn_called["args"]["idle_shutdown_seconds"] == 600
    assert "pid=4242" in r.output


# ---------------------------------------------------------------------------
# Idle-shutdown wiring: the dashboard module reads
# CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS at import. We can't easily test the
# actual auto-exit (requires running a real event loop and waiting 60s+),
# but we can assert the config plumbing reads the env var.
# ---------------------------------------------------------------------------


def test_dashboard_reads_idle_shutdown_env(monkeypatch):
    """The dashboard reads CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS at import time,
    so a reload picks up the env we set."""
    from clustertrace.dashboard import app as dash_app

    monkeypatch.setenv("CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS", "120")
    importlib.reload(dash_app)
    try:
        assert dash_app._IDLE_SHUTDOWN_SECONDS == 120
    finally:
        # Restore the default so other tests don't inherit non-zero idle config.
        monkeypatch.delenv("CLUSTERTRACE_IDLE_SHUTDOWN_SECONDS", raising=False)
        importlib.reload(dash_app)
    assert dash_app._IDLE_SHUTDOWN_SECONDS == 0


def test_dashboard_idle_shutdown_default_is_zero():
    """Manual `clustertrace dashboard` use must not get rugged by idle-shutdown
    — the default config is 0 (disabled)."""
    from clustertrace.dashboard import app as dash_app

    assert dash_app._IDLE_SHUTDOWN_SECONDS == 0
