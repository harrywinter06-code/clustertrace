"""OTLP/JSON importer — fixture-driven mapping verification.

Distinct from `tests/test_otel.py`, which exercises the OTel *exporter*
(`ClustertraceSpanExporter`) — this one exercises the OTLP/JSON *importer*
under `clustertrace.importers.otel`.
"""
from __future__ import annotations

import json
from pathlib import Path

from clustertrace import storage
from clustertrace.importers.otel import import_otlp

FIXTURE = Path(__file__).parent.parent / "fixtures" / "import_otel.json"


def test_otlp_fixture_imports_one_trace_with_three_spans() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        imported, _ = import_otlp(f)
    assert imported == 1

    with storage.connect() as c:
        spans = c.execute("SELECT name, kind, status FROM spans ORDER BY started_at").fetchall()
        trace = c.execute("SELECT id, status, error_type, error_message FROM traces").fetchone()

    by_name = {s["name"]: s for s in spans}
    # gen_ai.* attrs -> llm_call; tool.* attrs -> tool_call; otherwise function.
    assert by_name["anthropic.messages.create"]["kind"] == "llm_call"
    assert by_name["tool.fetch"]["kind"] == "tool_call"
    assert by_name["research_agent.run"]["kind"] == "function"
    assert by_name["tool.fetch"]["status"] == "error"

    # Token + model attrs were normalized onto the standard keys.
    with storage.connect() as c:
        attrs = json.loads(
            c.execute(
                "SELECT attrs_json FROM spans WHERE name = 'anthropic.messages.create'"
            ).fetchone()["attrs_json"]
        )
    assert attrs["model"] == "claude-haiku-4-5-20251001"
    assert attrs["input_tokens"] == 512
    assert attrs["output_tokens"] == 180

    # Child error propagates to the trace.
    assert trace["status"] == "error"
    assert trace["error_type"] == "HTTPError"
    assert "503" in (trace["error_message"] or "")


def test_otlp_id_collision_is_skipped() -> None:
    with FIXTURE.open(encoding="utf-8") as f:
        first, _ = import_otlp(f)
    with FIXTURE.open(encoding="utf-8") as f:
        second, _ = import_otlp(f)
    assert first == 1
    assert second == 0
    with storage.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
