"""LLM-as-judge on cluster representatives.

Coverage:
  - `no_exceptions_evaluator` correctly flags errored spans
  - `evaluate_cluster` persists a JudgeVerdict to `cluster_judgments`
  - `evaluate_all_clusters` runs each cluster exactly once and respects the
    cost-cap (the cap is hard: aborts BEFORE any LLM calls)
  - `llm_judge_evaluator` parses the model's reply and bubbles up missing
    credentials cleanly (the latter without ever opening a network socket)
  - Sampling is deterministic per sig_hash so the report is reproducible
"""
from __future__ import annotations

import time

import pytest

from clustertrace import cluster, judge, storage


def _seed_trace(
    name: str,
    status: str,
    *,
    started: float | None = None,
    span_status: str | None = None,
) -> str:
    started_at = started if started is not None else time.time()
    tid = f"t-{name}-{int(started_at * 1_000_000)}-{status}"
    storage.insert_trace(tid, name, started_at)
    storage.insert_span(
        span_id=f"{tid}:root",
        trace_id=tid,
        parent_id=None,
        name=name,
        kind="function",
        started_at=started_at,
        input_data={"args": [1, 2], "kwargs": {}},
    )
    storage.finish_span(
        f"{tid}:root", started_at + 0.001,
        "ok" if status != "error" else "error",
        output_data={"answer": 42},
    )
    storage.insert_span(
        span_id=f"{tid}:step",
        trace_id=tid,
        parent_id=f"{tid}:root",
        name="step",
        kind="function",
        started_at=started_at + 0.0001,
    )
    storage.finish_span(
        f"{tid}:step", started_at + 0.0005, span_status or status,
        error_message=("boom" if (span_status or status) == "error" else None),
    )
    storage.finish_trace(
        tid, started_at + 0.001, status,
        error_type="ValueError" if status == "error" else None,
        error_message="boom" if status == "error" else None,
    )
    cluster.compute_and_store_signature(tid)
    return tid


def _seed_cluster_with_traces(name: str, statuses: list[str]) -> str:
    """Seed N traces with the same shape but possibly different statuses.

    Returns the sig_hash they all share (they share a signature because the
    span name/topology is the same — only the final trace status differs).
    """
    sig_hash: str | None = None
    base = time.time()
    for i, st in enumerate(statuses):
        tid = _seed_trace(name=name, status=st, started=base + i * 0.01)
        if sig_hash is None:
            with storage.connect() as conn:
                row = conn.execute(
                    "SELECT signature FROM traces WHERE id = ?", (tid,)
                ).fetchone()
            sig_hash = cluster.signature_hash(row["signature"])
    assert sig_hash is not None
    return sig_hash


def test_no_exceptions_evaluator_passes_clean_trace() -> None:
    _seed_trace("agent", "ok")
    sig = _seed_cluster_with_traces("agent", ["ok"])
    verdict = judge.evaluate_cluster(sig, judge.no_exceptions_evaluator, n_samples=1)
    assert verdict.pass_count == 1
    assert verdict.fail_count == 0
    assert verdict.samples_evaluated == 1


def test_no_exceptions_evaluator_flags_errored_span() -> None:
    sig = _seed_cluster_with_traces("agent", ["error"])
    verdict = judge.evaluate_cluster(sig, judge.no_exceptions_evaluator, n_samples=1)
    assert verdict.pass_count == 0
    assert verdict.fail_count == 1
    assert verdict.representative_failures, "should record the failing trace_id"


def test_judgment_is_persisted_to_db() -> None:
    # Use only one status so all traces share a signature; the goal here is
    # to confirm the row hits the DB, not to test mixed-status clustering.
    sig = _seed_cluster_with_traces("agent", ["ok", "ok", "ok"])
    judge.evaluate_cluster(sig, judge.no_exceptions_evaluator, n_samples=3)
    latest = storage.get_latest_cluster_judgment(sig)
    assert latest is not None
    assert latest["evaluator_name"] == "no_exceptions"
    assert latest["pass_count"] + latest["fail_count"] == 3


def test_evaluate_cluster_handles_empty_cluster_without_crash() -> None:
    verdict = judge.evaluate_cluster(
        "nonexistent-hash", judge.no_exceptions_evaluator, n_samples=3
    )
    assert verdict.samples_evaluated == 0
    assert verdict.pass_count == 0
    assert verdict.fail_count == 0


def test_evaluator_exception_counts_as_fail() -> None:
    """An evaluator that itself crashes is recorded as a fail with the
    exception text — never silently swallowed."""
    sig = _seed_cluster_with_traces("agent", ["ok"])

    def bad_evaluator(_trace):
        raise RuntimeError("evaluator blew up")

    verdict = judge.evaluate_cluster(sig, bad_evaluator, n_samples=1)
    assert verdict.fail_count == 1
    assert "evaluator blew up" in verdict.notes


