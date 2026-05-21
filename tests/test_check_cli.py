"""Assertions + `clustertrace check` CLI.

Coverage:
  - Each assertion kind (success_rate, latency, cost, no_new_traces) passes
    and fails in the obvious cases.
  - `check` exits 0 when everything passes, 1 when anything fails.
  - `--format json` is parseable and contains the right fields.
  - `expected-failure` annotated clusters don't tank the suite by default.
  - The CLI's `--over-last` parser handles both `100` (traces) and `1d` (time).
  - Rule validation rejects malformed inputs at insert time.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from click.testing import CliRunner

import clustertrace
from clustertrace import assertions, cluster, storage
from clustertrace.cli import main as cli


def _seed_trace(
    name: str = "agent",
    status: str = "ok",
    *,
    started: float | None = None,
    duration_s: float = 0.001,
    cost_usd: float | None = None,
) -> str:
    started_at = started if started is not None else time.time()
    tid = f"t-{name}-{int(started_at * 1_000_000)}-{status}-{id(object())}"
    storage.insert_trace(tid, name, started_at)
    storage.insert_span(
        span_id=f"{tid}:root",
        trace_id=tid,
        parent_id=None,
        name=name,
        kind="function",
        started_at=started_at,
    )
    storage.finish_span(f"{tid}:root", started_at + duration_s, "ok")
    storage.insert_span(
        span_id=f"{tid}:step",
        trace_id=tid,
        parent_id=f"{tid}:root",
        name="step",
        kind="function",
        started_at=started_at + 0.0001,
    )
    storage.finish_span(f"{tid}:step", started_at + duration_s * 0.9, "ok")
    storage.finish_trace(tid, started_at + duration_s, status)
    cluster.compute_and_store_signature(tid)
    if cost_usd is not None:
        storage.set_trace_cost(tid, cost_usd)
    return tid


def _seed_cluster(name: str, statuses: list[str], **kwargs) -> str:
    sig_hash: str | None = None
    base = time.time()
    for i, st in enumerate(statuses):
        tid = _seed_trace(name=name, status=st, started=base + i * 0.01, **kwargs)
        if sig_hash is None:
            with storage.connect() as conn:
                row = conn.execute(
                    "SELECT signature FROM traces WHERE id = ?", (tid,)
                ).fetchone()
            sig_hash = cluster.signature_hash(row["signature"])
    assert sig_hash is not None
    return sig_hash


# --- success_rate_above ---------------------------------------------------


def test_success_rate_above_pass() -> None:
    sig = _seed_cluster("a", ["ok"] * 9 + ["error"])
    aid = assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.8, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is True
    assert r.observed == 0.9


def test_success_rate_above_fail() -> None:
    sig = _seed_cluster("a", ["error"] * 7 + ["ok"] * 3)
    aid = assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.9, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is False
    assert r.observed == 0.3


def test_success_rate_with_seconds_window() -> None:
    """`unit='seconds'` means the window slides on wallclock, not trace count."""
    long_ago = time.time() - 3 * 86400
    # Two traces a long time ago, one recent — only the recent counts.
    _seed_trace("a", "error", started=long_ago)
    _seed_trace("a", "error", started=long_ago + 0.01)
    sig = _seed_cluster("a", ["ok"])
    aid = assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 1.0, "over_last": 60, "unit": "seconds"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is True


# --- avg_latency_below ----------------------------------------------------


def test_avg_latency_below_pass() -> None:
    sig = _seed_cluster("a", ["ok"] * 3, duration_s=0.5)  # 500ms each
    aid = assertions.add_assertion(sig, {
        "kind": "avg_latency_below", "ms": 1000.0, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is True


def test_avg_latency_below_fail() -> None:
    sig = _seed_cluster("a", ["ok"] * 3, duration_s=2.0)  # 2000ms each
    aid = assertions.add_assertion(sig, {
        "kind": "avg_latency_below", "ms": 1000.0, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is False


# --- avg_cost_below -------------------------------------------------------


def test_avg_cost_below_pass() -> None:
    sig = _seed_cluster("a", ["ok"] * 3, cost_usd=0.0001)
    aid = assertions.add_assertion(sig, {
        "kind": "avg_cost_below", "usd": 0.01, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is True


def test_avg_cost_below_fail() -> None:
    sig = _seed_cluster("a", ["ok"] * 3, cost_usd=0.05)
    aid = assertions.add_assertion(sig, {
        "kind": "avg_cost_below", "usd": 0.01, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is False


def test_avg_cost_skips_traces_without_cost() -> None:
    """If no traces have cost data, the rule is a vacuous pass — never a
    silent-zero-counts-as-pass bug."""
    sig = _seed_cluster("a", ["ok"] * 3)  # no cost set
    aid = assertions.add_assertion(sig, {
        "kind": "avg_cost_below", "usd": 0.0, "over_last": 10, "unit": "traces"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is True
    assert r.observed is None


# --- no_new_traces --------------------------------------------------------


def test_no_new_traces_pass() -> None:
    # Seed one trace far in the past and confirm a 1-day window finds nothing.
    long_ago = time.time() - 7 * 86400
    tid = _seed_trace("a", "ok", started=long_ago)
    with storage.connect() as conn:
        sig_row = conn.execute(
            "SELECT signature FROM traces WHERE id = ?", (tid,)
        ).fetchone()
    sig = cluster.signature_hash(sig_row["signature"])
    aid = assertions.add_assertion(sig, {
        "kind": "no_new_traces", "over_last": 86400, "unit": "seconds"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is True


def test_no_new_traces_fail() -> None:
    sig = _seed_cluster("a", ["ok"])  # just now
    aid = assertions.add_assertion(sig, {
        "kind": "no_new_traces", "over_last": 86400, "unit": "seconds"
    })
    r = assertions.evaluate_assertion(aid, sig, assertions.list_assertions()[0]["rule"])
    assert r.passed is False


# --- validation -----------------------------------------------------------


def test_rule_validation_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        assertions.add_assertion("x", {"kind": "weird"})


def test_rule_validation_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        assertions.add_assertion("x", {
            "kind": "success_rate_above", "threshold": 2.0, "over_last": 10, "unit": "traces"
        })


# --- check CLI ------------------------------------------------------------


def test_check_exits_zero_when_all_pass() -> None:
    sig = _seed_cluster("a", ["ok"] * 10)
    assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.9, "over_last": 10, "unit": "traces"
    })
    runner = CliRunner()
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 0, result.output


def test_check_exits_nonzero_when_any_fail() -> None:
    sig = _seed_cluster("a", ["error"] * 10)
    assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.9, "over_last": 10, "unit": "traces"
    })
    runner = CliRunner()
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_check_json_format_is_parseable() -> None:
    sig = _seed_cluster("a", ["ok"] * 10)
    assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.9, "over_last": 10, "unit": "traces"
    })
    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--format", "json"])
    payload = json.loads(result.output)
    assert payload["passed"] is True
    assert payload["n_assertions"] == 1
    assert payload["results"][0]["passed"] is True


def test_check_skips_expected_failures_by_default() -> None:
    sig = _seed_cluster("a", ["error"] * 10)
    assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.9, "over_last": 10, "unit": "traces"
    })
    clustertrace.annotate_cluster(sig, status="expected-failure")
    runner = CliRunner()
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 0, result.output
    # The assertion still appears in output — just marked.
    assert "expected" in result.output.lower()


def test_check_count_expected_failures_with_flag() -> None:
    sig = _seed_cluster("a", ["error"] * 10)
    assertions.add_assertion(sig, {
        "kind": "success_rate_above", "threshold": 0.9, "over_last": 10, "unit": "traces"
    })
    clustertrace.annotate_cluster(sig, status="expected-failure")
    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--include-expected-failures"])
    assert result.exit_code == 1


def test_check_with_no_assertions_exits_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 0


# --- assert CLI -----------------------------------------------------------


def test_assert_cli_persists_rule() -> None:
    sig = _seed_cluster("a", ["ok"])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "assert", sig, "--success-rate-above", "0.9", "--over-last", "100"
    ])
    assert result.exit_code == 0, result.output
    assert "saved assertion" in result.output
    rules = assertions.list_assertions()
    assert len(rules) == 1
    assert rules[0]["rule"]["kind"] == "success_rate_above"
    assert rules[0]["rule"]["over_last"] == 100
    assert rules[0]["rule"]["unit"] == "traces"


def test_assert_cli_parses_time_window() -> None:
    sig = _seed_cluster("a", ["ok"])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "assert", sig, "--avg-latency-below", "5000", "--over-last", "1h"
    ])
    assert result.exit_code == 0
    rule = assertions.list_assertions()[0]["rule"]
    assert rule["unit"] == "seconds"
    assert rule["over_last"] == 3600


def test_assert_cli_rejects_multiple_kinds() -> None:
    sig = _seed_cluster("a", ["ok"])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "assert", sig,
        "--success-rate-above", "0.9",
        "--avg-cost-below", "0.01",
        "--over-last", "100",
    ])
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower()


def test_assert_cli_no_new_traces_uses_time_unit() -> None:
    sig = _seed_cluster("a", ["ok"])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "assert", sig, "--no-new-traces", "--over-last", "1d"
    ])
    assert result.exit_code == 0
    rule = assertions.list_assertions()[0]["rule"]
    assert rule == {"kind": "no_new_traces", "over_last": 86400, "unit": "seconds"}


# --- annotate CLI ---------------------------------------------------------


def test_annotate_cli_round_trip() -> None:
    sig = _seed_cluster("a", ["ok"])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "annotate", sig, "--status", "wontfix", "--note", "rate-limited upstream"
    ])
    assert result.exit_code == 0, result.output
    assert clustertrace.get_cluster_annotation(sig)["status"] == "wontfix"


def test_annotate_cli_clear() -> None:
    sig = _seed_cluster("a", ["ok"])
    clustertrace.annotate_cluster(sig, status="priority")
    runner = CliRunner()
    result = runner.invoke(cli, ["annotate", sig, "--status", "clear"])
    assert result.exit_code == 0
    assert clustertrace.get_cluster_annotation(sig) is None


# --- judge CLI ------------------------------------------------------------


def test_judge_cli_runs_no_exceptions_evaluator_default() -> None:
    """Default judge command needs no API key (uses no-exceptions evaluator)."""
    _seed_cluster("a", ["ok", "ok", "ok"])
    # Make sure no env key sneaks in
    os.environ.pop("ANTHROPIC_API_KEY", None)
    runner = CliRunner()
    result = runner.invoke(cli, ["judge", "--samples", "2"])
    assert result.exit_code == 0, result.output
    assert "# Judgment report" in result.output


def test_judge_cli_respects_cost_cap() -> None:
    _seed_cluster("a", ["ok"])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "judge", "--rubric", "did it work?",
        "--samples", "5",
        "--max-cost-usd", "0.0",
        "--model", "claude-haiku-4-5-20251001",
    ])
    assert result.exit_code != 0
    assert "exceeds cap" in result.output
