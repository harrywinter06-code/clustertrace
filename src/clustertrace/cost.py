"""Cost estimation for LLM calls.

Pricing is per million tokens. Sourced from each provider's public pricing
page as of 2026-05; users can override via $CLUSTERTRACE_PRICING_JSON or extend
the table at runtime.

Cost is computed lazily when:
  - the dashboard renders a trace (per-span cost shown in attrs)
  - someone calls cost.backfill() to populate the cached `cost_usd` columns
"""
from __future__ import annotations

import json
import os
from typing import Any

# (input_per_million, output_per_million) in USD
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-5-20251001": (3.00, 15.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-haiku-3-5": (0.80, 4.00),
    "claude-sonnet-3-7": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (1.10, 4.40),
    # Google (illustrative)
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.5-flash": (0.30, 2.50),
}


def _load_overrides() -> None:
    """Allow $CLUSTERTRACE_PRICING_JSON='{"some-model": [2.0, 10.0]}' to override."""
    raw = os.environ.get("CLUSTERTRACE_PRICING_JSON")
    if not raw:
        return
    try:
        data = json.loads(raw)
        for model, prices in data.items():
            if isinstance(prices, list | tuple) and len(prices) == 2:
                PRICING[model] = (float(prices[0]), float(prices[1]))
    except (ValueError, TypeError):
        pass


_load_overrides()


def _match_model(model: str) -> tuple[float, float] | None:
    """Exact match first, then prefix match for date-versioned ids."""
    if model in PRICING:
        return PRICING[model]
    # Try stripping any -YYYYMMDD suffix
    if len(model) > 9 and model[-9] == "-" and model[-8:].isdigit():
        stem = model[:-9]
        if stem in PRICING:
            return PRICING[stem]
    # Try prefix match: longest prefix wins
    matches = [(k, v) for k, v in PRICING.items() if model.startswith(k)]
    if matches:
        matches.sort(key=lambda kv: -len(kv[0]))
        return matches[0][1]
    return None


def estimate_span_cost(attrs: dict[str, Any] | None) -> float | None:
    """Compute USD cost for an LLM span given its attrs dict.

    Looks for: model, input_tokens / prompt_tokens, output_tokens / completion_tokens.
    Returns None when pricing for the model is unknown.
    """
    if not attrs:
        return None
    model = attrs.get("model")
    if not model:
        return None
    price = _match_model(str(model))
    if not price:
        return None
    in_tok = attrs.get("input_tokens") or attrs.get("prompt_tokens") or 0
    out_tok = attrs.get("output_tokens") or attrs.get("completion_tokens") or 0
    if not (in_tok or out_tok):
        return None
    in_cost = (in_tok / 1_000_000.0) * price[0]
    out_cost = (out_tok / 1_000_000.0) * price[1]
    return round(in_cost + out_cost, 6)


def backfill(limit: int | None = None) -> tuple[int, float]:
    """Compute and cache costs for every llm_call span and roll up per trace.

    Returns (n_spans_priced, total_cost).
    """
    from clustertrace import storage

    total = 0.0
    n_spans = 0
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT id, trace_id, attrs_json FROM spans WHERE kind='llm_call'"
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()

    per_trace: dict[str, float] = {}
    for r in rows:
        try:
            attrs = json.loads(r["attrs_json"]) if r["attrs_json"] else None
        except (ValueError, TypeError):
            attrs = None
        cost = estimate_span_cost(attrs)
        if cost is None:
            continue
        storage.set_span_cost(r["id"], cost)
        per_trace[r["trace_id"]] = per_trace.get(r["trace_id"], 0.0) + cost
        total += cost
        n_spans += 1

    for tid, cost in per_trace.items():
        storage.set_trace_cost(tid, round(cost, 6))

    return n_spans, round(total, 4)
