"""`clustertrace inspect` — render one trace as a rich terminal view.

The output is one screen of header + Gantt + tree, optionally followed by
expanded I/O for selected spans. No network, no live updates: a one-shot
render so `inspect` works over SSH where you can't pop a browser.

Layout choices:
  - Header is a `rich.Panel` with id / name / status / duration / cost / tags.
  - Gantt is an ASCII bar built into a `rich.Table` so widths stay consistent.
  - Span tree uses `rich.Tree` with status icons (✓ ok, ✗ error, ◌ running).
  - Error spans print the full error_type + message under their tree node.

The Gantt's bar character is plain `█` — works everywhere `rich`'s default
font does. ANSI is auto-disabled when stdout isn't a TTY (rich's default)
plus a `--no-color` escape for forced plain text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from clustertrace import storage


class InspectError(RuntimeError):
    """User-facing error: no trace, ambiguous selector, etc."""


_OK = "✓"
_ERR = "✗"
_RUN = "◌"


def _status_icon(status: str | None) -> str:
    if status == "ok":
        return _OK
    if status == "error":
        return _ERR
    if status == "running":
        return _RUN
    return "?"


def _status_style(status: str | None) -> str:
    if status == "ok":
        return "green"
    if status == "error":
        return "red bold"
    if status == "running":
        return "yellow"
    return "dim"


@dataclass
class _Span:
    id: str
    parent_id: str | None
    name: str
    kind: str
    status: str
    started_at: float
    ended_at: float | None
    error_type: str | None
    error_message: str | None
    input_json: str | None
    output_json: str | None
    attrs_json: str | None

    @property
    def duration_s(self) -> float | None:
        if self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)


@dataclass
class _Trace:
    id: str
    name: str
    status: str
    started_at: float
    ended_at: float | None
    error_type: str | None
    error_message: str | None
    signature: str | None
    cost_usd: float | None
    spans: list[_Span]
    tags: dict[str, str]

    @property
    def duration_s(self) -> float | None:
        if self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)


def resolve_trace_id(
    trace_id: str | None, latest: bool = False, failed: bool = False
) -> str:
    """Resolve the trace to render given the CLI flags.

    `--failed` takes precedence over `--latest`. If `trace_id` is supplied,
    flags are ignored and we just verify the id exists.
    """
    with storage.connect() as conn:
        if failed:
            row = conn.execute(
                "SELECT id FROM traces WHERE status='error' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise InspectError("no failed traces found")
            return row["id"]
        if trace_id:
            row = conn.execute(
                "SELECT id FROM traces WHERE id = ?", (trace_id,)
            ).fetchone()
            if row is None:
                raise InspectError(f"trace not found: {trace_id}")
            return row["id"]
        if latest:
            row = conn.execute(
                "SELECT id FROM traces ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise InspectError("no traces found")
            return row["id"]
    raise InspectError(
        "provide a trace id, or use --latest / --failed to auto-select one"
    )


def load_trace(trace_id: str) -> _Trace:
    """Load a trace + spans + tags from SQLite."""
    with storage.connect() as conn:
        t = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
        if t is None:
            raise InspectError(f"trace not found: {trace_id}")
        span_rows = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at ASC",
            (trace_id,),
        ).fetchall()
        tag_rows = conn.execute(
            "SELECT key, value FROM trace_tags WHERE trace_id = ?", (trace_id,)
        ).fetchall()
    spans = [
        _Span(
            id=s["id"],
            parent_id=s["parent_id"],
            name=s["name"],
            kind=s["kind"],
            status=s["status"],
            started_at=s["started_at"],
            ended_at=s["ended_at"],
            error_type=s["error_type"],
            error_message=s["error_message"],
            input_json=s["input_json"],
            output_json=s["output_json"],
            attrs_json=s["attrs_json"],
        )
        for s in span_rows
    ]
    return _Trace(
        id=t["id"],
        name=t["name"],
        status=t["status"],
        started_at=t["started_at"],
        ended_at=t["ended_at"],
        error_type=t["error_type"],
        error_message=t["error_message"],
        signature=t["signature"],
        cost_usd=t["cost_usd"],
        spans=spans,
        tags={r["key"]: r["value"] for r in tag_rows},
    )


# ---------------------------------------------------------------------------
# Rendering. All `rich` imports are local so importing this module without
# `rich` doesn't crash — `rich` IS a hard dep, but optional install of
# clustertrace[mcp] etc. should still let us inspect without rich loaded.
# ---------------------------------------------------------------------------


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.2f}s"
    return f"{seconds / 60:.2f}m"


def _bar_for(span: _Span, t0: float, total: float, width: int) -> str:
    """ASCII Gantt bar: leading spaces (offset) + filled blocks (duration).

    Width never overflows; minimum 1 cell so a 0-duration span still appears.
    """
    if total <= 0 or width <= 0:
        return ""
    start_frac = max(0.0, (span.started_at - t0) / total)
    end = span.ended_at if span.ended_at is not None else span.started_at
    dur_frac = max(0.0, (end - span.started_at) / total) if total > 0 else 0.0
    start_cells = int(round(start_frac * width))
    dur_cells = max(1, int(round(dur_frac * width)))
    if start_cells + dur_cells > width:
        dur_cells = max(1, width - start_cells)
    return " " * start_cells + "█" * dur_cells


def _decode_json(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def render_to_console(
    trace_id: str,
    expand_span_ids: set[str] | None = None,
    width: int | None = None,
    no_color: bool = False,
    file: Any = None,
) -> None:
    """Render to the live console (or `file` if given).

    `expand_span_ids` controls which spans get their I/O JSON dumped inline.
    `width` overrides the terminal width (useful for tests / docs); falls
    back to the `rich` auto-detected width.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    trace = load_trace(trace_id)
    console = Console(
        file=file,
        width=width,
        no_color=no_color,
        force_terminal=False,
        soft_wrap=False,
        legacy_windows=False,
    )
    # --- Header --------------------------------------------------------------
    header_lines: list[str] = []
    header_lines.append(f"[bold]id[/]      {trace.id}")
    header_lines.append(f"[bold]name[/]    {trace.name}")
    status_style = _status_style(trace.status)
    header_lines.append(
        f"[bold]status[/]  [{status_style}]{_status_icon(trace.status)} {trace.status}[/]"
    )
    header_lines.append(f"[bold]dur[/]     {_format_duration(trace.duration_s)}")
    if trace.cost_usd is not None and trace.cost_usd > 0:
        header_lines.append(f"[bold]cost[/]    ${trace.cost_usd:.6f}")
    if trace.tags:
        tag_str = "  ".join(f"{k}={v}" for k, v in sorted(trace.tags.items()))
        header_lines.append(f"[bold]tags[/]    {tag_str}")
    if trace.status == "error" and trace.error_type:
        header_lines.append(
            f"[bold red]error[/]   {trace.error_type}: {trace.error_message or ''}"
        )
    if trace.signature:
        header_lines.append(f"[bold]sig[/]     {trace.signature[:160]}")
    console.print(
        Panel(
            "\n".join(header_lines),
            title=f"trace {trace.id[:12]}",
            border_style=status_style,
            expand=True,
        )
    )

    # --- Gantt ---------------------------------------------------------------
    if not trace.spans:
        console.print("[dim]no spans recorded[/]")
        return

    t0 = trace.started_at
    end = trace.ended_at if trace.ended_at is not None else max(
        (s.ended_at or s.started_at for s in trace.spans), default=t0
    )
    total = max(end - t0, 1e-9)

    # Compute available width for the bar column: total - (name+dur+icon columns).
    name_max = max((len(s.name) for s in trace.spans), default=10)
    name_col = min(name_max, 28)
    icon_col = 2
    dur_col = 8
    margins = 6  # column gutters from rich's Table
    avail = console.size.width if console.size.width else 100
    bar_col = max(10, avail - name_col - dur_col - icon_col - margins)

    table = Table(
        show_header=True,
        header_style="bold",
        expand=False,
        padding=(0, 1),
        box=None,
    )
    table.add_column(" ", width=icon_col, no_wrap=True)
    table.add_column("span", width=name_col, no_wrap=True, overflow="ellipsis")
    table.add_column("gantt", width=bar_col, no_wrap=True)
    table.add_column("dur", width=dur_col, no_wrap=True, justify="right")
    for s in trace.spans:
        if s.parent_id is None:
            # Skip the root span in the Gantt — its bar spans the whole row
            # which adds visual noise without information.
            continue
        bar = _bar_for(s, t0, total, bar_col)
        style = _status_style(s.status)
        table.add_row(
            Text(_status_icon(s.status), style=style),
            Text(s.name, style=style),
            Text(bar, style=style),
            _format_duration(s.duration_s),
        )
    console.print(table)

    # --- Tree ----------------------------------------------------------------
    children: dict[str | None, list[_Span]] = {}
    for s in trace.spans:
        children.setdefault(s.parent_id, []).append(s)

    roots = children.get(None, [])
    if not roots:
        # No root span; treat the topologically-first span as the entry.
        roots = sorted(trace.spans, key=lambda s: s.started_at)[:1]

    def _build(node: Tree, span: _Span) -> None:
        for child in children.get(span.id, []):
            label = Text()
            label.append(
                f"{_status_icon(child.status)} ", style=_status_style(child.status)
            )
            label.append(child.name)
            label.append(f"  ({_format_duration(child.duration_s)})", style="dim")
            sub = node.add(label)
            if child.status == "error" and child.error_type:
                sub.add(
                    Text(
                        f"{child.error_type}: {child.error_message or ''}",
                        style="red",
                    )
                )
            if expand_span_ids and child.id in expand_span_ids:
                _attach_io(sub, child)
            _build(sub, child)

    console.print("[bold]spans:[/]")
    for r in roots:
        label = Text()
        label.append(
            f"{_status_icon(r.status)} ", style=_status_style(r.status)
        )
        label.append(r.name)
        label.append(f"  ({_format_duration(r.duration_s)})", style="dim")
        root_tree = Tree(label)
        if r.status == "error" and r.error_type:
            root_tree.add(
                Text(
                    f"{r.error_type}: {r.error_message or ''}",
                    style="red",
                )
            )
        if expand_span_ids and r.id in expand_span_ids:
            _attach_io(root_tree, r)
        _build(root_tree, r)
        console.print(root_tree)


def _attach_io(node: Any, span: _Span) -> None:
    """Render the input / output / attrs of a span under a Tree node."""
    from rich.syntax import Syntax

    for label, raw in (
        ("input", span.input_json),
        ("output", span.output_json),
        ("attrs", span.attrs_json),
    ):
        decoded = _decode_json(raw)
        if decoded is None:
            continue
        sub = node.add(f"[bold]{label}[/]")
        try:
            text = json.dumps(decoded, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(decoded)
        sub.add(Syntax(text, "json", theme="ansi_dark", background_color="default"))
