"""Shared contextvars for tracing.

Lives in its own module so other internal modules (e.g. clustertrace.anthropic)
can import without colliding with the public `clustertrace.trace` decorator name.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

current_trace_id: ContextVar[str | None] = ContextVar("clustertrace_trace_id", default=None)
current_span_id: ContextVar[str | None] = ContextVar("clustertrace_span_id", default=None)


def new_id() -> str:
    return uuid.uuid4().hex
