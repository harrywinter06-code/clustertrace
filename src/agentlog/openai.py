"""Explicit wrapper for the OpenAI SDK client.

Captures `client.chat.completions.create` calls (sync + async). No global
monkey-patch — pass the wrapped client where you'd pass the original.
"""
from __future__ import annotations

import time
from typing import Any

from agentlog import storage
from agentlog._ctx import current_span_id, current_trace_id, new_id


def _record_completion_span(model: str, messages: Any, kwargs: dict[str, Any]) -> tuple[str, str, bool, Any]:
    """Open a chat.completions.create span. Returns (span_id, trace_id, is_root, trace_tok)."""
    trace_id = current_trace_id.get()
    is_root = trace_id is None
    parent_id = current_span_id.get()
    now = time.time()
    span_id = new_id()

    trace_tok = None
    if is_root:
        trace_id = new_id()
        storage.insert_trace(trace_id, f"openai:{model}", now)
        trace_tok = current_trace_id.set(trace_id)

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=f"openai.chat.completions.create:{model}",
        kind="llm_call",
        started_at=now,
        input_data={"messages": messages, "model": model, "kwargs": _redact_kwargs(kwargs)},
        attrs={"model": model, "provider": "openai"},
    )
    return span_id, trace_id, is_root, trace_tok  # type: ignore[return-value]


def _redact_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    safe = {
        "max_tokens", "max_completion_tokens", "temperature", "top_p", "n",
        "tools", "tool_choice", "stop", "stream", "response_format",
        "seed", "frequency_penalty", "presence_penalty",
    }
    return {k: v for k, v in kwargs.items() if k in safe}


def _extract_response_summary(resp: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for attr in ("id", "model", "object", "created"):
        if hasattr(resp, attr):
            try:
                summary[attr] = getattr(resp, attr)
            except Exception:
                pass
    if hasattr(resp, "usage"):
        try:
            u = resp.usage
            summary["usage"] = {
                "prompt_tokens": getattr(u, "prompt_tokens", None),
                "completion_tokens": getattr(u, "completion_tokens", None),
                "total_tokens": getattr(u, "total_tokens", None),
            }
        except Exception:
            pass
    if hasattr(resp, "choices"):
        try:
            choices_out = []
            for ch in resp.choices:
                msg = getattr(ch, "message", None)
                content = getattr(msg, "content", None) if msg else None
                tool_calls = getattr(msg, "tool_calls", None) if msg else None
                tc_out = None
                if tool_calls:
                    tc_out = []
                    for tc in tool_calls:
                        fn = getattr(tc, "function", None)
                        tc_out.append({
                            "id": getattr(tc, "id", None),
                            "type": getattr(tc, "type", None),
                            "function": {
                                "name": getattr(fn, "name", None) if fn else None,
                                "arguments": getattr(fn, "arguments", None) if fn else None,
                            } if fn else None,
                        })
                choices_out.append({
                    "index": getattr(ch, "index", None),
                    "finish_reason": getattr(ch, "finish_reason", None),
                    "message": {
                        "role": getattr(msg, "role", None) if msg else None,
                        "content": (content or "")[:2000] if isinstance(content, str) else content,
                        "tool_calls": tc_out,
                    } if msg else None,
                })
            summary["choices"] = choices_out
        except Exception:
            summary["choices"] = repr(resp.choices)[:1000]
    return summary


def _finish(span_id: str, trace_id: str, is_root: bool, trace_tok, resp: Any | None, exc: BaseException | None) -> None:
    if exc is not None:
        error_type = type(exc).__name__
        error_message = str(exc)[:1000]
        storage.finish_span(
            span_id, time.time(), "error", error_type=error_type, error_message=error_message
        )
        if is_root:
            storage.finish_trace(trace_id, time.time(), "error", error_type=error_type, error_message=error_message)
    else:
        summary = _extract_response_summary(resp)
        attrs = None
        usage = summary.get("usage")
        if isinstance(usage, dict):
            attrs = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "model": summary.get("model"),
            }
        storage.finish_span(span_id, time.time(), "ok", output_data=summary, attrs=attrs)
        if is_root:
            storage.finish_trace(trace_id, time.time(), "ok")
    if is_root and trace_tok is not None:
        current_trace_id.reset(trace_tok)


class _WrappedCompletions:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        messages = kwargs.get("messages")
        span_id, trace_id, is_root, trace_tok = _record_completion_span(model, messages, kwargs)
        try:
            resp = self._inner.create(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc)
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None)
        return resp

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WrappedAsyncCompletions:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model") or (args[0] if args else "unknown")
        messages = kwargs.get("messages")
        span_id, trace_id, is_root, trace_tok = _record_completion_span(model, messages, kwargs)
        try:
            resp = await self._inner.create(*args, **kwargs)
        except BaseException as exc:
            _finish(span_id, trace_id, is_root, trace_tok, None, exc)
            raise
        _finish(span_id, trace_id, is_root, trace_tok, resp, None)
        return resp

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WrappedChat:
    def __init__(self, inner: Any, is_async: bool) -> None:
        self._inner = inner
        if is_async:
            self.completions = _WrappedAsyncCompletions(inner.completions)
        else:
            self.completions = _WrappedCompletions(inner.completions)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WrappedClient:
    def __init__(self, client: Any, is_async: bool) -> None:
        self._client = client
        self.chat = _WrappedChat(client.chat, is_async=is_async)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._client, item)


def wrap_openai(client: Any) -> Any:
    """Return a wrapped client whose chat.completions.create calls are logged.

    Detects sync vs. async via class name (AsyncOpenAI).
    """
    is_async = "Async" in type(client).__name__
    return _WrappedClient(client, is_async=is_async)