def test_evaluate_all_clusters_persists_one_judgment_per_cluster() -> None:
    sig_a = _seed_cluster_with_traces("agent_a", ["ok", "ok"])
    sig_b = _seed_cluster_with_traces("agent_b", ["error", "ok"])
    out = judge.evaluate_all_clusters(judge.no_exceptions_evaluator, n_samples=2)
    assert set(out.keys()) >= {sig_a, sig_b}
    assert storage.get_latest_cluster_judgment(sig_a) is not None
    assert storage.get_latest_cluster_judgment(sig_b) is not None


def test_cost_cap_aborts_before_any_calls() -> None:
    """Hard requirement: cost cap MUST prevent runaway spend BEFORE LLM
    calls. We assert by passing a $0 budget against an evaluator that
    declares non-zero cost, and confirming no DB rows are written and no
    network call is made."""
    sig = _seed_cluster_with_traces("agent", ["ok"])
    # Fake evaluator: looks like an LLM judge (has .model) but never gets called.
    sentinel: list[int] = []

    def fake_judge(_trace):
        sentinel.append(1)
        return {"pass": True, "reason": "noop"}

    fake_judge.model = "claude-haiku-4-5-20251001"

    with pytest.raises(judge.JudgeCostCapExceeded):
        judge.evaluate_all_clusters(
            fake_judge, n_samples=3, max_cost_usd=0.0
        )
    assert sentinel == [], "cost cap let the evaluator run"
    assert storage.get_latest_cluster_judgment(sig) is None


def test_llm_judge_requires_api_key(monkeypatch) -> None:
    sig = _seed_cluster_with_traces("agent", ["ok"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    judger = judge.llm_judge_evaluator("did the agent answer?")
    verdict = judge.evaluate_cluster(sig, judger, n_samples=1)
    # Missing key surfaces as a fail with a clear message — never a crash.
    assert verdict.fail_count == 1
    assert "ANTHROPIC_API_KEY" in verdict.notes


def test_llm_judge_parses_model_reply(monkeypatch) -> None:
    """End-to-end with a stubbed anthropic.Anthropic — confirms the prompt
    builder, response parser, and verdict aggregation work together."""
    sig = _seed_cluster_with_traces("agent", ["ok", "ok"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Block:
        type = "text"
        text = '{"pass": true, "reason": "looks right"}'

    class _Msg:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            return _Msg()

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    import sys
    import types

    fake_mod = types.SimpleNamespace(Anthropic=_Client)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    judger = judge.llm_judge_evaluator("answer the question?", model="claude-haiku-4-5-20251001")
    verdict = judge.evaluate_cluster(sig, judger, n_samples=2)
    assert verdict.pass_count == 2
    assert verdict.fail_count == 0


def test_llm_judge_handles_unparseable_reply(monkeypatch) -> None:
    """If the judge model emits garbage, the evaluator must record a fail
    with the raw text — not crash and not silently pass."""
    sig = _seed_cluster_with_traces("agent", ["ok"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Block:
        type = "text"
        text = "I have no idea what you want."

    class _Msg:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            return _Msg()

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    import sys
    import types

    fake_mod = types.SimpleNamespace(Anthropic=_Client)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    judger = judge.llm_judge_evaluator("rubric")
    verdict = judge.evaluate_cluster(sig, judger, n_samples=1)
    assert verdict.fail_count == 1


def test_sampling_is_deterministic_per_cluster() -> None:
    """Two judge runs on the same cluster pick the same sample set, so the
    report is reproducible.
    """
    sig = _seed_cluster_with_traces("agent", ["ok"] * 5 + ["error"] * 5)
    first = judge.evaluate_cluster(sig, judge.no_exceptions_evaluator, n_samples=3, persist=False)
    second = judge.evaluate_cluster(sig, judge.no_exceptions_evaluator, n_samples=3, persist=False)
    assert first.pass_count + first.fail_count == 3
    assert (first.pass_count, first.fail_count) == (second.pass_count, second.fail_count)


def test_no_exceptions_evaluator_is_zero_cost() -> None:
    """The cost cap should be a no-op for the free evaluator. Run with $0
    budget and confirm no exception is raised and judgments DO get persisted."""
    sig = _seed_cluster_with_traces("agent", ["ok"])
    out = judge.evaluate_all_clusters(
        judge.no_exceptions_evaluator, n_samples=1, max_cost_usd=0.0
    )
    assert sig in out
    assert storage.get_latest_cluster_judgment(sig) is not None
