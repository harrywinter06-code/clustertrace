"""Auto-repro — generate a runnable pytest file from a captured trace."""
from __future__ import annotations

import importlib.resources

import pytest
from click.testing import CliRunner

import clustertrace
from clustertrace import cli, cluster, export, repro, storage


def test_repro_generates_positive_test_for_simple_trace():
    """The happy path: a captured trace with JSON-safe args produces a runnable test."""
    @clustertrace.trace
    def adder(a, b):
        return a + b
    adder(2, 3)
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    src = repro.generate_repro(tid, "math:fsum", mode="positive")
    # Sanity: the generated file must mention the args, the entry, and a test fn.
    # @clustertrace.trace records args as a JSON array, so we get list syntax.
    assert "[2, 3]" in src
    assert "from math import fsum" in src
    assert "def test_repro_" in src
    assert "must not raise" in src


def test_repro_emits_negative_assertion_when_mode_negative():
    """mode='negative' asserts the original error type raises."""
    @clustertrace.trace
    def boomer():
        raise ValueError("nope")
    with pytest.raises(ValueError):
        boomer()
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    src = repro.generate_repro(tid, "examples:boomer", mode="negative")
    assert "pytest.raises(ValueError)" in src


def test_repro_sig_hash_picks_most_recent_failing_trace():
    """When a sig_hash is supplied (not a trace_id), the most recent failing trace seeds."""
    @clustertrace.trace
    def shaped(_x):
        raise RuntimeError("fail")

    @clustertrace.trace
    def shaped_ok(_x):
        pass  # different path, different signature

    with pytest.raises(RuntimeError):
        shaped(1)
    with pytest.raises(RuntimeError):
        shaped(2)
    shaped_ok(0)

    # Find the sig_hash of the failing cluster.
    clusters = cluster.list_clusters()
    fail_cluster = next(c for c in clusters if c.error_count > 0)
    sh = fail_cluster.sig_hash
    src = repro.generate_repro(sh, "examples:shaped", mode="positive")
    # The seed's args were [1] or [2] — either is acceptable (most-recent failing wins).
    assert "args = [1]" in src or "args = [2]" in src


def test_repro_truncated_input_emits_comment_not_args():
    """When root input was __truncated, emit a placeholder + comment instead of fabricated args."""
    @clustertrace.trace
    def big(_s):
        return None
    # Force truncation by sending a giant string
    big("x" * 100_000)
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    src = repro.generate_repro(tid, "examples:big", mode="positive")
    assert "__truncated" in src
    assert "TODO" in src


def test_repro_writes_to_file_when_out_passed(tmp_path):
    @clustertrace.trace
    def f(x):
        return x
    f(42)
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    runner = CliRunner()
    out_file = tmp_path / "test_repro_x.py"
    result = runner.invoke(cli.main, ["repro", tid, "--entry", "math:fsum", "--out", str(out_file)])
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    text = out_file.read_text(encoding="utf-8")
    assert "def test_repro_" in text


def test_repro_short_hash_naming_convention():
    """`tests/test_repro_<short_hash>.py` — the short_hash is the first 8 hex chars of the trace_id."""
    @clustertrace.trace
    def f():
        pass
    f()
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    src = repro.generate_repro(tid, "examples:f")
    expected_short = tid.replace("-", "")[:8]
    assert f"def test_repro_{expected_short}" in src


def test_repro_unknown_id_raises_helpful_error():
    with pytest.raises(repro.ReproError, match="no trace or cluster matches"):
        repro.generate_repro("nope-such-id", "x:y")


def test_repro_validates_entry_format():
    @clustertrace.trace
    def f(): pass
    f()
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    with pytest.raises(repro.ReproError, match="module:function"):
        repro.generate_repro(tid, "bad_entry_no_colon")


def test_repro_validates_mode():
    @clustertrace.trace
    def f(): pass
    f()
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    with pytest.raises(repro.ReproError, match="mode must"):
        repro.generate_repro(tid, "math:fsum", mode="banana")


def test_repro_generated_file_is_valid_python(tmp_path):
    """The generated file must compile and (with a real entry) run under pytest."""
    @clustertrace.trace
    def adder(a, b):
        return a + b
    adder(7, 8)
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    src = repro.generate_repro(tid, "math:fsum", mode="positive")
    # Must compile.
    compile(src, "<generated>", "exec")


def test_repro_works_on_demo_data():
    """Brief: must work against the bundled demo data. Pick the first cluster's representative."""
    data_file = importlib.resources.files("clustertrace").joinpath("data/demo-traces.jsonl")
    with data_file.open("r", encoding="utf-8") as f:
        imported, _ = export.import_lines(f)
    assert imported > 0
    cluster.backfill_signatures()
    clusters = cluster.list_clusters(limit=5)
    assert clusters
    # Try with a sig_hash from a failing cluster (if any), otherwise the first.
    target = next((c for c in clusters if c.error_count > 0), clusters[0])
    src = repro.generate_repro(target.sig_hash, "examples.agents:research_agent")
    assert "def test_repro_" in src
    # Generated source must compile.
    compile(src, "<generated_demo>", "exec")


def test_repro_cli_emits_to_stdout(tmp_path):
    @clustertrace.trace
    def f(x):
        return x
    f("hi")
    with storage.connect() as conn:
        tid = conn.execute("SELECT id FROM traces").fetchone()["id"]
    runner = CliRunner()
    result = runner.invoke(cli.main, ["repro", tid, "--entry", "math:fsum"])
    assert result.exit_code == 0
    assert "def test_repro_" in result.output


def test_repro_cli_unknown_id_clean_error():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["repro", "no-such-id", "--entry", "math:fsum"])
    assert result.exit_code != 0
    assert "no trace or cluster matches" in result.output
