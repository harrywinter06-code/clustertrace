"""Tracing primitives: @trace decorator, span context manager, tool_call logger.

Works for both sync and async functions. Spans nest via a contextvar, so
async tasks see their parent without manual plumbing.
"""
from __future__ import annotations

import functools
import inspect
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from agentlog import storage
from agentlog._ctx import current_span_id as _current_span_id
from agentlog._ctx import current_trace_id as _current_trace_id
from agentlog._ctx import new_id as _new_id


def tag(key: str, value: str | int | float | bool) -> None:
    """Attach a key=value tag to the currently active trace.

    No-op if called outside a trace. Useful for filtering in the dashboard.
    """
    tid = _current_trace_id.get()
    if tid is None:
        return
    try:
        storage.add_trace_tag(tid, str(key), str(value))
    except Exception:
        pass


def metric(name: str, value: float | int | bool) -> None:
    """Attach a numeric metric (eval score, latency target, pass/fail) to the current trace.

    Stored separately from tags so the dashboard can aggregate over time.
    No-op outside a trace.
    """
    tid = _current_trace_id.get()
    if tid is None:
        return
    try:
        storage.set_metric(tid, str(name), float(value), time.time())
    except Exception:
        pass

F = TypeVar("F", bound=Callable[..., Any])


def _safe_repr_args(args: tuple, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Best-effort capture of call args. Avoids touching `self`/`cls`."""
    out: dict[str, Any] = {}
    if args:
        positional = []
        for a in args:
            if a.__class__.__name__ in {"type", "module"}:
                positional.append(repr(a))
            else:
                positional.append(a)
        out["args"] = positional
    if kwargs:
        out["kwargs"] = kwargs
    return out


def _exc_info(exc: BaseException) -> tuple[str, str]:
    error_type = type(exc).__name__
    error_message = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return error_type, error_message


def trace(
    _fn: F | None = None,
    *,
    name: str | None = None,
    tags: dict[str, Any] | None = None,
) -> Any:
    """Capture inputs, outputs, exceptions, and duration of a function.

    Use as @trace, @trace(name="..."), or @trace(tags={"agent": "researcher"}).
    Works for sync and async. Tags attach only to the root trace; nested calls
    inside a running trace ignore them (use `agentlog.tag()` for that).
    """

    def decorator(fn: F) -> F:
        span_name = name or fn.__qualname__
        is_coro = inspect.iscoroutinefunction(fn)

        if is_coro:

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with _async_call_span(fn, span_name, args, kwargs, tags) as set_result:
                    result = await fn(*args, **kwargs)
                    set_result(result)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _sync_call_span(fn, span_name, args, kwargs, tags) as set_result:
                result = fn(*args, **kwargs)
                set_result(result)
                return result

        return sync_wrapper  # type: ignore[return-value]

    if _fn is not None and callable(_fn):
        return decorator(_fn)
    return decorator


@contextmanager
def _sync_call_span(
    fn: Callable[..., Any],
    span_name: str,
    args: tuple,
    kwargs: dict[str, Any],
    tags: dict[str, Any] | None = None,
) -> Iterator[Callable[[Any], None]]:
    trace_id = _current_trace_id.get()
    is_root = trace_id is None
    span_id = _new_id()
    parent_id = _current_span_id.get()
    started_at = time.time()

    if is_root:
        trace_id = _new_id()
        storage.insert_trace(trace_id, span_name, started_at)
        trace_tok = _current_trace_id.set(trace_id)
        if tags:
            for k, v in tags.items():
                storage.add_trace_tag(trace_id, str(k), str(v))
    else:
        trace_tok = None

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=span_name,
        kind="function",
        started_at=started_at,
        input_data=_safe_repr_args(args, kwargs),
        attrs={"module": getattr(fn, "__module__", None)},
    )
    span_tok = _current_span_id.set(span_id)

    captured: dict[str, Any] = {}

    def set_result(value: Any) -> None:
        captured["result"] = value

    try:
        yield set_result
    except BaseException as exc:
        error_type, error_message = _exc_info(exc)
        storage.finish_span(
            span_id, time.time(), "error", error_type=error_type, error_message=error_message
        )
        if is_root:
            storage.finish_trace(
                trace_id,  # type: ignore[arg-type]
                time.time(),
                "error",
                error_type=error_type,
                error_message=error_message,
            )
        raise
    else:
        storage.finish_span(span_id, time.time(), "ok", output_data=captured.get("result"))
        if is_root:
            storage.finish_trace(trace_id, time.time(), "ok")  # type: ignore[arg-type]
    finally:
        _current_span_id.reset(span_tok)
        if trace_tok is not None:
            _current_trace_id.reset(trace_tok)


class _AsyncCallSpan:
    """Async-compatible analog of _sync_call_span."""

    def __init__(
        self,
        fn: Callable[..., Any],
        span_name: str,
        args: tuple,
        kwargs: dict[str, Any],
        tags: dict[str, Any] | None = None,
    ) -> None:
        self.fn = fn
        self.span_name = span_name
        self.args = args
        self.kwargs = kwargs
        self.tags = tags
        self.captured: dict[str, Any] = {}
        self.trace_id: str | None = None
        self.span_id: str | None = None
        self.trace_tok = None
        self.span_tok = None
        self.is_root = False

    async def __aenter__(self) -> Callable[[Any], None]:
        trace_id = _current_trace_id.get()
        self.is_root = trace_id is None
        self.span_id = _new_id()
        parent_id = _current_span_id.get()
        started_at = time.time()

        if self.is_root:
            trace_id = _new_id()
            storage.insert_trace(trace_id, self.span_name, started_at)
            self.trace_tok = _current_trace_id.set(trace_id)
            if self.tags:
                for k, v in self.tags.items():
                    storage.add_trace_tag(trace_id, str(k), str(v))

        self.trace_id = trace_id
        storage.insert_span(
            span_id=self.span_id,
            trace_id=trace_id,  # type: ignore[arg-type]
            parent_id=parent_id,
            name=self.span_name,
            kind="function",
            started_at=started_at,
            input_data=_safe_repr_args(self.args, self.kwargs),
            attrs={"module": getattr(self.fn, "__module__", None), "async": True},
        )
        self.span_tok = _current_span_id.set(self.span_id)

        def set_result(value: Any) -> None:
            self.captured["result"] = value

        return set_result

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc is not None:
                error_type, error_message = _exc_info(exc)
                storage.finish_span(
                    self.span_id,  # type: ignore[arg-type]
                    time.time(),
                    "error",
                    error_type=error_type,
                    error_message=error_message,
                )
                if self.is_root:
                    storage.finish_trace(
                        self.trace_id,  # type: ignore[arg-type]
                        time.time(),
                        "error",
                        error_type=error_type,
                        error_message=error_message,
                    )
            else:
                storage.finish_span(
                    self.span_id,  # type: ignore[arg-type]
                    time.time(),
                    "ok",
                    output_data=self.captured.get("result"),
                )
                if self.is_root:
                    storage.finish_trace(
                        self.trace_id,  # type: ignore[arg-type]
                        time.time(),
                        "ok",
                    )
        finally:
            if self.span_tok is not None:
                _current_span_id.reset(self.span_tok)
            if self.trace_tok is not None:
                _current_trace_id.reset(self.trace_tok)
        return False


def _async_call_span(fn, span_name, args, kwargs, tags=None):
    return _AsyncCallSpan(fn, span_name, args, kwargs, tags)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[None]:
    """Context manager that creates a nested span.

    Works inside sync OR async code (async tasks inherit the parent via contextvars).
    """
    trace_id = _current_trace_id.get()
    is_root = trace_id is None
    span_id = _new_id()
    parent_id = _current_span_id.get()
    started_at = time.time()

    trace_tok = None
    if is_root:
        trace_id = _new_id()
        storage.insert_trace(trace_id, name, started_at)
        trace_tok = _current_trace_id.set(trace_id)

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=name,
        kind="span",
        started_at=started_at,
        attrs=attrs or None,
    )
    span_tok = _current_span_id.set(span_id)
    try:
        yield
    except BaseException as exc:
        error_type, error_message = _exc_info(exc)
        storage.finish_span(
            span_id, time.time(), "error", error_type=error_type, error_message=error_message
        )
        if is_root:
            storage.finish_trace(
                trace_id, time.time(), "error", error_type=error_type, error_message=error_message  # type: ignore[arg-type]
            )
        raise
    else:
        storage.finish_span(span_id, time.time(), "ok")
        if is_root:
            storage.finish_trace(trace_id, time.time(), "ok")  # type: ignore[arg-type]
    finally:
        _current_span_id.reset(span_tok)
        if trace_tok is not None:
            _current_trace_id.reset(trace_tok)


def tool_call(name: str, args: Any = None, result: Any = None, error: BaseException | None = None) -> None:
    """Log a single tool invocation as a span.

    If called outside a trace, an implicit single-span trace is opened.
    """
    trace_id = _current_trace_id.get()
    is_root = trace_id is None
    parent_id = _current_span_id.get()
    now = time.time()
    span_id = _new_id()

    trace_tok = None
    if is_root:
        trace_id = _new_id()
        storage.insert_trace(trace_id, f"tool_call:{name}", now)
        trace_tok = _current_trace_id.set(trace_id)

    storage.insert_span(
        span_id=span_id,
        trace_id=trace_id,  # type: ignore[arg-type]
        parent_id=parent_id,
        name=name,
        kind="tool_call",
        started_at=now,
        input_data=args,
    )
    if error is not None:
        error_type, error_message = _exc_info(error)
        storage.finish_span(
            span_id,
            time.time(),
            "error",
            output_data=result,
            error_type=error_type,
            error_message=error_message,
        )
        status = "error"
    else:
        storage.finish_span(span_id, time.time(), "ok", output_data=result)
        status = "ok"

    if is_root:
        storage.finish_trace(
            trace_id,  # type: ignore[arg-type]
            time.time(),
            status,
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
        if trace_tok is not None:
            _current_trace_id.reset(trace_tok)


def current_trace_id() -> str | None:
    return _current_trace_id.get()


def current_span_id() -> str | None:
    return _current_span_id.get()
