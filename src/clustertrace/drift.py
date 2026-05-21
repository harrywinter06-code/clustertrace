"""Cluster drift detection.

Compares per-cluster failure rates between two time windows: a *current*
window ending at now, and a *compare* window of equal length ending where
the current one begins.

Returned drifts are sorted by `abs(delta)` descending. To keep the signal
above noise, only clusters with `n >= 3` traces in the current window are
included.

No new schema — we read directly from `traces.signature` + `traces.status` +
`traces.started_at`. The same `mode='ordered'` signature column that powers
the rest of the cluster UI is used here.
"""
from __future__ import annotations

import time
from typing import Any

from clustertrace import cluster, storage

# Statistical floor: clusters with fewer than this many traces in the
# *current* window are excluded — a 1-trace, 1-error cluster isn't a drift,
# it's noise.
_MIN_CURRENT_N = 3


def compute_drift(
    window_seconds: float, compare_seconds: float, now: float | None = None
) -> dict[str, Any]:
    """Return per-cluster failure-rate change between two adjacent time windows.

    The current window is `[now - window_seconds, now]`.
    The compare window is `[now - window_seconds - compare_seconds, now - window_seconds]`.

    Sorted by `abs(delta)` descending. Only clusters with `>= 3` traces in the
    current window appear.
    """
    end = now if now is not None else time.time()
    current_start = end - window_seconds
    compare_end = current_start
    compare_start = current_start - compare_seconds

    with storage.connect() as conn:
        current_rows = conn.execute(
            """SELECT signature,
                      COUNT(*) AS n,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs
               FROM traces
               WHERE signature IS NOT NULL
                 AND status != 'running'
                 AND started_at >= ? AND started_at < ?
               GROUP BY signature""",
            (current_start, end),
        ).fetchall()
        compare_rows = conn.execute(
            """SELECT signature,
                      COUNT(*) AS n,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs
               FROM traces
               WHERE signature IS NOT NULL
                 AND status != 'running'
                 AND started_at >= ? AND started_at < ?
               GROUP BY signature""",
            (compare_start, compare_end),
        ).fetchall()
        current_total = conn.execute(
            """SELECT COUNT(*) FROM traces
               WHERE started_at >= ? AND started_at < ? AND status != 'running'""",
            (current_start, end),
        ).fetchone()[0]
        compare_total = conn.execute(
            """SELECT COUNT(*) FROM traces
               WHERE started_at >= ? AND started_at < ? AND status != 'running'""",
            (compare_start, compare_end),
        ).fetchone()[0]

    by_sig_current: dict[str, dict[str, int]] = {
        r["signature"]: {"n": r["n"], "errs": r["errs"] or 0} for r in current_rows
    }
    by_sig_compare: dict[str, dict[str, int]] = {
        r["signature"]: {"n": r["n"], "errs": r["errs"] or 0} for r in compare_rows
    }

    drifts: list[dict[str, Any]] = []
    for sig, cur in by_sig_current.items():
        if cur["n"] < _MIN_CURRENT_N:
            continue
        prev = by_sig_compare.get(sig, {"n": 0, "errs": 0})
        cur_rate = cur["errs"] / cur["n"] if cur["n"] else 0.0
        prev_rate = prev["errs"] / prev["n"] if prev["n"] else 0.0
        delta = cur_rate - prev_rate
        if abs(delta) < 1e-9:
            # No movement — skip to keep the response small.
            continue
        drifts.append(
            {
                "sig_hash": cluster.signature_hash(sig),
                "signature": sig,
                "pattern": [{"name": n, "status": s} for n, s in cluster._decode_pattern(sig)],
                "current": {"n": cur["n"], "errs": cur["errs"], "rate": cur_rate},
                "previous": {"n": prev["n"], "errs": prev["errs"], "rate": prev_rate},
                "delta": delta,
                "direction": "regressed" if delta > 0 else "improved",
            }
        )
    drifts.sort(key=lambda d: -abs(d["delta"]))
    return {
        "current_window": {"start": current_start, "end": end, "trace_count": current_total},
        "compare_window": {
            "start": compare_start,
            "end": compare_end,
            "trace_count": compare_total,
        },
        "drifts": drifts,
    }
