"""Cost estimation, model matching, backfill."""
import pytest

import agentlog
from agentlog import cost, storage


def test_known_model_estimates_correctly():
    attrs = {"model": "claude-haiku-4-5-20251001", "input_tokens": 1000, "output_tokens": 500}
    # 1000 in × $1/M + 500 out × $5/M = $0.001 + $0.0025 = $0.0035
    assert cost.estimate_span_cost(attrs) == pytest.approx(0.0035)


def test_openai_keys_also_work():
    attrs = {"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 500}
    # 1000 × $0.15/M + 500 × $0.60/M = $0.00015 + $0.0003 = $0.00045
    assert cost.estimate_span_cost(attrs) == pytest.approx(0.00045)


def test_unknown_model_returns_none():
    attrs = {"model": "nope-not-a-real-model", "input_tokens": 100, "output_tokens": 100}
    assert cost.estimate_span_cost(attrs) is None


def test_no_tokens_returns_none():
    attrs = {"model": "claude-haiku-4-5-20251001"}
    assert cost.estimate_span_cost(attrs) is None


def test_no_attrs_returns_none():
    assert cost.estimate_span_cost(None) is None
    assert cost.estimate_span_cost({}) is None


def test_date_suffix_stripping():
    # We match the date-suffixed id directly; if a user passes an unknown
    # date suffix we still find the base price.
    attrs = {"model": "claude-haiku-4-5-99991231", "input_tokens": 1000, "output_tokens": 0}
    cost_val = cost.estimate_span_cost(attrs)
    assert cost_val == pytest.approx(0.001)


def test_env_override(monkeypatch):
    monkeypatch.setenv("AGENTLOG_PRICING_JSON", '{"made-up-model": [10.0, 20.0]}')
    # Re-import to trigger the load
    import importlib

    import agentlog.cost as fresh
    importlib.reload(fresh)
    attrs = {"model": "made-up-model", "input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert fresh.estimate_span_cost(attrs) == pytest.approx(30.0)


def test_backfill_caches_cost_on_spans_and_traces():
    """End-to-end: wrap_anthropic call → backfill → cost columns populated."""

    class FakeUsage:
        def __init__(self):
            self.input_tokens = 1000
            self.output_tokens = 500

    class FakeBlock:
        type = "text"
        text = "hi"

    class FakeResp:
        id = "x"
        model = "claude-haiku-4-5-20251001"
        stop_reason = "end_turn"
        role = "assistant"
        content = [FakeBlock()]
        usage = FakeUsage()

    class FakeMessages:
        def create(self, **kwargs): return FakeResp()

    class FakeClient:
        def __init__(self): self.messages = FakeMessages()

    wrapped = agentlog.wrap_anthropic(FakeClient())

    @agentlog.trace
    def go():
        wrapped.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10, messages=[])

    go()

    n, total = cost.backfill()
    assert n == 1
    assert total == pytest.approx(0.0035)
    with storage.connect() as c:
        span_cost = c.execute("SELECT cost_usd FROM spans WHERE kind='llm_call'").fetchone()["cost_usd"]
        trace_cost = c.execute("SELECT cost_usd FROM traces").fetchone()["cost_usd"]
    assert span_cost == pytest.approx(0.0035)
    assert trace_cost == pytest.approx(0.0035)
