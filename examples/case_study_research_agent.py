"""Maintainer dogfood self-study — synthetic research-assistant agent.

This script is the reproducible workload backing
`docs/case-studies/maintainer-dogfood.md`. It runs a five-step research
agent against a deterministic question set, with realistic failure modes
injected so the cluster page surfaces something meaningful.

Reproducibility: every random draw is seeded from the question + a global
seed. Re-running produces identical clustertrace output. Re-run with
`--seed 7` (or any other int) to confirm the patterns aren't seed-cherry-
picked.

Usage:

    # Before any "fix"
    python examples/case_study_research_agent.py --runs 200 --version v1

    # After the rerank fix
    python examples/case_study_research_agent.py --runs 200 --version v2 \
        --reset

The script is the deliberately-imperfect agent. The point of clustertrace
is to find what's wrong with it from the traces alone.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import time
from collections.abc import Callable

import clustertrace

# --- Question set ----------------------------------------------------------
# Mix of question shapes so failure modes correlate with real-input
# features, not pure noise. 40 distinct questions; the runner picks each
# `--runs` times with a deterministic seed.

QUESTIONS = [
    "What year did the Apollo 11 mission land on the moon?",
    "How many billion dollars did Apple earn in Q3 2024?",
    "What is the boiling point of water at sea level?",
    "Who wrote 'War and Peace'?",
    "When was the Eiffel Tower completed?",
    "How many bones are in the human body?",
    "What is the chemical symbol for gold?",
    "Which year did World War II end?",
    "How tall is Mount Everest in meters?",
    "What is the population of Tokyo as of 2024?",
    "Who painted the Mona Lisa?",
    "What is the speed of light in km/s?",
    "When did the Roman Empire fall?",
    "What is the capital of Australia?",
    "How many continents are there?",
    "Which billionaire founded Tesla?",
    "What year was the Internet invented?",
    "How many planets are in the solar system?",
    "Who discovered penicillin?",
    "What is the deepest ocean trench?",
    "When was the Declaration of Independence signed?",
    "How many sides does a hexagon have?",
    "What is the largest mammal on Earth?",
    "Which country invented paper?",
    "What year was the printing press invented?",
    "How many oceans are there?",
    "Who composed the Ninth Symphony?",
    "What is the freezing point of mercury?",
    "When did dinosaurs go extinct?",
    "How many billion years old is the universe?",
    "What is the smallest country in the world?",
    "Who wrote 'Romeo and Juliet'?",
    "What year did the Cold War end?",
    "How many time zones does Russia span?",
    "What is the longest river in the world?",
    "Who invented the telephone?",
    "When was the United Nations founded?",
    "What is the lightest element on the periodic table?",
    "How many UNESCO World Heritage Sites are there?",
    "What year did SpaceX achieve first orbital launch?",
]


# --- Tool simulations ------------------------------------------------------
# Each "tool" is intentionally fragile in a way that mirrors a real LLM
# agent. The failure rates are tuned to give clustertrace something
# interesting to find without being so broken every cluster collapses
# into one.


class SearchFailed(Exception):
    pass


class RerankFailed(Exception):
    pass


class SynthesisOverBudget(Exception):
    pass


class VerificationUncertain(Exception):
    pass


def _seeded_random(question: str, salt: str, global_seed: int) -> random.Random:
    """Per-question deterministic randomness — re-running gives the same
    traces. The salt lets each tool draw independently."""
    h = hashlib.sha256(f"{global_seed}|{question}|{salt}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def tool_query_rewrite(question: str, *, global_seed: int) -> str:
    """Step 1: rewrite the question into search keywords."""
    rng = _seeded_random(question, "rewrite", global_seed)
    time.sleep(rng.uniform(0.001, 0.003))
    return " ".join(w for w in question.lower().split() if len(w) > 3)[:80]


def tool_web_search(query: str, question: str, *, global_seed: int) -> list[str]:
    """Step 2: simulate a web search. Real failure mode: occasional timeout
    plus a meaningful "thin results" mode (≤2 hits) when the query is short."""
    rng = _seeded_random(question, "search", global_seed)
    time.sleep(rng.uniform(0.002, 0.005))
    # 5% network timeout
    if rng.random() < 0.05:
        raise SearchFailed("upstream timeout (simulated)")
    # If the query is very short (few keywords), return thin results — this
    # is the "real" upstream behavior the v1 agent fails to handle.
    if len(query.split()) <= 3:
        return [f"result about {query}", f"another result about {query}"]
    n = rng.randint(4, 8)
    return [f"result-{i} about {query}" for i in range(n)]


def tool_rerank_v1(results: list[str], question: str, *, global_seed: int) -> list[str]:
    """Step 3 (v1 — broken): rerank picks the top 3 results.

    Latent bug: if `results` has fewer than 3 items, the slice still works
    but a downstream check `len(reranked) == 3` raises RerankFailed. This
    is the deliberate dominant failure mode of the v1 workload.
    """
    rng = _seeded_random(question, "rerank", global_seed)
    time.sleep(rng.uniform(0.001, 0.003))
    rng.shuffle(results)
    reranked = results[:3]
    if len(reranked) != 3:
        raise RerankFailed(
            f"expected exactly 3 candidates after rerank, got {len(reranked)}"
        )
    return reranked


def tool_rerank_v2(results: list[str], question: str, *, global_seed: int) -> list[str]:
    """Step 3 (v2 — fixed): handles thin results gracefully."""
    rng = _seeded_random(question, "rerank", global_seed)
    time.sleep(rng.uniform(0.001, 0.003))
    rng.shuffle(results)
    # Take up to 3, but accept any non-empty list. This is the actual fix
    # implied by the cluster diagnosis in v1.
    reranked = results[:3]
    if not reranked:
        raise RerankFailed("no candidates at all to rerank")
    return reranked


def tool_synthesize(
    reranked: list[str], question: str, *, global_seed: int
) -> str:
    """Step 4: LLM synthesis. Simulates Anthropic call. Fails for very
    long inputs (over-budget) — about 3% of cases."""
    rng = _seeded_random(question, "synth", global_seed)
    time.sleep(rng.uniform(0.003, 0.007))
    payload_size = sum(len(r) for r in reranked) + len(question)
    # Over-budget gate — fires on the longest inputs.
    if payload_size > 180 and rng.random() < 0.40:
        raise SynthesisOverBudget(
            f"input tokens estimated at {payload_size} exceeded limit"
        )
    return f"Answer derived from {len(reranked)} sources for: {question}"


def tool_verify_claim(answer: str, question: str, *, global_seed: int) -> str:
    """Step 5: verify factual claim. Fails when the question contains a
    specific factual anchor word — these need exact verification that the
    mocked verifier can't always provide."""
    rng = _seeded_random(question, "verify", global_seed)
    time.sleep(rng.uniform(0.001, 0.002))
    anchors = ("year", "billion", "many")
    if any(a in question.lower() for a in anchors):
        # Anchored questions fail verification 25% of the time.
        if rng.random() < 0.25:
            raise VerificationUncertain(
                "could not verify factual anchor with sufficient confidence"
            )
    return answer


