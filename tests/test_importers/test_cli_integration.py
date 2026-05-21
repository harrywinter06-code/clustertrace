"""End-to-end CLI integration tests for `clustertrace import --from <source>`.

We invoke the Click command in-process via `CliRunner` because spawning the
real `clustertrace` script would also spawn a fresh process with a different
$CLUSTERTRACE_DB, defeating the per-test SQLite isolation in conftest.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from clustertrace.cli import main

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_cli_import_langfuse_from_file_then_stats() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["import", "--from", "langfuse", "--file", str(FIXTURES / "import_langfuse.json")],
    )
    assert result.exit_code == 0, result.output
    assert "imported 2 traces" in result.output

    stats = runner.invoke(main, ["stats"])
    assert stats.exit_code == 0
    assert "traces: 2" in stats.output


def test_cli_import_phoenix_from_stdin() -> None:
    runner = CliRunner()
    payload = (FIXTURES / "import_phoenix.jsonl").read_text(encoding="utf-8")
    result = runner.invoke(main, ["import", "--from", "phoenix"], input=payload)
    assert result.exit_code == 0, result.output
    assert "imported 1 traces" in result.output


def test_cli_import_langsmith_then_otel_accumulates() -> None:
    runner = CliRunner()
    r1 = runner.invoke(
        main,
        ["import", "--from", "langsmith", "--file", str(FIXTURES / "import_langsmith.json")],
    )
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(
        main,
        ["import", "--from", "otel", "--file", str(FIXTURES / "import_otel.json")],
    )
    assert r2.exit_code == 0, r2.output

    stats = runner.invoke(main, ["stats"])
    assert "traces: 2" in stats.output  # 1 langsmith + 1 otel


def test_cli_unknown_source_exits_with_code_2() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["import", "--from", "helicone"])
    assert result.exit_code == 2
    # Error message goes to stderr in click; CliRunner mixes streams by default.
    combined = (result.output or "") + (result.stderr_bytes.decode() if result.stderr_bytes else "")
    assert "unknown source" in combined
    assert "langfuse" in combined and "phoenix" in combined


def test_cli_import_help_lists_all_sources() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["import", "--help"])
    assert result.exit_code == 0
    for source in ("langfuse", "phoenix", "langsmith", "otel"):
        assert source in result.output


def test_cli_import_native_jsonl_still_works() -> None:
    """Backward-compat: existing `clustertrace export | clustertrace import` flow."""
    runner = CliRunner()
    # Generate native-format data by running the langfuse import then exporting.
    imp = runner.invoke(
        main,
        ["import", "--from", "langfuse", "--file", str(FIXTURES / "import_langfuse.json")],
    )
    assert imp.exit_code == 0, imp.output
    exported = runner.invoke(main, ["export", "--all"])
    assert exported.exit_code == 0, exported.output

    # Re-import the export into the same DB — should skip everything since the
    # IDs already exist (proving the native code path runs).
    reimport = runner.invoke(main, ["import"], input=exported.stdout)
    assert reimport.exit_code == 0, reimport.output
    assert "imported 0" in reimport.output
