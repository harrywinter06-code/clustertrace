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

from clustertrace import storage

_LLM_PREFIXES = ("anthropic.messages.create:", "openai.chat.completions.create:")


def _normalize_name(name: str) -> str:
    for prefix in _LLM_PREFIXES:
        if name.startswith(prefix):
            return prefix.rstrip(":")
    return name


# The signature format is `name:status|name:status|...`. If a span name or
# status contains `|` or `:`, the round-trip (encode then split) loses
# structure. Encode them as %7C and %3A on the way in; decode on the way out.
def _escape(s: str) -> str:
    return s.replace("%", "%25").replace("|", "%7C").replace(":", "%3A")


def _unescape(s: str) -> str:
    return s.replace("%3A", ":").replace("%7C", "|").replace("%25", "%")


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
      "tree_edit" — returns the *raw* token sequence (no RLE collapse) so
        Wagner-Fischer edit distance can compare two traces token-by-token.
        `list_clusters(mode='tree_edit')` then groups traces whose edit
        distance is within a small threshold; one extra retry or a single
        reordering no longer splits a cluster. The signature returned here
        is the per-trace token string — *not* a canonical group key.
    """
    rows = sorted(
        (s for s in spans if s.get("parent_id") is not None),
        key=lambda s: s["started_at"],
    )
    if not rows:
        return "empty"
    if mode == "set":
        tokens = sorted({f"{_escape(_normalize_name(s['name']))}:{_escape(s['status'])}" for s in rows})
        return "|".join(tokens)
    if mode == "tree_edit":
        # Full token sequence, no RLE collapse — we want to see retries.
        tokens = [
            f"{_escape(_normalize_name(s['name']))}:{_escape(s['status'])}" for s in rows
        ]
        return "|".join(tokens)
    # default: ordered + RLE-collapsed
    parts: list[str] = []
    last: str | None = None
    for s in rows:
        token = f"{_escape(_normalize_name(s['name']))}:{_escape(s['status'])}"
        if token != last:
            parts.append(token)
            last = token
    return "|".join(parts)


def _decode_pattern(sig: str) -> list[tuple[str, str]]:
    """Reverse the encoding used by signature_for_spans. Robust to | and : in names."""
    if not sig or sig == "empty":
        return []
    out: list[tuple[str, str]] = []
    for part in sig.split("|"):
        if ":" not in part:
            # Defensive: legacy signatures stored before escaping, or oddly-shaped data
            out.append((_unescape(part), ""))
            continue
        name, status = part.rsplit(":", 1)
        out.append((_unescape(name), _unescape(status)))
    return out


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


def list_clusters(
    limit: int = 50,
    offset: int = 0,
    mode: str = "ordered",
    threshold: int | None = None,
) -> list[Cluster]:
    """Return all distinct trace signatures with counts, sorted by frequency.

    mode='ordered' uses the stored signature column (cheap, indexed).
    mode='set' re-clusters on the fly from spans (Python-side, slower) so the
    user can compare ordered vs reorder-insensitive groupings without a
    second column.

    `threshold` is honored only for mode='tree_edit'. Pass it directly to
    avoid the module-level override — otherwise concurrent callers racing on
    `_tree_edit_threshold_override` would see each other's values. The override
    remains as a convenience for non-API callers (notebooks, scripts).
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
            pattern = _decode_pattern(sig)
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

    if mode == "tree_edit":
        # Explicit kwarg wins; otherwise fall back to the module-level override
        # (None means "use the per-DB default").
        effective = threshold if threshold is not None else _tree_edit_threshold_override
        return _list_clusters_tree_edit_mode(
            limit=limit, offset=offset, threshold=effective
        )

    # mode='set' — recompute on the fly
    return _list_clusters_set_mode(limit=limit, offset=offset)


# --- Tree-edit-distance clustering -----------------------------------------
#
# Wagner-Fischer (1974) computes the Levenshtein distance between two token
# sequences in O(n × m) time and O(min(n, m)) space (we use full matrix here
# for clarity — sequences are short, the matrix fits in cache).
#
# Performance wall: for N traces against K canonicals with token sequences of
# length m, this is O(N × K × m²) total. On the demo data (60 traces, m ≈ 20)
# it finishes in milliseconds. The brief calls for ≤5s on 1,000 traces; we
# benchmark this in tests/test_tree_edit.py — the pure-Python implementation
# is well inside the budget. If you push to N ≥ 10,000, switch to a banded
# Levenshtein (early-exit once min row value > threshold) or add `rapidfuzz`.

_tree_edit_threshold_override: int | None = None


def set_tree_edit_threshold(n: int | None) -> None:
    """Override the per-cluster edit-distance threshold for `mode='tree_edit'`.

    Pass `None` to restore the default (computed per-DB as max(2, 0.1 × median
    trace length) at clustering time).
    """
    global _tree_edit_threshold_override
    if n is not None and n < 0:
        raise ValueError("threshold must be >= 0")
    _tree_edit_threshold_override = n


