"""Trace clustering and failure-signature mining.

A trace's *structural signature* is the ordered sequence of (span_name, status)
pairs for all non-root spans. Two traces with the same signature took the same
execution path; clustering by signature reveals the distinct ways an agent
runs and which ones fail.

LLM-call span names contain the model id, which would fragment clusters every
time you change models. We normalize them to `llm_call:<provider>` so the
clusters track the structural pattern, not the model choice.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from agentlog import storage

_LLM_PREFIXES = ("anthropic.messages.create:", "openai.chat.completions.create:")


def _normalize_name(name: str) -> str:
    for prefix in _LLM_PREFIXES:
        if name.startswith(prefix):
            return prefix.rstrip(":")
    return name


def signature_for_spans(spans: Iterable[dict]) -> str:
    """Compute a stable signature for a trace from its child spans.

    Input rows must have at least: name, status, parent_id, started_at.
    The root span (parent_id IS NULL) is excluded.

    Consecutive identical (normalized_name, status) pairs are collapsed into
    one step — agents loop on the same operation, and a run of N identical
    calls is the same execution pattern regardless of N.
    """
    rows = sorted(
        (s for s in spans if s.get("parent_id") is not None),
        key=lambda s: s["started_at"],
    )
    parts: list[str] = []
    last: str | None = None
    for s in rows:
        token = f"{_normalize_name(s['name'])}:{s['status']}"
        if token != last:
            parts.append(token)
            last = token
    if not parts:
        return "empty"
    return "|".join(parts)


def signature_hash(signature: str) -> str:
    """Short stable hash for use in URLs/UI without showing the full sig."""
    return hashlib.sha1(signature.encode()).hexdigest()[:10]


def compute_and_store_signature(trace_id: str) -> str | None:
    """After a trace finishes, compute its signature and persist it. Returns the sig."""
    with storage.connect() as conn:
        rows = conn.execute(
            """SELECT name, status, parent_id, started_at
               FROM spans
               WHERE trace_id = ?
               ORDER BY started_at""",
            (trace_id,),
        ).fetchall()
    if not rows:
        return None
    sig = signature_for_spans([dict(r) for r in rows])
    storage.set_trace_signature(trace_id, sig)
    return sig


def backfill_signatures() -> int:
    """Compute signatures for any traces missing one. Returns number filled."""
    with storage.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM traces WHERE signature IS NULL AND status != 'running'"
        ).fetchall()]
    n = 0
    for tid in ids:
        if compute_and_store_signature(tid) is not None:
            n += 1
    return n


@dataclass
class Cluster:
    signature: str
    sig_hash: str
    count: int
    error_count: int
    avg_duration_ms: float | None
    representative_trace_id: str
    pattern: list[tuple[str, str]]  # decoded [(name, status), ...]

    @property
    def error_rate(self) -> float:
        return self.error_count / self.count if self.count else 0.0


def list_clusters(limit: int = 50) -> list[Cluster]:
    """Return all distinct trace signatures with counts, sorted by frequency."""
    with storage.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                signature,
                COUNT(*) AS n,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs,
                AVG(CASE WHEN ended_at IS NOT NULL THEN (ended_at-started_at)*1000 END) AS avg_ms,
                (SELECT id FROM traces t2 WHERE t2.signature = traces.signature
                 ORDER BY started_at DESC LIMIT 1) AS rep
            FROM traces
            WHERE signature IS NOT NULL
            GROUP BY signature
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    out: list[Cluster] = []
    for r in rows:
        sig = r["signature"]
        pattern = [tuple(part.split(":", 1)) for part in sig.split("|")] if sig != "empty" else []
        out.append(
            Cluster(
                signature=sig,
                sig_hash=signature_hash(sig),
                count=r["n"],
                error_count=r["errs"] or 0,
                avg_duration_ms=round(r["avg_ms"], 1) if r["avg_ms"] is not None else None,
                representative_trace_id=r["rep"],
                pattern=pattern,  # type: ignore[arg-type]
            )
        )
    return out


def failure_prefix(traces_signatures: list[str]) -> list[tuple[str, str]]:
    """Longest common prefix of (name, status) across a list of signatures."""
    if not traces_signatures:
        return []
    split = [sig.split("|") for sig in traces_signatures]
    prefix: list[str] = []
    for parts in zip(*split, strict=False):
        first = parts[0]
        if all(p == first for p in parts):
            prefix.append(first)
        else:
            break
    return [tuple(p.split(":", 1)) for p in prefix]  # type: ignore[misc]


def failure_summary(limit_clusters: int = 20) -> dict:
    """Mine the database for failure patterns across all clusters.

    Returns:
      - overall_failure_rate
      - clusters: ranked by failure rate, then by count
      - common_failure_prefix: prefix shared by all failing traces (across clusters)
      - failing_node_counts: which span (name, status) appears most in failed traces
    """
    with storage.connect() as conn:
        clusters_rows = conn.execute(
            """SELECT signature,
                      COUNT(*) AS n,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs
               FROM traces
               WHERE signature IS NOT NULL
               GROUP BY signature"""
        ).fetchall()
        failed_sigs = [
            r["signature"]
            for r in conn.execute(
                "SELECT signature FROM traces WHERE status='error' AND signature IS NOT NULL"
            ).fetchall()
        ]
        traces_total = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        traces_failed = conn.execute(
            "SELECT COUNT(*) FROM traces WHERE status='error'"
        ).fetchone()[0]

    common_prefix = failure_prefix(failed_sigs)

    failing_node_counts: dict[tuple[str, str], int] = {}
    for sig in failed_sigs:
        for part in sig.split("|"):
            name, status = part.split(":", 1)
            failing_node_counts[(name, status)] = failing_node_counts.get((name, status), 0) + 1
    failing_nodes = sorted(failing_node_counts.items(), key=lambda kv: -kv[1])

    ranked_clusters = sorted(
        [
            {
                "signature": r["signature"],
                "sig_hash": signature_hash(r["signature"]),
                "count": r["n"],
                "errors": r["errs"] or 0,
                "error_rate": (r["errs"] or 0) / r["n"] if r["n"] else 0.0,
            }
            for r in clusters_rows
        ],
        key=lambda c: (-float(c["error_rate"]), -int(c["count"])),
    )[:limit_clusters]

    return {
        "traces_total": traces_total,
        "traces_failed": traces_failed,
        "overall_failure_rate": (traces_failed / traces_total) if traces_total else 0.0,
        "n_clusters": len(clusters_rows),
        "clusters": ranked_clusters,
        "common_failure_prefix": [{"name": n, "status": s} for n, s in common_prefix],
        "top_failing_nodes": [
            {"name": n, "status": s, "count": c} for (n, s), c in failing_nodes[:15]
        ],
    }
