"""OpenTelemetry exporter — ingest spans from any OTel-instrumented app.

Usage:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from agentlog.otel import AgentlogSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(AgentlogSpanExporter()))

Any tool with OpenTelemetry instrumentation — LangChain, LlamaIndex, OpenAI
SDK auto-instrumentation, Bedrock auto-instrumentation, your own custom OTel
code — now flows into agentlog's clusters page.
"""
from __future__ import annotations

from typing import Any

from agentlog import storage


def _hex(span_id_int: int, width: int = 16) -> str:
    """OTel span IDs are ints; we store hex strings."""
    return format(span_id_int, f"0{width}x")


def _otel_status_to_agentlog(otel_status: Any) -> str:
    """Map OTel StatusCode → agentlog status."""
    code = getattr(otel_status, "status_code", None)
    name = getattr(code, "name", None) or str(otel_status)
    if "ERROR" in name.upper():
        return "error"
    return "ok"


class AgentlogSpanExporter:
    """A SpanExporter that writes OTel spans into agentlog's SQLite store.

    Implements the OTel exporter protocol (export/shutdown). No hard dependency
    on opentelemetry-sdk — the caller provides the spans.
    """

    def export(self, spans: list) -> int:
        """Required by OTel SpanExporter. Returns 0 on success (matches SUCCESS=0)."""
        for span in spans:
            try:
                self._export_one(span)
            except Exception:
                # Match OTel semantics: a single bad span doesn't fail the batch.
                continue
        return 0

    def shutdown(self) -> None:
        """OTel exporter contract — nothing to flush, SQLite writes are sync."""
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True

    # ------------------------------------------------------------------

    def _export_one(self, span: Any) -> None:
        ctx = span.get_span_context()
        parent = span.parent

        trace_id_hex = _hex(ctx.trace_id, width=32)
        span_id_hex = _hex(ctx.span_id, width=16)
        parent_id_hex = _hex(parent.span_id, width=16) if parent else None

        started_at = span.start_time / 1e9 if span.start_time else 0.0
        ended_at = span.end_time / 1e9 if span.end_time else None
        status = _otel_status_to_agentlog(span.status)
        name = span.name or "otel.span"

        attrs = dict(span.attributes) if span.attributes else {}
        attrs["_otel_kind"] = getattr(span.kind, "name", str(span.kind))

        # Map common LLM-flavored attribute keys onto agentlog's conventions.
        kind = "function"
        if any(k.startswith("gen_ai.") or k.startswith("llm.") for k in attrs):
            kind = "llm_call"
            # Normalize token-count keys
            mapping = [
                ("gen_ai.usage.input_tokens", "input_tokens"),
                ("gen_ai.usage.prompt_tokens", "input_tokens"),
                ("gen_ai.usage.output_tokens", "output_tokens"),
                ("gen_ai.usage.completion_tokens", "output_tokens"),
                ("gen_ai.request.model", "model"),
                ("gen_ai.response.model", "model"),
                ("llm.model", "model"),
                ("llm.usage.prompt_tokens", "input_tokens"),
                ("llm.usage.completion_tokens", "output_tokens"),
            ]
            for otel_key, ours in mapping:
                if otel_key in attrs and ours not in attrs:
                    attrs[ours] = attrs[otel_key]
        elif any(k.startswith("tool.") for k in attrs):
            kind = "tool_call"

        error_type = None
        error_message = None
        if status == "error" and span.events:
            for e in span.events:
                ename = getattr(e, "name", "")
                if ename == "exception":
                    e_attrs = dict(getattr(e, "attributes", {}) or {})
                    error_type = e_attrs.get("exception.type")
                    error_message = e_attrs.get("exception.message")
                    break

        # Upsert the trace row (first span we see for this trace creates it).
        with storage.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM traces WHERE id = ?", (trace_id_hex,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO traces(id, name, started_at, status) VALUES(?, ?, ?, 'running')",
                    (trace_id_hex, name, started_at),
                )

        storage.insert_span(
            span_id=span_id_hex,
            trace_id=trace_id_hex,
            parent_id=parent_id_hex,
            name=name,
            kind=kind,
            started_at=started_at,
            input_data=None,
            attrs=attrs,
        )
        storage.finish_span(
            span_id_hex,
            ended_at or started_at,
            status,
            output_data=None,
            error_type=error_type,
            error_message=error_message,
            attrs=attrs,
        )

        # If this span has no parent, it's the root — finish the trace.
        # Promote any child-span errors to the trace status so the clusters
        # page treats OTel-ingested failures the same as native ones.
        if parent is None:
            final_status = status
            final_err_type = error_type
            final_err_msg = error_message
            if final_status == "ok":
                with storage.connect() as conn:
                    bad = conn.execute(
                        "SELECT error_type, error_message FROM spans "
                        "WHERE trace_id = ? AND status = 'error' LIMIT 1",
                        (trace_id_hex,),
                    ).fetchone()
                if bad is not None:
                    final_status = "error"
                    final_err_type = bad["error_type"] or "ChildSpanError"
                    final_err_msg = bad["error_message"] or "an inner span recorded an error"
            storage.finish_trace(
                trace_id_hex,
                ended_at or started_at,
                final_status,
                error_type=final_err_type,
                error_message=final_err_msg,
            )
