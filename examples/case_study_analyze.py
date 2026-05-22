"""Pull diagnostic numbers from a finished case-study run.

Outputs the exact numbers the case-study markdown cites — so any reviewer
can re-run this and verify nothing was fabricated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", choices=("v1", "v2"), required=True)
    parser.add_argument("--top-clusters", type=int, default=5)
    args = parser.parse_args()

    db_path = os.path.abspath(
        os.path.join(".case-study-db", f"{args.version}.db")
    )
    os.environ["CLUSTERTRACE_DB"] = db_path

    from clustertrace import cluster, storage  # noqa: F401

    storage.reset_initialized_cache()

    # Backfill signatures so list_clusters has something to group on.
    n_backfilled = cluster.backfill_signatures()

    clusters = cluster.list_clusters(limit=args.top_clusters)
    summary = cluster.failure_summary(limit_clusters=args.top_clusters)

    print(f"=== {args.version}  db={db_path} ===")
    print(f"backfilled {n_backfilled} signatures")
    print()
    print(f"traces_total    = {summary['traces_total']}")
    print(f"traces_failed   = {summary['traces_failed']}")
    print(f"overall_rate    = {summary['overall_failure_rate']:.1%}")
    print(f"n_clusters      = {summary['n_clusters']}")
    print()
    print(f"--- top {args.top_clusters} clusters (by count) ---")
    for i, c in enumerate(clusters, 1):
        pattern_str = " ->".join(f"{n}:{s}" for n, s in c.pattern) or "(empty)"
        print(f"#{i}  count={c.count:>3}  errors={c.error_count:>3}  "
              f"rate={c.error_rate:.0%}  sig_hash={c.sig_hash}")
        print(f"     {pattern_str}")
    print()
    print(f"--- common failure prefix across ALL failed traces ---")
    cp = summary["common_failure_prefix"]
    if cp:
        print("  " + " ->".join(f"{p['name']}:{p['status']}" for p in cp))
    else:
        print("  (none — failures don't share a prefix)")
    print()
    print(f"--- top failing nodes ---")
    for node in summary["top_failing_nodes"][:5]:
        print(f"  {node['name']}:{node['status']}  ×{node['count']}")
    print()

    # Same data in JSON so the markdown can pull exact values.
    json_out = {
        "version": args.version,
        "traces_total": summary["traces_total"],
        "traces_failed": summary["traces_failed"],
        "overall_failure_rate": summary["overall_failure_rate"],
        "n_clusters": summary["n_clusters"],
        "clusters": [
            {
                "rank": i,
                "sig_hash": c.sig_hash,
                "count": c.count,
                "errors": c.error_count,
                "error_rate": c.error_rate,
                "pattern": [{"name": n, "status": s} for n, s in c.pattern],
            }
            for i, c in enumerate(clusters, 1)
        ],
        "common_failure_prefix": cp,
        "top_failing_nodes": summary["top_failing_nodes"][:5],
    }
    out_path = os.path.abspath(
        os.path.join(".case-study-db", f"{args.version}-stats.json")
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(json_out, f, indent=2)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
