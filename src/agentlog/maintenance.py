"""Cleanup, retention, and graceful shutdown helpers.

agentlog writes synchronously, so there's no buffer to flush in the
network-IO sense. What there *is*: traces that started but didn't finish
(process killed, crashed, debugger detached) and stay in `status='running'`
forever. These helpers finalize them.
"""
from __future__ import annotations

import re
import time

from agentlog import storage

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$")


def parse_duration(s: str) -> int:
    """Parse '7d' / '24h' / '30m' / '60s' / '2w' into a seconds count."""
    m = _DURATION_RE.match(s.strip().lower())
    if not m:
        raise ValueError(f"invalid duration {s!r} — expected '7d', '24h', '30m', '60s', '2w'")
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def cleanup_orphans(stale_after_seconds: float = 300.0) -> int:
    """Mark traces still in 'running' state as 'incomplete' once they've gone stale.

    Default stale threshold: 5 minutes (enough for any reasonable agent).
    Returns the number of traces flipped.
    """
    cutoff = time.time() - stale_after_seconds
    with storage.connect() as conn:
        rows = conn.execute(
            """SELECT id FROM traces
               WHERE status = 'running' AND started_at < ?""",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        # End time = latest completed child span, or the cutoff if none
        for tid in ids:
            last = conn.execute(
                """SELECT MAX(COALESCE(ended_at, started_at)) FROM spans WHERE trace_id = ?""",
                (tid,),
            ).fetchone()
            ended = last[0] if last and last[0] else cutoff
            conn.execute(
                """UPDATE traces
                   SET status = 'incomplete',
                       ended_at = COALESCE(ended_at, ?),
                       error_type = COALESCE(error_type, 'IncompleteTrace'),
                       error_message = COALESCE(error_message, 'process exited before trace finished')
                   WHERE id = ?""",
                (ended, tid),
            )
        # Also flip any orphan running spans inside those traces
        conn.execute(
            f"""UPDATE spans
                SET status = 'incomplete',
                    ended_at = COALESCE(ended_at, ?)
                WHERE status = 'running' AND trace_id IN ({placeholders})""",
            (cutoff, *ids),
        )
    return len(rows)


def flush() -> int:
    """Best-effort: finalize any orphan traces with no in-flight children.

    Safe to call before process exit. Synchronous and idempotent.
    Returns the number of traces finalized.
    """
    return cleanup_orphans(stale_after_seconds=0.0)


def vacuum(older_than_seconds: int, dry_run: bool = False) -> tuple[int, int]:
    """Delete traces older than the cutoff. Returns (n_traces_deleted, bytes_freed).

    bytes_freed is approximate — taken before/after `PRAGMA page_count`.
    """
    cutoff = time.time() - older_than_seconds
    with storage.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM traces WHERE started_at < ?", (cutoff,)
        ).fetchone()[0]
        if dry_run or n == 0:
            return n, 0
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        before = conn.execute("PRAGMA page_count").fetchone()[0]
        # ON DELETE CASCADE handles spans, tags, metrics
        conn.execute("DELETE FROM traces WHERE started_at < ?", (cutoff,))
        # VACUUM reclaims space but can't run inside a transaction; with
        # autocommit (isolation_level=None) this is fine.
        conn.execute("VACUUM")
        after = conn.execute("PRAGMA page_count").fetchone()[0]
    return n, max(0, (before - after) * page_size)
