"""Explicit wrapper for the boto3 Bedrock runtime client."""
from __future__ import annotations

import io
import json
import time
from typing import Any

from clustertrace import storage
from clustertrace._ctx import current_span_id, current_trace_id, new_id


class _BufferedBody:
    def __init__(self, inner: Any, payload: bytes) -> None:
        self._inner = inner
        self._buffer = io.BytesIO(payload)

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._buffer.read(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


def _record_span(operation: str, model: str, kwargs: dict[str, Any]) -> tuple[str, str, bool, Any]:
    trace_id = current_trace_id.get()
    is_root = trace_id is None
    parent_id = current_span_id.get()
    now = time.time()
    span_id = new_id()

    trace_tok = None
    if is_root:
        trace_id = new_id()
        storage.insert_trace(trace_id, f"bedrock:{model}", now)
        trace_tok = current_trace_id.set(trace_id)

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=f"bedrock.{operation}:{model}",
        kind="llm_call",
        started_at=now,
        input_data={"model": model, "kwargs": _redact_kwargs(kwargs)},
        attrs={
            "model": model,
            "provider": "bedrock",
            "streaming": operation == "invoke_model_with_response_stream",
        },
    )
    return span_id, trace_id, is_root, trace_tok  # type: ignore[return-value]


def _redact_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    safe = {"modelId", "accept", "contentType", "guardrailIdentifier", "guardrailVersion"}
    out = {key: value for key, value in kwargs.items() if key in safe}
    body = kwargs.get("body")
    if body is not None:
        out["body"] = _safe_json_body(body)
    return out


def _safe_json_body(body: Any) -> Any:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return "<non-string body>"
    try:
        parsed = json.loads(body)
    except ValueError:
        return "<non-json body>"
    if isinstance(parsed, dict):
        return {key: parsed[key] for key in parsed if key not in {"api_key", "password", "token"}}
    return parsed


def _extract_response_summary(resp: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if not isinstance(resp, dict):
        return summary

    body = resp.get("body")
    payload: bytes | None = None
    if hasattr(body, "read"):
        try:
            payload = body.read()
        except Exception:
            payload = None
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if isinstance(payload, bytes):
            resp["body"] = _BufferedBody(body, payload)
    elif isinstance(body, bytes):
        payload = body
    elif isinstance(body, str):
        payload = body.encode("utf-8")

    if not payload:
        return summary

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return summary

    summary["response"] = data
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        summary["usage"] = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    return summary


def _finish(
    span_id: str,
    trace_id: str,
    is_root: bool,
    trace_tok,
    resp: Any | None,
    exc: BaseException | None,
    *,
    model: str,
    streaming: bool,
) -> None:
    if exc is not None:
        error_type = type(exc).__name__
        error_message = str(exc)[:1000]
        storage.finish_span(span_id, time.time(), "error", error_type=error_type, error_message=error_message)
        if is_root:
            storage.finish_trace(trace_id, time.time(), "error", error_type=error_type, error_message=error_message)
    else:
        summary = _extract_response_summary(resp)
        attrs = {"model": model, "provider": "bedrock", "streaming": streaming}
        usage = summary.get("usage")
        if isinstance(usage, dict):
            attrs.update(
                {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
            )
        storage.finish_span(span_id, time.time(), "ok", output_data=summary, attrs=attrs)
        if is_root:
            storage.finish_trace(trace_id, time.time(), "ok")
    if is_root and trace_tok is not None:
        current_trace_id.reset(trace_tok)


class _WrappedClient:
    def __init__(self, client: Any) -> None:
        self._client = client

    def invoke_model(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("modelId") or (args[0] if args else "unknown")
        span_id, trace_id, is_root, trace_tok = _record_span("invoke_model", model, kwargs)
        try:
            resp = self._client.invoke_model(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc, model=model, streaming=False)
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None, model=model, streaming=False)
        return resp

    def invoke_model_with_response_stream(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("modelId") or (args[0] if args else "unknown")
        span_id, trace_id, is_root, trace_tok = _record_span("invoke_model_with_response_stream", model, kwargs)
        try:
            resp = self._client.invoke_model_with_response_stream(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc, model=model, streaming=True)
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None, model=model, streaming=True)
        return resp

    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)


def wrap_bedrock(client: Any) -> Any:
    """Return a wrapped boto3 Bedrock runtime client whose model calls are logged."""
    return _WrappedClient(client)
