"""Coverage for prompt_help engine + /api/prompts/* endpoints.

The engine is pure functions, so we exercise it directly. The endpoints
get a thinner integration check using TestClient + seeded storage.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from clustertrace import prompt_help, storage
from clustertrace.dashboard.app import app

# ---------------------------------------------------------------------------
# Pure engine: heuristics
# ---------------------------------------------------------------------------


def test_heuristic_vague_verb_fires_on_fix():
    r = prompt_help.heuristic_results("fix this thing please")
    assert r["starts_with_vague_verb"] is True


def test_heuristic_vague_verb_does_not_fire_on_specific_verb():
    r = prompt_help.heuristic_results("rewrite the parser in src/parser.py")
    assert r["starts_with_vague_verb"] is False


def test_heuristic_file_path_fires_on_unix_and_windows():
    assert prompt_help.heuristic_results("update src/foo.py")["mentions_file_path"]
    assert prompt_help.heuristic_results(r"edit C:\Users\me\bar.ts")["mentions_file_path"]


def test_heuristic_file_path_does_not_fire_on_domain_or_version():
    # Regression: the original regex matched any "word.word" token, so plain
    # English with embedded dots leaked into the file-path heuristic.
    assert not prompt_help.heuristic_results("see example.com for details")[
        "mentions_file_path"
    ]
    assert not prompt_help.heuristic_results("on version 1.2.3 of the library")[
        "mentions_file_path"
    ]
    assert not prompt_help.heuristic_results("Mr. Smith said i.e. that")[
        "mentions_file_path"
    ]


def test_heuristic_file_path_fires_on_bare_known_extension():
    # Bare names with known code/doc extensions should still count as paths.
    assert prompt_help.heuristic_results("update README.md")["mentions_file_path"]
    assert prompt_help.heuristic_results("edit package.json")["mentions_file_path"]


def test_heuristic_acceptance_criteria_fires():
    assert prompt_help.heuristic_results("it should compile and pass tests")[
        "has_acceptance_criteria"
    ]


def test_heuristic_multiple_unrelated_asks():
    # 3 connectives ("and", "also", "then") should trip the heuristic.
    assert prompt_help.heuristic_results(
        "do A and B, also C, then D"
    )["multiple_unrelated_asks"]


def test_analyse_prompt_orders_weak_findings_first():
    r = prompt_help.analyse_prompt("fix this")
    # Heuristics fire: very_short + starts_with_vague_verb. Both 'weak'.
    assert r["findings"], "expected at least one finding for 'fix this'"
    # All weak findings should come before any strong ones.
    polarities = [f["polarity"] for f in r["findings"]]
    assert polarities == sorted(polarities, key=lambda p: 0 if p == "weak" else 1)


def test_patterns_from_traces_sorts_by_absolute_delta():
    # Succeeded prompts: all mention files. Dead-ended: none do.
    succeeded = ["fix src/a.py", "rewrite lib/b.ts", "update docs/c.md"]
    dead_ended = ["fix this", "improve this", "make it better"]
    report = prompt_help.patterns_from_traces(succeeded, dead_ended)
    assert report["succeeded_count"] == 3
    assert report["dead_ended_count"] == 3
    # The biggest-delta row should be the file-path one.
    top = report["rows"][0]
    assert abs(top["delta"]) >= abs(report["rows"][1]["delta"])


# ---------------------------------------------------------------------------
# /api/prompts/critique
# ---------------------------------------------------------------------------


def test_api_critique_returns_findings_for_vague_prompt():
    client = TestClient(app)
    r = client.post("/api/prompts/critique", json={"text": "fix this please"})
    assert r.status_code == 200
    body = r.json()
    assert "findings" in body
    keys = [f["key"] for f in body["findings"]]
    assert "starts_with_vague_verb" in keys


def test_api_critique_rejects_empty_text():
    client = TestClient(app)
    r = client.post("/api/prompts/critique", json={"text": "   "})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/prompts/templates (CRUD)
# ---------------------------------------------------------------------------


def test_api_templates_create_list_update_delete():
    client = TestClient(app)

    # Create
    r = client.post(
        "/api/prompts/templates",
        json={"name": "PR description", "body": "Write a PR description for {diff}.", "tags": ["pr", "writing"]},
    )
    assert r.status_code == 200
    tid = r.json()["id"]
    assert tid > 0

    # List — should contain it
    r = client.get("/api/prompts/templates")
    listed = r.json()["templates"]
    found = [t for t in listed if t["id"] == tid]
    assert found, "newly-created template missing from list"
    assert found[0]["tags"] == ["pr", "writing"]

    # Update body
    r = client.patch(
        f"/api/prompts/templates/{tid}",
        json={"body": "Write a PR description from {diff} in bullet points."},
    )
    assert r.status_code == 200

    # Use bump — should increment counter
    client.post(f"/api/prompts/templates/{tid}/use")
    client.post(f"/api/prompts/templates/{tid}/use")
    r = client.get("/api/prompts/templates")
    t = [t for t in r.json()["templates"] if t["id"] == tid][0]
    assert t["use_count"] >= 2

    # Delete
    r = client.delete(f"/api/prompts/templates/{tid}")
    assert r.status_code == 200
    r = client.get("/api/prompts/templates")
    assert not [t for t in r.json()["templates"] if t["id"] == tid]


def test_api_templates_rejects_empty_name_or_body():
    client = TestClient(app)
    r = client.post("/api/prompts/templates", json={"name": "", "body": "x"})
    assert r.status_code == 400
    r = client.post("/api/prompts/templates", json={"name": "x", "body": "  "})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/prompts/llm-deepen
# ---------------------------------------------------------------------------


def test_api_llm_deepen_returns_503_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(app)
    r = client.post(
        "/api/prompts/llm-deepen",
        json={"kind": "critique", "payload": "fix this"},
    )
    assert r.status_code == 503
    body = r.json()
    assert "ANTHROPIC_API_KEY" in body["error"]


def test_api_llm_deepen_rejects_unknown_kind():
    client = TestClient(app)
    r = client.post(
        "/api/prompts/llm-deepen",
        json={"kind": "magic", "payload": "x"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/prompts/patterns
# ---------------------------------------------------------------------------


def _seed_trace_with_prompt(trace_id: str, prompt_text: str, *, dead_ended: bool) -> None:
    now = time.time()
    storage.insert_trace(trace_id, "session", now)
    storage.finish_trace(trace_id, now + 1.0, "error" if dead_ended else "ok")
    # Use user_prompt attribute (the Claude Code OTel path).
    storage.insert_span(
        span_id=f"sp-{trace_id}",
        trace_id=trace_id,
        parent_id=None,
        name="claude_code.interaction",
        kind="function",
        started_at=now,
        input_data=None,
        attrs={"user_prompt": prompt_text},
    )
    storage.finish_span(
        f"sp-{trace_id}", now + 1.0, "ok", output_data=None, attrs={"user_prompt": prompt_text}
    )


def test_api_patterns_splits_succeeded_vs_dead_ended():
    client = TestClient(app)
    _seed_trace_with_prompt("t-pat-s1", "rewrite src/foo.py to use X", dead_ended=False)
    _seed_trace_with_prompt("t-pat-s2", "rewrite src/bar.py to use Y", dead_ended=False)
    _seed_trace_with_prompt("t-pat-d1", "fix this", dead_ended=True)
    _seed_trace_with_prompt("t-pat-d2", "improve that", dead_ended=True)
    r = client.get("/api/prompts/patterns?window=all")
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded_count"] >= 2
    assert body["dead_ended_count"] >= 2
    # File-path heuristic should be one of the rows; succeeded rate should beat dead-ended rate.
    file_row = next((r for r in body["rows"] if r["key"] == "mentions_file_path"), None)
    assert file_row is not None
    assert file_row["succeeded_rate"] > file_row["dead_ended_rate"]


def test_api_patterns_with_samples_returns_actual_prompts():
    """The Deepen-with-Claude flow depends on sample prompts being shipped
    with the patterns response. Without with_samples=1 they must be absent;
    with it, each bucket must contain at least one prompt from the seeded
    traces (truncated to 600 chars)."""
    client = TestClient(app)
    long_prompt = "rewrite src/foo.py " + ("blah " * 200)  # > 600 chars
    _seed_trace_with_prompt("t-sample-s1", long_prompt, dead_ended=False)
    _seed_trace_with_prompt("t-sample-d1", "fix this thing", dead_ended=True)

    # Default: no samples in response.
    r = client.get("/api/prompts/patterns?window=all")
    assert "sample_succeeded" not in r.json()

    # with_samples=1: arrays present and prompts capped at 600 chars.
    r = client.get("/api/prompts/patterns?window=all&with_samples=1")
    body = r.json()
    assert "sample_succeeded" in body
    assert "sample_dead_ended" in body
    assert any("rewrite src/foo.py" in s for s in body["sample_succeeded"])
    assert all(len(s) <= 600 for s in body["sample_succeeded"])
    assert any("fix this" in s for s in body["sample_dead_ended"])
