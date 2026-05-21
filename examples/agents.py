"""Three small agent topologies used by the demo data generator.

Each agent calls Haiku 4.5 via wrap_anthropic and exercises a different
shape of execution so the clusters page has a story to tell:

- research_agent: messy retrieve→fetch→extract→verify→summarize chain
  with random flakiness on fetches and verifications.
- rag_agent: deterministic retrieve→rerank→answer flow; failures cluster
  on the reranker when the retrieved docs are off-topic.
- tool_use_agent: planner→fixed-order tool calls→synthesize; failures
  cluster on calculator argument errors.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from anthropic import Anthropic

import clustertrace

MODEL = "claude-haiku-4-5-20251001"
_client = clustertrace.wrap_anthropic(Anthropic())


class ToolFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# 1. research_agent — variable-length, messy, lots of intermediate failures
# ---------------------------------------------------------------------------

@dataclass
class Paper:
    id: str
    body: str


_PAPERS = {
    "p001": Paper("p001", "Sparse attention reduces hallucination by 12% on TruthfulQA."),
    "p002": Paper("p002", "Best-of-N at inference time recovers 70% of the 7B/70B gap."),
    "p003": Paper("p003", "RLHF on short tasks causes 4.1x error rate on long-horizon planning."),
    "p004": Paper("p004", "Toolformer without tools at eval time slightly improves accuracy."),
    "p005": Paper("p005", "Verifier-guided sampling beats self-consistency by 6 points on GSM8K."),
    "p006": Paper("p006", "Draft-target speculative decoding gives 1.8x speedup at 32k context."),
}


@clustertrace.trace
def search_papers(query: str) -> list[str]:
    ids = list(_PAPERS.keys())
    random.shuffle(ids)
    return ids[:3]


@clustertrace.trace
def fetch_paper(paper_id: str) -> str:
    if random.random() < 0.20:
        raise ToolFailure(f"fetch timed out for {paper_id}")
    paper = _PAPERS.get(paper_id)
    if paper is None:
        raise ToolFailure(f"unknown paper id: {paper_id}")
    return paper.body


@clustertrace.trace
def extract_claims(text: str) -> list[str]:
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=200,
        system="Extract factual claims from this text, one per line. No preamble.",
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text if resp.content else ""
    return [c.strip("-• ").strip() for c in raw.splitlines() if c.strip()][:4]


@clustertrace.trace
def verify_claim(claim: str) -> dict:
    if random.random() < 0.25:
        raise ToolFailure(f"verifier inconclusive for: {claim[:40]}")
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=20,
        system="Reply with exactly one word: 'plausible' or 'doubtful'.",
        messages=[{"role": "user", "content": f"Claim: {claim}"}],
    )
    verdict = (resp.content[0].text if resp.content else "").strip().lower()
    return {"claim": claim, "verdict": verdict}


@clustertrace.trace
def summarize_research(verdicts: list[dict]) -> str:
    bullet = "\n".join(f"- {v['claim']} → {v.get('verdict','?')}" for v in verdicts)
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=120,
        system="Summarize in 1 sentence.",
        messages=[{"role": "user", "content": bullet}],
    )
    return (resp.content[0].text if resp.content else "").strip()


@clustertrace.trace(tags={"agent": "research"})
def research_agent(query: str) -> str:
    clustertrace.tag("query", query[:40])
    paper_ids = search_papers(query)
    all_claims: list[str] = []
    for pid in paper_ids:
        try:
            body = fetch_paper(pid)
        except ToolFailure:
            continue
        all_claims.extend(extract_claims(body))
    if not all_claims:
        raise ToolFailure("no claims extracted")
    verdicts: list[dict] = []
    for c in all_claims[:4]:
        try:
            verdicts.append(verify_claim(c))
        except ToolFailure as e:
            verdicts.append({"claim": c, "verdict": "error", "error": str(e)})
    if not any(v.get("verdict") in ("plausible", "doubtful") for v in verdicts):
        raise ToolFailure("all verifications failed")
    return summarize_research(verdicts)


# ---------------------------------------------------------------------------
# 2. rag_agent — deterministic 3-step pipeline; failures cluster on reranker
# ---------------------------------------------------------------------------

_RAG_CORPUS = [
    "Postgres uses MVCC for concurrent reads/writes without blocking.",
    "Redis is single-threaded but extremely fast for in-memory ops.",
    "Kubernetes uses etcd as a distributed key-value store.",
    "Kafka partitions topics for parallel consumption.",
    "ClickHouse stores columns separately for analytical queries.",
    "SQLite serializes writes through a single writer at a time.",
]


@clustertrace.trace
def retrieve(query: str) -> list[str]:
    """Pretend retrieval: returns 3 random docs (some relevant, some not)."""
    return random.sample(_RAG_CORPUS, 3)


@clustertrace.trace
def rerank(query: str, docs: list[str]) -> list[str]:
    """Asks Haiku to rerank; flaky when retrieved docs are clearly off-topic."""
    relevant = sum(1 for d in docs if any(w in d.lower() for w in query.lower().split() if len(w) > 3))
    if relevant == 0 and random.random() < 0.6:
        raise ToolFailure("rerank confidence too low — no on-topic docs")
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=200,
        system="Pick the 2 most relevant docs and return them in order, one per line.",
        messages=[{"role": "user", "content": f"Q: {query}\nDocs:\n" + "\n".join(f"- {d}" for d in docs)}],
    )
    raw = resp.content[0].text if resp.content else ""
    return [line.strip("-• ").strip() for line in raw.splitlines() if line.strip()][:2]


@clustertrace.trace
def answer(query: str, docs: list[str]) -> str:
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=120,
        system="Answer the question using ONLY the provided docs. Be concise.",
        messages=[{"role": "user", "content": f"Q: {query}\nDocs: " + " | ".join(docs)}],
    )
    return (resp.content[0].text if resp.content else "").strip()


@clustertrace.trace(tags={"agent": "rag"})
def rag_agent(query: str) -> str:
    clustertrace.tag("query", query[:40])
    docs = retrieve(query)
    ranked = rerank(query, docs)
    return answer(query, ranked)


# ---------------------------------------------------------------------------
# 3. tool_use_agent — planner + fixed tool calls; failures cluster on calc
# ---------------------------------------------------------------------------

@clustertrace.trace
def plan(task: str) -> list[str]:
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=120,
        system="List exactly 3 steps (one per line, no numbering) to solve this. Use tools: web_search, calculator, lookup.",
        messages=[{"role": "user", "content": task}],
    )
    raw = resp.content[0].text if resp.content else ""
    return [s.strip() for s in raw.splitlines() if s.strip()][:3]


@clustertrace.trace
def web_search(query: str) -> dict:
    if random.random() < 0.10:
        raise ToolFailure("search rate limit")
    return {"hits": ["result-a", "result-b"]}


@clustertrace.trace
def calculator(expression: str) -> float:
    """Evaluate a simple arithmetic expression; flaky on malformed input."""
    # ~30% of the time we get arg-shape errors (LLM hands us a non-expression)
    if not expression or any(c.isalpha() for c in expression.replace(" ", "")):
        raise ToolFailure(f"invalid expression: {expression!r}")
    try:
        return float(eval(expression, {"__builtins__": {}}, {}))
    except Exception as e:
        raise ToolFailure(f"calc error: {e}") from e


@clustertrace.trace
def lookup(key: str) -> dict:
    return {"key": key, "value": random.choice(["alpha", "beta", "gamma"])}


@clustertrace.trace
def synthesize(task: str, observations: list) -> str:
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=100,
        system="Synthesize a 1-sentence answer from these observations.",
        messages=[{"role": "user", "content": f"Task: {task}\nObs: {observations}"}],
    )
    return (resp.content[0].text if resp.content else "").strip()


@clustertrace.trace(tags={"agent": "tool_use"})
def tool_use_agent(task: str) -> str:
    clustertrace.tag("task", task[:40])
    plan(task)
    # Fixed call order: web_search → calculator → lookup
    obs: list = []
    obs.append(web_search(task))
    # Pass the LLM-generated "expression" — sometimes a real arithmetic string,
    # sometimes garbage (failure path).
    expr = random.choice(["2+2*5", "sqrt(16)", "len('abc') + 1", "100/4", "x ** 2", "(3+4)*2"])
    try:
        obs.append({"calc": calculator(expr)})
    except ToolFailure as e:
        obs.append({"calc_error": str(e)})
    obs.append(lookup(task[:20]))
    return synthesize(task, obs)
