"""Langfuse importer — fixture-driven coverage of the mapping rules."""
from __future__ import annotations

import json
from pathlib import Path

from clustertrace import storage
from clustertrace.importers.langfuse import import_langfuse

FIXTURE = Path(__file__).parent.parent / "fixtures" / "import_langfuse.json"


def test_langfuse_fixture_imports_two_traces() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        imported, _skipped = import_langfuse(f)
    assert imported == 2

    with storage.connect() as c:
        trace_ids = [r["id"] for r in c.execute("SELECT id FROM traces ORDER BY id").fetchall()]
        spans = c.execute("SELECT name, kind FROM spans ORDER BY started_at").fetchall()
        tag_rows = c.execute("SELECT trace_id, key, value FROM trace_tags").fetchall()

    assert trace_ids == ["langfuse:lf-trace-001", "langfuse:lf-trace-002"]
    # Mapping rule: type=GENERATION -> llm_call, type=SPAN -> function
    kinds = {(s["name"], s["kind"]) for s in spans}
    assert ("anthropic.messages.create", "llm_call") in kinds
    assert ("fetch_paper", "function") in kinds
    assert ("openai.chat.completions", "llm_call") in kinds

    # LLM span carries model + token attrs
    with storage.connect() as c:
        attrs_row = c.execute(
            "SELECT attrs_json FROM spans WHERE name = 'anthropic.messages.create'"
        ).fetchone()
    attrs = json.loads(attrs_row["attrs_json"])
    assert attrs["model"] == "claude-haiku-4-5-20251001"
    assert attrs["input_tokens"] == 240
    assert attrs["output_tokens"] == 110

    # Tags survive (string tags + metadata-as-tags)
    tag_keys = {(t["trace_id"], t["key"]) for t in tag_rows}
    assert ("langfuse:lf-trace-001", "agent:research") in tag_keys
    assert ("langfuse:lf-trace-001", "env:prod") in tag_keys
    assert ("langfuse:lf-trace-001", "meta.user_id") in tag_keys


def test_langfuse_error_observation_propagates_to_trace() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        import_langfuse(f)
    with storage.connect() as c:
        row = c.execute(
            "SELECT status, error_type, error_message FROM traces WHERE id = ?",
            ("langfuse:lf-trace-002",),
        ).fetchone()
    assert row["status"] == "error"
    assert row["error_type"] == "LangfuseError"
    assert "rate_limit" in (row["error_message"] or "")


def test_langfuse_id_collision_is_skipped_not_overwritten() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        first_imported, _ = import_langfuse(f)
    with FIXTURE.open(encoding="utf-8") as f:
        second_imported, second_skipped = import_langfuse(f)
    assert first_imported == 2
    assert second_imported == 0
    assert second_skipped >= 2

    with storage.connect() as c:
        trace_count = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert trace_count == 2  # no duplicates
