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


def signature_for_spans(spans: Iterable[dict], mode: str = "ordered") -> str:
    """Compute a stable signature for a trace from its child spans.

    Input rows must have at least: name, status, parent_id, started_at.
    The root span (parent_id IS NULL) is excluded.

    Modes:
      "ordered" (default) — consecutive identical (normalized_name, status)
        pairs are collapsed into one step. Order is preserved. Two traces
        with the same step sequence cluster together; reorderings split.
      "set" — sorted unique set of (normalized_name, status) pairs.
        Reorderings, retries, and different loop orderings all cluster
        together. Use this when your agent's branching produces too many
        ordered clusters to be useful.
    """
    rows = sorted(
        (s for s in spans if s.get("parent_id") is not None),
        key=lambda s: s["started_at"],
    )
    if not rows:
        return "empty"
    if mode == "set":
        tokens = sorted({f"{_normalize_name(s['name'])}:{s['status']}" for s in rows})
        return "|".join(tokens)
    # default: ordered + RLE-collapsed
    parts: list[str] = []
    last: str | None = None
    for s in rows:
        token = f"{_normalize_name(s['name'])}:{s['status']}"
        if token != last:
            parts.append(token)
            last = token
    return "|".join(parts)


def signature_hash(signature: str) -> str:
    """Short stable hash for use in URLs/UI without showing the full sig."""
    return hashlib.sha1(signature.encode()).hexdigest()[:10]


def compute_and_store_signature(trace_id: str, mode: str = "ordered") -> str | None:
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
    sig = signature_for_spans([dict(r) for r in rows], mode=mode)
    storage.set_trace_signature(trace_id, sig)
    return sig


def backfill_signatures(mode: str = "ordered") -> int:
    """Compute signatures for any traces missing one. Returns number filled."""
    with storage.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM traces WHERE signature IS NULL AND status != 'running'"
        ).fetchall()]
    n = 0
    for tid in ids:
        if compute_and_store_signature(tid, mode=mode) is not None:
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


def list_clusters(limit: int = 50, offset: int = 0, mode: str = "ordered") -> list[Cluster]:
    """Return all distinct trace signatures with counts, sorted by frequency.

    mode='ordered' uses the stored signature column (cheap, indexed).
    mode='set' re-clusters on the fly from spans (Python-side, slower) so the
    user can compare ordered vs reorder-insensitive groupings without a
    second column.
    """
    if mode == "ordered":
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
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
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

    # mode='set' — recompute on the fly
    return _list_clusters_set_mode(limit=limit, offset=offset)


def _list_clusters_set_mode(limit: int = 50, offset: int = 0) -> list[Cluster]:
    """On-the-fly recompute using mode='set'. O(n_spans) in Python."""
    from collections import defaultdict

    groups: dict[str, dict] = defaultdict(
        lambda: {"trace_ids": [], "errors": 0, "durations": [], "last_started": 0.0}
    )
    with storage.connect() as conn:
        traces = conn.execute(
            "SELECT id, status, started_at, ended_at FROM traces WHERE status != 'running'"
        ).fetchall()
        trace_lookup = {t["id"]: t for t in traces}
        spans = conn.execute(
            "SELECT trace_id, name, status, parent_id, started_at FROM spans"
        ).fetchall()
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(dict(s))
    for tid, t in trace_lookup.items():
        sig = signature_for_spans(by_trace.get(tid, []), mode="set")
        g = groups[sig]
        g["trace_ids"].append(tid)
        if t["status"] == "error":
            g["errors"] += 1
        if t["ended_at"]:
            g["durations"].append((t["ended_at"] - t["started_at"]) * 1000)
        if t["started_at"] > g["last_started"]:
            g["last_started"] = t["started_at"]
            g["rep"] = tid
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]["trace_ids"]))[offset:offset + limit]
    out: list[Cluster] = []
    for sig, g in ranked:
        pattern = [tuple(part.split(":", 1)) for part in sig.split("|")] if sig != "empty" else []
        n = len(g["trace_ids"])
        avg = sum(g["durations"]) / len(g["durations"]) if g["durations"] else None
        out.append(
            Cluster(
                signature=sig,
                sig_hash=signature_hash(sig),
                count=n,
                error_count=g["errors"],
                avg_duration_ms=round(avg, 1) if avg is not None else None,
                representative_trace_id=g.get("rep", g["trace_ids"][0]),
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


def failure_summary(limit_clusters: int = 20, group_by_tag: str | None = "agent") -> dict:
    """Mine the database for failure patterns across all clusters.

    Returns:
      - overall_failure_rate
      - clusters: ranked by failure rate, then by count
      - common_failure_prefix: prefix shared by all failing traces (across clusters)
      - failing_node_counts: which span (name, status) appears most in failed traces
      - prefixes_by_tag: per-value failure prefix when `group_by_tag` is set.
        The default global prefix is empty when failures come from different
        agents/topologies; grouping by `agent` (or another tag) makes it meaningful.
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
        prefixes_by_tag: dict[str, list[dict]] = {}
        if group_by_tag:
            tag_rows = conn.execute(
                """SELECT tt.value AS v, t.signature AS sig
                   FROM traces t JOIN trace_tags tt ON tt.trace_id = t.id AND tt.key = ?
                   WHERE t.status = 'error' AND t.signature IS NOT NULL""",
                (group_by_tag,),
            ).fetchall()
            from collections import defaultdict

            grouped: dict[str, list[str]] = defaultdict(list)
            for r in tag_rows:
                grouped[r["v"]].append(r["sig"])
            for v, sigs in grouped.items():
                prefix = failure_prefix(sigs)
                if prefix:
                    prefixes_by_tag[v] = [{"name": n, "status": s} for n, s in prefix]

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
        "prefixes_by_tag": prefixes_by_tag,
        "group_by_tag": group_by_tag,
        "top_failing_nodes": [
            {"name": n, "status": s, "count": c} for (n, s), c in failing_nodes[:15]
        ],
    }
