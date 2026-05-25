"""Explicit wrapper for the google-genai SDK client."""
from __future__ import annotations

import time
from typing import Any, Iterable

from clustertrace import storage
from clustertrace._ctx import current_span_id, current_trace_id, new_id


def _record_span(operation: str, model: str, kwargs: dict[str, Any]) -> tuple[str, str, bool, Any]:
    trace_id = current_trace_id.get()
    is_root = trace_id is None
    parent_id = current_span_id.get()
    now = time.time()
    span_id = new_id()

    trace_tok = None
    if is_root:
        trace_id = new_id()
        storage.insert_trace(trace_id, f"gemini:{model}", now)
        trace_tok = current_trace_id.set(trace_id)

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=f"gemini.models.{operation}:{model}",
        kind="llm_call",
        started_at=now,
        input_data={"model": model, "kwargs": _redact_kwargs(kwargs)},
        attrs={
            "model": model,
            "provider": "gemini",
            "streaming": operation.endswith("stream"),
        },
    )
    return span_id, trace_id, is_root, trace_tok  # type: ignore[return-value]


def _redact_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    safe = {"model", "contents", "config"}
    return {key: value for key, value in kwargs.items() if key in safe}


def _extract_response_summary(resp: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for attr in ("model_version", "text"):
        if hasattr(resp, attr):
            try:
                value = getattr(resp, attr)
            except Exception:
                continue
            summary[attr] = value[:2000] if isinstance(value, str) else value

    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        summary["usage"] = {
            "input_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
        }
    return summary


def _attrs_from_summary(summary: dict[str, Any], model: str, streaming: bool) -> dict[str, Any]:
    attrs: dict[str, Any] = {"model": model, "provider": "gemini", "streaming": streaming}
    usage = summary.get("usage")
    if isinstance(usage, dict):
        attrs.update(
            {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        )
    return attrs


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
        summary = _extract_response_summary(resp) if resp is not None else {}
        storage.finish_span(
            span_id,
            time.time(),
            "ok",
            output_data=summary,
            attrs=_attrs_from_summary(summary, model, streaming),
        )
        if is_root:
            storage.finish_trace(trace_id, time.time(), "ok")
    if is_root and trace_tok is not None:
        current_trace_id.reset(trace_tok)


class _WrappedStream:
    def __init__(self, inner: Iterable[Any], on_finish) -> None:
        self._inner = iter(inner)
        self._on_finish = on_finish
        self._last_chunk = None
        self._finished = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._inner)
        except StopIteration:
            self._finish()
            raise
        self._last_chunk = chunk
        return chunk

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._on_finish(self._last_chunk)


class _WrappedModels:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        span_id, trace_id, is_root, trace_tok = _record_span("generate_content", model, kwargs)
        try:
            resp = self._inner.generate_content(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc, model=model, streaming=False)
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None, model=model, streaming=False)
        return resp

    def generate_content_stream(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        span_id, trace_id, is_root, trace_tok = _record_span("generate_content_stream", model, kwargs)
        try:
            stream = self._inner.generate_content_stream(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc, model=model, streaming=True)
            raise

        def finish(last_chunk):
            _finish(span_id, trace_id, is_root, trace_tok, last_chunk, None, model=model, streaming=True)

        return _WrappedStream(stream, finish)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WrappedClient:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.models = _WrappedModels(client.models)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)


def wrap_gemini(client: Any) -> Any:
    """Return a wrapped google-genai client whose model calls are logged."""
    return _WrappedClient(client)