def _wagner_fischer(a: list[str], b: list[str], max_distance: int | None = None) -> int:
    """Return the Levenshtein edit distance between two token sequences.

    `max_distance` enables an early exit: if every cell in a row exceeds the
    bound, we know the final distance must also exceed it and can return
    `max_distance + 1` immediately. This keeps the hot path (rejecting a
    candidate canonical because the trace is obviously not close enough)
    proportional to `max_distance × min(n, m)` rather than `n × m`.
    """
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    # Length-difference is a lower bound on edit distance — short-circuit when
    # the bound is already over the threshold.
    if max_distance is not None and abs(n - m) > max_distance:
        return max_distance + 1
    # Use rolling rows to keep memory linear.
    prev = list(range(m + 1))
    curr = [0] * (m + 1)
    for i in range(1, n + 1):
        curr[0] = i
        row_min = curr[0]
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ai == b[j - 1] else 1
            curr[j] = min(
                curr[j - 1] + 1,      # insert
                prev[j] + 1,          # delete
                prev[j - 1] + cost,   # substitute
            )
            if curr[j] < row_min:
                row_min = curr[j]
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        prev, curr = curr, prev
    return prev[m]


def _tokenize_signature(sig: str) -> list[str]:
    """Split a stored signature into its token list. Robust to escaped `|`."""
    if not sig or sig == "empty":
        return []
    return sig.split("|")


def _default_tree_edit_threshold(token_lengths: list[int]) -> int:
    """max(2, 0.1 × median token-sequence length). Brief-specified."""
    if not token_lengths:
        return 2
    s = sorted(token_lengths)
    mid = len(s) // 2
    median = s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2
    return max(2, int(0.1 * median))


def _list_clusters_tree_edit_mode(
    limit: int = 50, offset: int = 0, threshold: int | None = None
) -> list[Cluster]:
    """On-the-fly cluster assignment via Wagner-Fischer edit distance.

    Algorithm (greedy, single-pass):
      1. Compute the raw token signature for every trace.
      2. Process traces in `started_at` order. The first trace seeds canonical 0.
      3. For each subsequent trace, find the closest existing canonical (lowest
         edit distance). If `distance <= threshold`, the trace joins that
         cluster and inherits its sig_hash. Otherwise it seeds a new canonical.

    The canonical is the *first* trace that landed in the cluster — we don't
    recompute a "median" canonical. This makes the result stable: adding a new
    trace never reshuffles existing clusters.
    """
    from collections import defaultdict

    with storage.connect() as conn:
        traces = conn.execute(
            """SELECT id, status, started_at, ended_at
               FROM traces
               WHERE status != 'running'
               ORDER BY started_at ASC"""
        ).fetchall()
        spans = conn.execute(
            "SELECT trace_id, name, status, parent_id, started_at FROM spans"
        ).fetchall()
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(dict(s))

    # Compute signatures up front so we can derive a default threshold.
    trace_tokens: dict[str, list[str]] = {}
    raw_signatures: dict[str, str] = {}
    for t in traces:
        sig = signature_for_spans(by_trace.get(t["id"], []), mode="tree_edit")
        raw_signatures[t["id"]] = sig
        trace_tokens[t["id"]] = _tokenize_signature(sig)

    if threshold is None:
        threshold = _default_tree_edit_threshold(
            [len(toks) for toks in trace_tokens.values()]
        )

    # canonicals[i] = (tokens, signature, sig_hash, group_state)
    canonicals: list[tuple[list[str], str, str]] = []
    groups: list[dict] = []

    for t in traces:
        tid = t["id"]
        tokens = trace_tokens[tid]
        assigned: int | None = None
        best_distance = threshold + 1
        for idx, (canon_tokens, _csig, _chash) in enumerate(canonicals):
            d = _wagner_fischer(tokens, canon_tokens, max_distance=best_distance)
            if d <= threshold and d < best_distance:
                best_distance = d
                assigned = idx
                if d == 0:
                    break  # exact match — can't do better
        if assigned is None:
            sig = raw_signatures[tid]
            sig_hash = signature_hash(sig)
            canonicals.append((tokens, sig, sig_hash))
            groups.append(
                {"trace_ids": [], "errors": 0, "durations": [], "last_started": 0.0, "rep": tid}
            )
            assigned = len(canonicals) - 1
        g = groups[assigned]
        g["trace_ids"].append(tid)
        if t["status"] == "error":
            g["errors"] += 1
        if t["ended_at"]:
            g["durations"].append((t["ended_at"] - t["started_at"]) * 1000)
        if t["started_at"] > g["last_started"]:
            g["last_started"] = t["started_at"]
            g["rep"] = tid

    ranked = sorted(
        zip(canonicals, groups, strict=False),
        key=lambda cg: -len(cg[1]["trace_ids"]),
    )[offset : offset + limit]
    out: list[Cluster] = []
    for (_tokens, sig, sig_hash), g in ranked:
        n = len(g["trace_ids"])
        avg = sum(g["durations"]) / len(g["durations"]) if g["durations"] else None
        pattern = _decode_pattern(sig)
        out.append(
            Cluster(
                signature=sig,
                sig_hash=sig_hash,
                count=n,
                error_count=g["errors"],
                avg_duration_ms=round(avg, 1) if avg is not None else None,
                representative_trace_id=g["rep"],
                pattern=pattern,
            )
        )
    return out


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
        pattern = _decode_pattern(sig)
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
    return _decode_pattern("|".join(prefix))


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
        for name, status in _decode_pattern(sig):
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
