"""Cluster pass/fail assertions — persisted rules surfaced by `clustertrace check`.

A rule is a JSON blob of the form `{"kind": <kind>, ...}`. Supported kinds:

  - `success_rate_above`:
      `{"kind": "success_rate_above", "threshold": 0.9, "over_last": 100, "unit": "traces"}`
      Pass if (1 - error_rate) over the last N traces in the cluster >= threshold.
      `unit` is `traces` (default) or `seconds`.

  - `avg_latency_below`:
      `{"kind": "avg_latency_below", "ms": 5000.0, "over_last": 100, "unit": "traces"}`
      Pass if mean trace duration (ms) over the last N traces <= threshold.

  - `avg_cost_below`:
      `{"kind": "avg_cost_below", "usd": 0.01, "over_last": 100, "unit": "traces"}`
      Pass if mean cost_usd over the last N traces <= threshold. Skips traces
      with no cost data rather than counting them as 0 — that would
      flatter clusters that never report cost.

  - `no_new_traces`:
      `{"kind": "no_new_traces", "over_last": 86400, "unit": "seconds"}`
      Pass if the cluster has no traces in the last N seconds. Used to
      catch deprecated paths re-appearing.

The assertion syntax for the CLI was chosen to match how engineers
already think about regressions: `--success-rate-above 0.9 --over-last 100`
reads left-to-right and the inverse case `--avg-latency-below 5000` does
too. Alternatives considered: `--ge success_rate 0.9` (parses as data, not
intent — harder to read); a small DSL like `success_rate >= 0.9 over 100`
(too much surface for v0.8).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from clustertrace import annotations, storage

# Allowed unit literals — keep small, every kind picks what it supports.
_VALID_UNITS = {"traces", "seconds"}


# --- Persisting rules -----------------------------------------------------


def add_assertion(sig_hash: str, rule: dict[str, Any]) -> int:
    """Persist a rule. Validates `kind` + required fields before insert."""
    _validate_rule(rule)
    return storage.insert_cluster_assertion(sig_hash, rule, time.time())


def list_assertions() -> list[dict[str, Any]]:
    return storage.list_cluster_assertions()


def delete_assertion(assertion_id: int) -> bool:
    return storage.delete_cluster_assertion(assertion_id)


def _validate_rule(rule: dict[str, Any]) -> None:
    kind = rule.get("kind")
    if kind == "success_rate_above":
        if "threshold" not in rule:
            raise ValueError("success_rate_above requires `threshold`")
        if not (0.0 <= float(rule["threshold"]) <= 1.0):
            raise ValueError("threshold must be between 0 and 1")
        _validate_window(rule)
    elif kind == "avg_latency_below":
        if "ms" not in rule:
            raise ValueError("avg_latency_below requires `ms`")
        if float(rule["ms"]) <= 0:
            raise ValueError("ms must be > 0")
        _validate_window(rule)
    elif kind == "avg_cost_below":
        if "usd" not in rule:
            raise ValueError("avg_cost_below requires `usd`")
        if float(rule["usd"]) < 0:
            raise ValueError("usd must be >= 0")
        _validate_window(rule)
    elif kind == "no_new_traces":
        if "over_last" not in rule:
            raise ValueError("no_new_traces requires `over_last`")
        if rule.get("unit", "seconds") != "seconds":
            raise ValueError("no_new_traces unit must be `seconds`")
    else:
        raise ValueError(
            f"unknown assertion kind {kind!r}; "
            "supported: success_rate_above, avg_latency_below, avg_cost_below, no_new_traces"
        )


def _validate_window(rule: dict[str, Any]) -> None:
    if "over_last" not in rule:
        raise ValueError("rule requires `over_last`")
    if int(rule["over_last"]) <= 0:
        raise ValueError("over_last must be > 0")
    unit = rule.get("unit", "traces")
    if unit not in _VALID_UNITS:
        raise ValueError(f"unit must be one of {sorted(_VALID_UNITS)}")


# --- Evaluating rules ------------------------------------------------------


@dataclass
class AssertionResult:
    assertion_id: int
    sig_hash: str
    rule: dict[str, Any]
    passed: bool
    observed: float | int | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "sig_hash": self.sig_hash,
            "rule": self.rule,
            "passed": self.passed,
            "observed": self.observed,
            "reason": self.reason,
        }


def _matching_signatures(sig_hash: str) -> list[str]:
    """All stored signatures that hash to `sig_hash`. Usually 1."""
    from clustertrace import cluster as _cluster

    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT signature FROM traces WHERE signature IS NOT NULL"
        ).fetchall()
    return [r["signature"] for r in rows if _cluster.signature_hash(r["signature"]) == sig_hash]


def _trace_window(sig_hash: str, rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the slice of traces this rule applies to.

    Trace-units window: most-recent N traces in the cluster.
    Seconds-units window: traces started within the last N seconds.
    """
    signatures = _matching_signatures(sig_hash)
    if not signatures:
        return []
    placeholders = ",".join("?" for _ in signatures)
    unit = rule.get("unit", "traces")
    over_last = int(rule["over_last"])
    with storage.connect() as conn:
        if unit == "traces":
            rows = conn.execute(
                f"""SELECT id, status, started_at, ended_at, cost_usd
                    FROM traces
                    WHERE signature IN ({placeholders}) AND status != 'running'
                    ORDER BY started_at DESC LIMIT ?""",
                (*signatures, over_last),
            ).fetchall()
        else:  # seconds
            cutoff = time.time() - over_last
            rows = conn.execute(
                f"""SELECT id, status, started_at, ended_at, cost_usd
                    FROM traces
                    WHERE signature IN ({placeholders})
                      AND started_at >= ?
                      AND status != 'running'
                    ORDER BY started_at DESC""",
                (*signatures, cutoff),
            ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def evaluate_assertion(assertion_id: int, sig_hash: str, rule: dict[str, Any]) -> AssertionResult:
    """Evaluate one rule, return an AssertionResult with pass/fail + observed value."""
    kind = rule.get("kind")
    if kind == "no_new_traces":
        signatures = _matching_signatures(sig_hash)
        if not signatures:
            return AssertionResult(
                assertion_id=assertion_id,
                sig_hash=sig_hash,
                rule=rule,
                passed=True,
                observed=0,
                reason="cluster has no traces at all",
            )
        cutoff = time.time() - int(rule["over_last"])
        placeholders = ",".join("?" for _ in signatures)
        with storage.connect() as conn:
            n = conn.execute(
                f"""SELECT COUNT(*) FROM traces
                    WHERE signature IN ({placeholders}) AND started_at >= ?""",
                (*signatures, cutoff),
            ).fetchone()[0]
        passed = n == 0
        return AssertionResult(
            assertion_id=assertion_id,
            sig_hash=sig_hash,
            rule=rule,
            passed=passed,
            observed=n,
            reason=(
                f"cluster has {n} traces in the last {int(rule['over_last'])}s"
                if not passed else
                f"no traces in the last {int(rule['over_last'])}s"
            ),
        )

    rows = _trace_window(sig_hash, rule)
    n = len(rows)
    if n == 0:
        return AssertionResult(
            assertion_id=assertion_id,
            sig_hash=sig_hash,
            rule=rule,
            passed=True,
            observed=None,
            reason="no traces in window — vacuous pass",
        )

    if kind == "success_rate_above":
        ok = sum(1 for r in rows if r["status"] == "ok")
        rate = ok / n
        threshold = float(rule["threshold"])
        passed = rate >= threshold
        return AssertionResult(
            assertion_id=assertion_id,
            sig_hash=sig_hash,
            rule=rule,
            passed=passed,
            observed=round(rate, 4),
            reason=(
                f"success rate {rate:.2%} {'>=' if passed else '<'} threshold {threshold:.2%} "
                f"(over {n} traces)"
            ),
        )

    if kind == "avg_latency_below":
        durations_ms = [
            (r["ended_at"] - r["started_at"]) * 1000.0
            for r in rows
            if r["ended_at"] is not None and r["started_at"] is not None
        ]
        if not durations_ms:
            return AssertionResult(
                assertion_id=assertion_id,
                sig_hash=sig_hash,
                rule=rule,
                passed=True,
                observed=None,
                reason="no traces with both start and end time — vacuous pass",
            )
        avg = sum(durations_ms) / len(durations_ms)
        threshold = float(rule["ms"])
        passed = avg <= threshold
        return AssertionResult(
            assertion_id=assertion_id,
            sig_hash=sig_hash,
            rule=rule,
            passed=passed,
            observed=round(avg, 2),
            reason=(
                f"avg latency {avg:.1f}ms {'<=' if passed else '>'} threshold {threshold:.1f}ms "
                f"(over {len(durations_ms)} traces)"
            ),
        )

    if kind == "avg_cost_below":
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        if not costs:
            return AssertionResult(
                assertion_id=assertion_id,
                sig_hash=sig_hash,
                rule=rule,
                passed=True,
                observed=None,
                reason="no traces with cost data — vacuous pass",
            )
        avg = sum(costs) / len(costs)
        threshold = float(rule["usd"])
        passed = avg <= threshold
        return AssertionResult(
            assertion_id=assertion_id,
            sig_hash=sig_hash,
            rule=rule,
            passed=passed,
            observed=round(avg, 6),
            reason=(
                f"avg cost ${avg:.6f} {'<=' if passed else '>'} threshold ${threshold:.6f} "
                f"(over {len(costs)} traces)"
            ),
        )

    raise ValueError(f"unknown assertion kind {kind!r}")


def evaluate_all() -> list[AssertionResult]:
    """Evaluate every persisted assertion, in id order."""
    return [
        evaluate_assertion(a["id"], a["sig_hash"], a["rule"])
        for a in list_assertions()
    ]


def check(*, exclude_expected_failures: bool = True) -> tuple[list[AssertionResult], bool]:
    """Run every assertion and return (results, all_passed).

    By default, assertions against clusters annotated as `expected-failure`
    are still evaluated but their failures don't tank the suite — the
    annotation is the user's explicit "I know about this one". Pass
    `exclude_expected_failures=False` to make them count.
    """
    results = evaluate_all()
    expected = annotations.expected_failure_sig_hashes() if exclude_expected_failures else set()
    binding_failures = [
        r for r in results if not r.passed and r.sig_hash not in expected
    ]
    return results, not binding_failures
