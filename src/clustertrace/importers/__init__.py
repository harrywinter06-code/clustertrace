"""Competitor-format importers.

Each module here parses one tool's export format and writes the resulting
traces/spans/tags/metrics through `clustertrace.storage`. Importers are
append-only: a source trace ID that collides with an existing clustertrace
trace ID is skipped, never overwritten.

To avoid cross-source ID collisions when the same UUID happens to appear in
two tools' exports, every importer prefixes its trace/span IDs with a source
tag (`langfuse:`, `phoenix:`, `langsmith:`, `otel:`).

Importers reuse `storage.insert_trace`/`insert_span`/`add_trace_tag`/
`set_metric` — they do not write SQL directly. Each returns
`(imported_traces, skipped_lines_or_traces)`.

Mapping conventions (every importer normalizes onto these attribute keys):
    attrs["model"]          — model name (e.g. "claude-haiku-4-5-20251001")
    attrs["input_tokens"]   — prompt / input token count (int)
    attrs["output_tokens"]  — completion / output token count (int)
    attrs["streaming"]      — bool, if the source records it
"""
from __future__ import annotations

from collections.abc import Callable
from typing import IO

from clustertrace.importers.langfuse import import_langfuse

# Stream input is either a file-like object or any iterable of strings.
ImporterFn = Callable[[IO[str]], tuple[int, int]]

SOURCES: dict[str, ImporterFn] = {
    "langfuse": import_langfuse,
}

SUPPORTED = ", ".join(sorted(SOURCES.keys()))

__all__ = [
    "SOURCES",
    "SUPPORTED",
    "import_langfuse",
]
