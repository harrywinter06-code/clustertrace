"""Explicit wrapper for the Anthropic SDK client.

Why explicit and not monkey-patch: SDK shape changes silently break global
patches. This module wraps a single client instance, leaving everything else
in your process untouched.
"""
from __future__ import annotations

import time
from typing import Any

from agentlog import storage
from agentlog._ctx import current_span_id, current_trace_id, new_id


def _record_messages_span(model: str, messages: Any, kwargs: dict[str, Any]) -> tuple[str, str, float, bool, Any]:
    """Open a messages.create span. Returns (span_id, trace_id, t0, is_root, trace_tok)."""
    trace_id = current_trace_id.get()
    is_root = trace_id is None
    parent_id = current_span_id.get()
    now = time.time()
    span_id = new_id()

    trace_tok = None
    if is_root:
        trace_id = new_id()
        storage.insert_trace(trace_id, f"anthropic:{model}", now)
        trace_tok = current_trace_id.set(trace_id)

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=f"anthropic.messages.create:{model}",
        kind="llm_call",
        started_at=now,
        input_data={"messages": messages, "model": model, "kwargs": _redact_kwargs(kwargs)},
        attrs={"model": model, "provider": "anthropic", "streaming": _streaming(kwargs)},
    )
    return span_id, trace_id, now, is_root, trace_tok  # type: ignore[return-value]


def _streaming(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("stream"))


def _redact_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Strip out anything that could carry secrets; keep tuning knobs visible."""
    out = {}
    safe_keys = {
        "max_tokens", "temperature", "top_p", "top_k", "system",
        "tools", "tool_choice", "stop_sequences", "stream", "metadata",
    }
    for k, v in kwargs.items():
        if k in safe_keys:
            out[k] = v
    return out


def _extract_response_summary(resp: Any) -> dict[str, Any]:
    """Pull out the useful bits of a Messages response without dumping full SDK objects."""
    summary: dict[str, Any] = {}
    for attr in ("id", "model", "stop_reason", "role"):
        if hasattr(resp, attr):
            try:
                summary[attr] = getattr(resp, attr)
            except Exception:
                pass
    if hasattr(resp, "usage"):
        try:
            u = resp.usage
            summary["usage"] = {
                "input_tokens": getattr(u, "input_tokens", None),
                "output_tokens": getattr(u, "output_tokens", None),
            }
        except Exception:
            pass
    if hasattr(resp, "content"):
        try:
            parts = []
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    parts.append({"type": "text", "text": getattr(block, "text", "")[:2000]})
                elif btype == "tool_use":
                    parts.append({
                        "type": "tool_use",
                        "name": getattr(block, "name", None),
                        "input": getattr(block, "input", None),
                    })
                else:
                    parts.append({"type": btype, "repr": repr(block)[:500]})
            summary["content"] = parts
        except Exception:
            summary["content"] = repr(resp.content)[:1000]
    return summary


def _finish(
    span_id: str,
    trace_id: str,
    is_root: bool,
    trace_tok,
    resp: Any | None,
    exc: BaseException | None,
    streaming: bool = False,
) -> None:
    if exc is not None:
        error_type = type(exc).__name__
        error_message = str(exc)[:1000]
        # Preserve the streaming flag on the failure case too
        err_attrs = {"streaming": streaming} if streaming else None
        storage.finish_span(
            span_id, time.time(), "error", error_type=error_type, error_message=error_message, attrs=err_attrs
        )
        if is_root:
            storage.finish_trace(trace_id, time.time(), "error", error_type=error_type, error_message=error_message)
    else:
        summary = _extract_response_summary(resp)
        attrs: dict[str, Any] | None = None
        usage = summary.get("usage")
        if isinstance(usage, dict):
            attrs = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "model": summary.get("model"),
                "stop_reason": summary.get("stop_reason"),
                "streaming": streaming,
            }
        elif streaming:
            attrs = {"streaming": True}
        storage.finish_span(span_id, time.time(), "ok", output_data=summary, attrs=attrs)
        if is_root:
            storage.finish_trace(trace_id, time.time(), "ok")
    if is_root and trace_tok is not None:
        current_trace_id.reset(trace_tok)


class _WrappedMessages:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        messages = kwargs.get("messages")
        span_id, trace_id, _t0, is_root, trace_tok = _record_messages_span(model, messages, kwargs)
        try:
            resp = self._inner.create(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc, streaming=_streaming(kwargs))
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None, streaming=_streaming(kwargs))
        return resp

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WrappedAsyncMessages:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        messages = kwargs.get("messages")
        span_id, trace_id, _t0, is_root, trace_tok = _record_messages_span(model, messages, kwargs)
        try:
            resp = await self._inner.create(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc, streaming=_streaming(kwargs))
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None, streaming=_streaming(kwargs))
        return resp

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WrappedClient:
    def __init__(self, client: Any, is_async: bool) -> None:
        self._client = client
        if is_async:
            self.messages = _WrappedAsyncMessages(client.messages)
        else:
            self.messages = _WrappedMessages(client.messages)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)


def wrap_anthropic(client: Any) -> Any:
    """Return a wrapped client whose `messages.create` calls are logged.

    Detects sync vs. async clients via the class name (`AsyncAnthropic`).
    """
    is_async = client.__class__.__name__.startswith("Async") or "Async" in type(client).__name__
    return _WrappedClient(client, is_async=is_async)
