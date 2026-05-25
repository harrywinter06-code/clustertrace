"""POST /v1/traces — OTLP/JSON span ingestion via the dashboard."""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from clustertrace import storage
from clustertrace.dashboard.app import app


def _otlp_attr(key: str, value: Any) -> dict[str, Any]:
    """Build a single OTLP/JSON KeyValue. Mirrors the wire format protoc produces."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _otlp_span(
    *,
    name: str,
    trace_id_hex: str,
    span_id_hex: str,
    parent_span_id_hex: str = "",
    start_ns: int = 1_000_000_000,
    end_ns: int = 2_000_000_000,
    status_code: int = 1,  # 1=OK, 2=ERROR
    attributes: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "traceId": trace_id_hex,
        "spanId": span_id_hex,
        "parentSpanId": parent_span_id_hex,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes or [],
        "status": {"code": status_code},
        "events": events or [],
    }


def _wrap(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a flat list of OTLP/JSON spans into an ExportTraceServiceRequest body."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "test", "version": "0.0.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def test_otlp_post_creates_trace_and_span():
    client = TestClient(app)
    body = _wrap(
        [
            _otlp_span(
                name="ts.root",
                trace_id_hex="11111111111111111111111111111111",
                span_id_hex="2222222222222222",
            )
        ]
    )
    r = client.post("/v1/traces", json=body)
    assert r.status_code == 200
    assert "partialSuccess" in r.json()

    with storage.connect() as c:
        t = c.execute("SELECT id, name, status FROM traces").fetchone()
        sp = c.execute("SELECT name, kind, status FROM spans").fetchone()
    assert t is not None, "no trace landed in SQLite"
    assert t["name"] == "ts.root"
    assert t["status"] == "ok"
    assert sp["status"] == "ok"


def test_otlp_post_protobuf_creates_trace_and_span():
    """Protobuf branch: same payload via OTLP/HTTP/protobuf instead of JSON.

    Skipped automatically when `clustertrace[otel-import]` extra is missing
    (opentelemetry-proto is optional).
    """
    import pytest
    pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    ss = rs.scope_spans.add()
    span = ss.spans.add()
    span.trace_id = bytes.fromhex("33333333333333333333333333333333")
    span.span_id = bytes.fromhex("4444444444444444")
    span.name = "pb.root"
    span.kind = 1
    span.start_time_unix_nano = 1_000_000_000
    span.end_time_unix_nano = 2_000_000_000
    span.status.code = 1

    body = req.SerializeToString()
    client = TestClient(app)
    r = client.post(
        "/v1/traces",
        content=body,
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 200, r.text
    assert "partialSuccess" in r.json()

    with storage.connect() as c:
        t = c.execute(
            "SELECT id, name, status FROM traces WHERE name = 'pb.root'"
        ).fetchone()
    assert t is not None, "protobuf-decoded trace did not land in SQLite"
    assert t["status"] == "ok"
    # The exporter prefixes IDs by source (`otel:<hex>`) — the hex part is what
    # matters for the round-trip from bytes -> base64 (MessageToDict) -> hex.
    assert "33333333333333333333333333333333" in t["id"]


def test_otlp_post_protobuf_without_extra_returns_415(monkeypatch):
    """When opentelemetry.proto is not importable, the protobuf branch must
    return 415 with a helpful install hint rather than 500.

    Sys-modules patching is unreliable for the `from x.y import z` shape we
    use, so simulate the missing-extra state by patching the decoder itself.
    """
    def fake_decode(_raw):
        raise ImportError("simulated: opentelemetry-proto not installed")

    monkeypatch.setattr(
        "clustertrace.dashboard.app._decode_otlp_protobuf", fake_decode
    )

    client = TestClient(app)
    r = client.post(
        "/v1/traces",
        content=b"\x00\x00",  # body content doesn't matter; we patched the decoder
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 415
    assert "otel-import" in r.json()["error"]


def test_otlp_post_maps_gen_ai_to_llm_call_kind():
    client = TestClient(app)
    body = _wrap(
        [
            _otlp_span(
                name="anthropic.messages.create",
                trace_id_hex="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                span_id_hex="bbbbbbbbbbbbbbbb",
                attributes=[
                    _otlp_attr("gen_ai.request.model", "claude-haiku-4-5-20251001"),
                    _otlp_attr("gen_ai.usage.input_tokens", 100),
                    _otlp_attr("gen_ai.usage.output_tokens", 50),
                ],
            )
        ]
    )
    r = client.post("/v1/traces", json=body)
    assert r.status_code == 200
    with storage.connect() as c:
        kind = c.execute("SELECT kind FROM spans").fetchone()["kind"]
    assert kind == "llm_call"


def test_otlp_post_error_status_with_exception_event():
    client = TestClient(app)
    body = _wrap(
        [
            _otlp_span(
                name="bad.thing",
                trace_id_hex="12121212121212121212121212121212",
                span_id_hex="3434343434343434",
                status_code=2,  # ERROR
                events=[
                    {
                        "name": "exception",
                        "timeUnixNano": "1500000000",
                        "attributes": [
                            _otlp_attr("exception.type", "ValueError"),
                            _otlp_attr("exception.message", "bad"),
                        ],
                    }
                ],
            )
        ]
    )
    r = client.post("/v1/traces", json=body)
    assert r.status_code == 200
    with storage.connect() as c:
        t = c.execute("SELECT status, error_type, error_message FROM traces").fetchone()
    assert t["status"] == "error"
    assert t["error_type"] == "ValueError"
    assert "bad" in (t["error_message"] or "")


def test_otlp_post_parent_chain():
    client = TestClient(app)
    trace_id = "99999999999999999999999999999999"
    root_id = "aaaaaaaaaaaaaaaa"
    child_id = "bbbbbbbbbbbbbbbb"
    body = _wrap(
        [
            _otlp_span(
                name="root", trace_id_hex=trace_id, span_id_hex=root_id,
                start_ns=1_000_000_000, end_ns=3_000_000_000,
            ),
            _otlp_span(
                name="child", trace_id_hex=trace_id, span_id_hex=child_id,
                parent_span_id_hex=root_id,
                start_ns=1_500_000_000, end_ns=2_500_000_000,
            ),
        ]
    )
    r = client.post("/v1/traces", json=body)
    assert r.status_code == 200
    with storage.connect() as c:
        spans = c.execute("SELECT name, parent_id FROM spans ORDER BY started_at").fetchall()
    assert spans[0]["name"] == "root"
    assert spans[0]["parent_id"] is None
    assert spans[1]["name"] == "child"
    assert spans[1]["parent_id"] is not None


def test_otlp_post_malformed_span_does_not_break_batch():
    client = TestClient(app)
    body = _wrap(
        [
            # bad: missing traceId entirely
            {"spanId": "ffffffffffffffff", "name": "broken", "kind": 1},
            _otlp_span(
                name="ok_one",
                trace_id_hex="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                span_id_hex="ffffffffffffffff",
            ),
        ]
    )
    r = client.post("/v1/traces", json=body)
    assert r.status_code == 200
    ps = r.json().get("partialSuccess", {})
    assert ps.get("rejectedSpans") == "1"
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert n == 1


def test_otlp_post_invalid_json_returns_400():
    client = TestClient(app)
    r = client.post(
        "/v1/traces",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_otlp_post_empty_batch_is_success():
    client = TestClient(app)
    r = client.post("/v1/traces", json={"resourceSpans": []})
    assert r.status_code == 200


def test_otlp_options_preflight_cors():
    client = TestClient(app)
    r = client.options("/v1/traces")
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "POST" in (r.headers.get("access-control-allow-methods") or "")


def test_otlp_post_response_has_cors_header():
    client = TestClient(app)
    r = client.post("/v1/traces", json={"resourceSpans": []})
    assert r.headers.get("access-control-allow-origin") == "*"


def test_otlp_tolerates_legacy_instrumentation_library_spans_field():
    """Some older OTel JS exporters still emit `instrumentationLibrarySpans`."""
    client = TestClient(app)
    body = {
        "resourceSpans": [
            {
                "instrumentationLibrarySpans": [
                    {
                        "spans": [
                            _otlp_span(
                                name="legacy",
                                trace_id_hex="cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
                                span_id_hex="abababababababab",
                            )
                        ]
                    }
                ]
            }
        ]
    }
    r = client.post("/v1/traces", json=body)
    assert r.status_code == 200
    with storage.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    assert n == 1