# --- Agent assembly --------------------------------------------------------


def make_agent(rerank_fn: Callable, *, global_seed: int):
    """Return a `@clustertrace.trace`d agent function bound to the given
    rerank implementation. Each step is wrapped in `clustertrace.span` so
    the trace structure tells a story."""

    @clustertrace.trace(tags={"agent": "research_assistant"})
    def research(question: str) -> str:
        with clustertrace.span("query_rewrite") as _:
            query = tool_query_rewrite(question, global_seed=global_seed)

        with clustertrace.span("web_search"):
            results = tool_web_search(query, question, global_seed=global_seed)

        with clustertrace.span("rerank"):
            reranked = rerank_fn(results, question, global_seed=global_seed)

        with clustertrace.span("synthesize"):
            answer = tool_synthesize(reranked, question, global_seed=global_seed)

        with clustertrace.span("verify_claim"):
            verified = tool_verify_claim(answer, question, global_seed=global_seed)

        return verified

    return research


# --- Runner ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="dogfood case study runner")
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--version",
        choices=("v1", "v2"),
        default="v1",
        help="v1 = unfixed rerank (dominant cluster). v2 = rerank fix.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear the clustertrace DB before running. Used to keep v1 and v2 "
        "results in separate DBs.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override CLUSTERTRACE_DB path. Defaults to a per-version path "
        "under .case-study-db/.",
    )
    args = parser.parse_args()

    db_path = args.db or os.path.abspath(
        os.path.join(".case-study-db", f"{args.version}.db")
    )
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if args.reset and os.path.exists(db_path):
        os.remove(db_path)
    os.environ["CLUSTERTRACE_DB"] = db_path

    # Re-import storage so the env var takes effect for this process.
    from clustertrace import storage

    storage.reset_initialized_cache()

    rerank = tool_rerank_v1 if args.version == "v1" else tool_rerank_v2
    research = make_agent(rerank, global_seed=args.seed)

    rng = random.Random(args.seed)
    qs = [rng.choice(QUESTIONS) for _ in range(args.runs)]

    successes = 0
    failures = 0
    start = time.time()
    for q in qs:
        try:
            research(q)
            successes += 1
        except Exception:
            failures += 1
    elapsed = time.time() - start

    print(f"version={args.version}  runs={args.runs}  db={db_path}")
    print(f"successes={successes}  failures={failures}  "
          f"failure_rate={failures / args.runs:.1%}")
    print(f"elapsed={elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
