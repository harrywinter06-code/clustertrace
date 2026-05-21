"""LangSmith importer — fixture-driven mapping verification."""
from __future__ import annotations

import json
from pathlib import Path

from clustertrace import storage
from clustertrace.importers.langsmith import import_langsmith

FIXTURE = Path(__file__).parent.parent / "fixtures" / "import_langsmith.json"


def test_langsmith_fixture_imports_one_trace_with_three_spans() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        imported, _ = import_langsmith(f)
    assert imported == 1

    with storage.connect() as c:
        spans = c.execute("SELECT name, kind, status FROM spans ORDER BY started_at").fetchall()
        trace = c.execute("SELECT id, status, error_type FROM traces").fetchone()
        tags = c.execute("SELECT key FROM trace_tags WHERE trace_id = ?", (trace["id"],)).fetchall()

    assert len(spans) == 3
    by_name = {s["name"]: s for s in spans}
    # run_type=llm -> llm_call, run_type=tool -> tool_call, chain -> function
    assert by_name["ChatAnthropic"]["kind"] == "llm_call"
    assert by_name["doc_lookup"]["kind"] == "tool_call"
    assert by_name["research_agent"]["kind"] == "function"

    # LLM span carries model + tokens.
    with storage.connect() as c:
        attrs = json.loads(
            c.execute("SELECT attrs_json FROM spans WHERE name = 'ChatAnthropic'").fetchone()[
                "attrs_json"
            ]
        )
    assert attrs["model"] == "claude-haiku-4-5-20251001"
    assert attrs["input_tokens"] == 410
    assert attrs["output_tokens"] == 220

    # Tool error propagates to the trace.
    assert trace["status"] == "error"
    assert trace["error_type"] == "LangSmithError"

    # Tags survive.
    keys = {t["key"] for t in tags}
    assert "agent:research" in keys
    assert "env:staging" in keys


def test_langsmith_orphan_child_still_creates_trace() -> None:
    """A child run with no matching parent in the file must not be lost."""
    orphan = {
        "id": "orphan-child",
        "name": "lonely_llm",
        "run_type": "llm",
        "start_time": "2026-05-21T10:00:00Z",
        "end_time": "2026-05-21T10:00:01Z",
        "parent_run_id": "missing-parent",
        "trace_id": "orphan-child",
        "extra": {"invocation_params": {"model": "gpt-4o-mini"}},
    }
    import io

    imported, _ = import_langsmith(io.StringIO(json.dumps({"runs": [orphan]})))
    assert imported == 1
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    assert n == 1
