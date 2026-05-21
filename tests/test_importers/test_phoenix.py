"""Phoenix / OpenInference importer — fixture-driven mapping verification."""
from __future__ import annotations

import json
from pathlib import Path

from clustertrace import storage
from clustertrace.importers.phoenix import import_phoenix

FIXTURE = Path(__file__).parent.parent / "fixtures" / "import_phoenix.jsonl"


def test_phoenix_fixture_imports_single_trace_with_three_spans() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        imported, _skipped = import_phoenix(f)
    assert imported == 1

    with storage.connect() as c:
        spans = c.execute("SELECT name, kind, status FROM spans ORDER BY started_at").fetchall()
        trace = c.execute("SELECT id, status, error_type FROM traces").fetchone()

    assert len(spans) == 3
    names_kinds = {(s["name"], s["kind"]) for s in spans}
    # OpenInference span.kind LLM -> llm_call, TOOL -> tool_call, CHAIN -> function.
    assert ("anthropic.messages.create", "llm_call") in names_kinds
    assert ("tools.search", "tool_call") in names_kinds
    assert ("research_chain", "function") in names_kinds

    # LLM span has model + tokens normalized
    with storage.connect() as c:
        attrs_row = c.execute(
            "SELECT attrs_json FROM spans WHERE name = 'anthropic.messages.create'"
        ).fetchone()
    attrs = json.loads(attrs_row["attrs_json"])
    assert attrs["model"] == "claude-haiku-4-5-20251001"
    assert attrs["input_tokens"] == 320
    assert attrs["output_tokens"] == 150

    # Child-span error promotes to trace status (mirrors OTel exporter behavior).
    assert trace["status"] == "error"
    assert trace["error_type"] == "ConnectionError"

    # session.id surfaced as a tag.
    with storage.connect() as c:
        tag = c.execute(
            "SELECT value FROM trace_tags WHERE trace_id = ? AND key = 'session.id'",
            (trace["id"],),
        ).fetchone()
    assert tag is not None
    assert tag["value"] == "sess-123"


def test_phoenix_idempotent_re_import() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        first, _ = import_phoenix(f)
    with FIXTURE.open(encoding="utf-8") as f:
        second, _ = import_phoenix(f)
    assert first == 1
    assert second == 0
    with storage.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
