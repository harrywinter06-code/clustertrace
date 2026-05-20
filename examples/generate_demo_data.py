"""Populate ~/.agentlog/traces.db (or $AGENTLOG_DB) with three agent topologies.

Runs each agent N times with varied inputs so the dashboard has real
clusters and failure patterns to show. Uses Haiku 4.5 — ~$3-6 for 300 runs.

Usage:
  AGENTLOG_DB=./demo-traces/traces.db python examples/generate_demo_data.py 100
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path


def _load_env() -> None:
    for candidate in [Path(__file__).parent / ".env", Path(__file__).parent.parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if not os.environ.get(k):
                    os.environ[k] = v


_load_env()

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit("ANTHROPIC_API_KEY not set. Put it in a .env at the repo root or export it.")

sys.path.insert(0, str(Path(__file__).parent))
from agents import (  # noqa: E402
    rag_agent,
    research_agent,
    tool_use_agent,
)

RESEARCH_QUERIES = [
    "sparse attention and hallucination",
    "inference time scaling laws",
    "RLHF drift in long horizon agents",
    "verifier-guided decoding vs self consistency",
    "speculative decoding for long context",
    "small models with best of N sampling",
]

RAG_QUERIES = [
    # On-topic — should retrieve relevant docs
    "How does Postgres handle concurrent writes?",
    "What does Redis use for storage?",
    "How does Kafka enable parallel consumption?",
    "Why is ClickHouse fast for analytics?",
    # Off-topic — likely to fail at rerank
    "What's the airspeed of an unladen swallow?",
    "Best Italian restaurants in Edinburgh",
    "Recipe for sourdough starter",
]

TOOL_USE_TASKS = [
    "Find the population of Edinburgh and double it",
    "Compute 2+2*5 and look up alpha",
    "Search for Kubernetes etcd notes and compute 100/4",
    "Look up beta and add three",
    "Tell me a joke and multiply 7 by 8",
]


def main() -> None:
    per_agent = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    random.seed(seed)

    plan = [
        ("research", research_agent, RESEARCH_QUERIES),
        ("rag", rag_agent, RAG_QUERIES),
        ("tool_use", tool_use_agent, TOOL_USE_TASKS),
    ]

    total = per_agent * len(plan)
    ok = err = 0
    t0 = time.time()
    i = 0
    for name, fn, queries in plan:
        for _ in range(per_agent):
            i += 1
            q = random.choice(queries)
            try:
                fn(q)
                ok += 1
                status = "ok"
            except Exception as e:
                err += 1
                status = f"FAIL {type(e).__name__}"
            print(f"[{i}/{total}] {name:10s} {status}")
    dur = time.time() - t0
    print(f"\nDone in {dur:.1f}s — {ok} ok, {err} failed ({err / total * 100:.0f}%).")


if __name__ == "__main__":
    main()
