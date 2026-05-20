"""Populate ~/.agentlog/traces.db with realistic demo runs.

Runs the research agent N times across varied queries. Some succeed, some fail.
Uses Haiku to keep API spend small (~$1-3 for 60 runs).

Usage:
  AGENTLOG_DB=./demo-traces/traces.db python examples/generate_demo_data.py 60
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

# Load .env from this directory or parent if present (developer convenience).
def _load_env() -> None:
    for candidate in [Path(__file__).parent / ".env", Path(__file__).parent.parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit("ANTHROPIC_API_KEY not set. Put it in a .env at the repo root or export it.")

from examples.research_agent import research  # noqa: E402

QUERIES = [
    "sparse attention and hallucination",
    "inference time scaling laws",
    "RLHF drift in long horizon agents",
    "verifier-guided decoding vs self consistency",
    "speculative decoding for long context",
    "toolformer ablation studies",
    "agent failure modes",
    "small models with best of N sampling",
    "training-free reasoning improvements",
    "draft target speculative pairs",
]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    random.seed(seed)

    ok = 0
    err = 0
    t0 = time.time()
    for i in range(n):
        q = random.choice(QUERIES)
        try:
            research(q)
            ok += 1
        except Exception as e:
            err += 1
            print(f"[{i+1}/{n}] FAIL ({type(e).__name__}): {e}")
        else:
            print(f"[{i+1}/{n}] ok")
    dur = time.time() - t0
    print(f"\nDone in {dur:.1f}s — {ok} ok, {err} failed ({err / n * 100:.0f}% failure rate).")


if __name__ == "__main__":
    main()
