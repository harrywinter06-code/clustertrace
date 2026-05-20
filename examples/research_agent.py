"""A small multi-step research agent that exercises agentlog.

Tools:
  - search_papers(query): returns fake paper hits
  - fetch_paper(paper_id): returns paper text (sometimes fails)
  - extract_claims(text): asks Haiku to pull claims out
  - verify_claim(claim): asks Haiku to rate a claim
  - summarize(claims): asks Haiku to write a final summary

The agent loops: plan → search → fetch → extract → verify → summarize.
Some seeds will trigger fetch_paper to fail (network-ish), some claims will
fail verification, so the demo data shows a realistic mix of pass/fail traces.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

from anthropic import Anthropic

import agentlog

MODEL = "claude-haiku-4-5-20251001"

_client = agentlog.wrap_anthropic(Anthropic())


@dataclass
class Paper:
    id: str
    title: str
    body: str


_PAPER_LIBRARY = {
    "p001": Paper("p001", "Sparse Attention Reduces Hallucination",
                  "We trained a 7B model with sparse attention. On TruthfulQA, hallucination drops 12%."),
    "p002": Paper("p002", "Inference-Time Scaling Beats Bigger Models",
                  "Best-of-N at inference time recovers ~70% of the gap between a 7B and a 70B model on math benchmarks."),
    "p003": Paper("p003", "RLHF Drift in Long-Horizon Agents",
                  "Agents trained with RLHF on short tasks regress on long-horizon planning. We measured a 4.1x error rate increase."),
    "p004": Paper("p004", "Toolformer with No Tools",
                  "Removing tool access from Toolformer at eval time slightly improves accuracy. Possibly an artifact."),
    "p005": Paper("p005", "Self-Consistency vs Verifier-Guided Decoding",
                  "Verifier-guided sampling beats naive self-consistency on GSM8K by 6 points but doubles cost."),
    "p006": Paper("p006", "Speculative Decoding for Long Context",
                  "Draft-target speculative decoding sees a 1.8x speedup on 32k-context prompts with no quality loss."),
}


class ToolFailure(Exception):
    pass


@agentlog.trace
def search_papers(query: str) -> list[str]:
    """Pretend to search; return paper IDs that vaguely match the query."""
    agentlog.tool_call("search_papers", args={"query": query}, result=list(_PAPER_LIBRARY.keys()))
    # always return up to 3 paper ids
    ids = list(_PAPER_LIBRARY.keys())
    random.shuffle(ids)
    return ids[:3]


@agentlog.trace
def fetch_paper(paper_id: str) -> str:
    """Fetch the paper body. ~20% of calls 'fail' to simulate flaky I/O."""
    if random.random() < 0.20:
        err = ToolFailure(f"fetch timed out for {paper_id}")
        agentlog.tool_call("fetch_paper", args={"paper_id": paper_id}, error=err)
        raise err
    paper = _PAPER_LIBRARY.get(paper_id)
    if paper is None:
        err = ToolFailure(f"unknown paper id: {paper_id}")
        agentlog.tool_call("fetch_paper", args={"paper_id": paper_id}, error=err)
        raise err
    agentlog.tool_call("fetch_paper", args={"paper_id": paper_id}, result={"chars": len(paper.body)})
    return paper.body


@agentlog.trace
def extract_claims(text: str) -> list[str]:
    """Ask Haiku to extract a few claims."""
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=200,
        system="You extract factual claims from research abstracts. Output each claim on its own line, no preamble.",
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text if resp.content else ""
    claims = [c.strip("-• ").strip() for c in raw.splitlines() if c.strip()]
    return claims[:4]


@agentlog.trace
def verify_claim(claim: str) -> dict:
    """Ask Haiku to verify; ~25% chance we force-fail it for demo realism."""
    if random.random() < 0.25:
        raise ToolFailure(f"verifier inconclusive for: {claim[:40]}")
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=80,
        system="Reply with exactly one word: 'plausible' or 'doubtful'.",
        messages=[{"role": "user", "content": f"Claim: {claim}"}],
    )
    verdict = (resp.content[0].text if resp.content else "").strip().lower()
    return {"claim": claim, "verdict": verdict}


@agentlog.trace
def summarize(verdicts: list[dict]) -> str:
    """Ask Haiku to summarize verdicts into a one-paragraph note."""
    bullet = "\n".join(f"- {v['claim']} → {v.get('verdict','?')}" for v in verdicts)
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=200,
        system="Summarize these claim verdicts in 2 sentences.",
        messages=[{"role": "user", "content": bullet}],
    )
    return (resp.content[0].text if resp.content else "").strip()


@agentlog.trace
def research(query: str) -> str:
    """Top-level entrypoint: plan → search → fetch → extract → verify → summarize."""
    with agentlog.span("plan", query=query):
        pass  # in a real agent we'd ask the LLM to plan

    paper_ids = search_papers(query)

    all_claims: list[str] = []
    with agentlog.span("research_loop", n_papers=len(paper_ids)):
        for pid in paper_ids:
            try:
                body = fetch_paper(pid)
            except ToolFailure:
                continue
            claims = extract_claims(body)
            all_claims.extend(claims)

    if not all_claims:
        raise ToolFailure("no claims extracted — all fetches failed")

    verdicts: list[dict] = []
    for c in all_claims[:5]:
        try:
            verdicts.append(verify_claim(c))
        except ToolFailure as e:
            verdicts.append({"claim": c, "verdict": "error", "error": str(e)})

    if not any(v.get("verdict") in ("plausible", "doubtful") for v in verdicts):
        raise ToolFailure("all verifications failed")

    return summarize(verdicts)


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY before running this example.")
    out = research("inference-time scaling and long-horizon agents")
    print("\n=== summary ===\n" + out)
