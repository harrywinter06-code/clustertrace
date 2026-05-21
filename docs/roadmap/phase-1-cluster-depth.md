# Phase 1 — Cluster depth

> **Self-contained brief.** A Claude Code session reading just this file should have everything needed to start building.

## What this is

Three additions to the clustering primitive that make it the actual best-in-class for failure-pattern diagnostics:

1. **Tree-edit-distance signature mode** — `mode='tree_edit'` clusters traces that differ by small reorderings or retries
2. **Drift detection** — compare cluster failure rates between two time windows; flag clusters that regressed
3. **Auto-repro from a cluster** — `clustertrace repro <cluster_hash>` generates a pytest fixture that captures a failing trace's inputs

Together these turn the cluster page from "interesting list" into "the place you go when something broke after your last deploy."

## Why it exists

The current clustering is `mode='ordered'` (exact-string + RLE) or `mode='set'` (sorted unique). Both fail on real-world variance: one extra retry creates a new cluster (ordered) or all order information is lost (set). Tree-edit-distance is the middle ground.

Drift detection answers the most common LLM debugging question: *"What changed?"* — currently the dashboard can show what's failing now, not what got worse.

Auto-repro closes the loop from "I see the failure" to "I can reproduce it locally."

## Pre-flight checks (run BEFORE writing code)

1. **Read the existing cluster module** — `src/clustertrace/cluster.py`. Understand the signature format, the `_decode_pattern` escaping, and how `list_clusters(mode=...)` already dispatches between ordered and set.
2. **Read the existing tests** — `tests/test_cluster.py` and the cluster-related tests in `tests/test_red_team.py`. The escape encoding for `|`, `:`, `%` matters.
3. **Verify pytest + ruff + pyright pass on `main`** before you start. Baseline: 105/105 tests.
4. **Confirm SQLite schema v3 is the current** — `_SCHEMA_VERSION = 3` in `src/clustertrace/storage.py`. If you need a v4 migration, append to `_MIGRATIONS` and bump.

## What ships (binary, all must pass)

### Tree-edit-distance mode

- [ ] `signature_for_spans(spans, mode='tree_edit')` returns a *canonical* signature for a trace; two traces whose step sequences have tree-edit-distance ≤ `threshold` map to the same canonical
- [ ] Default threshold is computed per-DB as max(2, 0.1 × median trace length); configurable via `clustertrace.cluster.set_tree_edit_threshold(N)`
- [ ] `list_clusters(mode='tree_edit')` walks all traces, computes pairwise edit distances against existing cluster canonicals, assigns each trace to the closest canonical within threshold, creates a new canonical otherwise
- [ ] Algorithm: Wagner-Fischer on the `(name, status)` token sequence; O(n × m) per pair. For N traces and K canonicals, O(N × K × m²) total — acceptable up to N ≤ 10,000 traces; document the perf wall in the docstring.
- [ ] The canonical's `signature` is the sequence of the FIRST trace that landed in that cluster (we don't recompute a "median" canonical)
- [ ] When a trace joins an existing cluster, it inherits the cluster's `sig_hash` for UI grouping but keeps its own raw signature
- [ ] Dashboard `/clusters?mode=tree_edit` works; the UI toggle gets a third option

### Drift detection

- [ ] New endpoint `GET /api/cluster-drift?window=24h&compare=24h` returns clusters with their failure-rate change between the two windows
- [ ] Output shape:
  ```json
  {
    "current_window": {"start": ts, "end": ts, "trace_count": N},
    "compare_window": {"start": ts, "end": ts, "trace_count": M},
    "drifts": [
      {"sig_hash": "...", "signature": "...", "current": {"n": 8, "errs": 6, "rate": 0.75},
       "previous": {"n": 12, "errs": 0, "rate": 0.0}, "delta": 0.75, "direction": "regressed"}
    ]
  }
  ```
- [ ] Sort by `abs(delta)` descending; only include clusters with `n >= 3` in current window (statistical floor)
- [ ] New dashboard page `/drift` renders this — same look-and-feel as `/clusters`, but each card shows before/after rates and a red/green arrow
- [ ] Time windows accept `24h`, `7d`, `30d` (reuse `maintenance.parse_duration`)

### Auto-repro

- [ ] New CLI `clustertrace repro <sig_hash_or_trace_id>` emits a pytest file to stdout
- [ ] The pytest file:
  - Imports the entrypoint from a `--entry mod:fn` argument (same as replay)
  - Has the captured args/kwargs inlined as Python literals (if they're JSON-serializable)
  - Calls the function, asserts it does NOT raise (or asserts that it DOES raise the expected error type, configurable via `--mode positive|negative`)
- [ ] If the trace's root input is `__truncated`, emit a comment explaining the args weren't captured and ask the user to fill them in
- [ ] `clustertrace repro --out tests/test_repro_<short_hash>.py` writes to a file
- [ ] If user passes a `sig_hash`, pick the most recent failing trace in that cluster as the seed

## Hard rules

- **No schema migration unless absolutely necessary.** Drift and tree-edit-distance both work on the existing `traces` and `spans` tables — no new columns. Repro needs no schema changes.
- **All three features must work against the bundled demo data** (`clustertrace demo`) — write tests that load the demo JSONL into a tmp DB and verify cluster counts, drift output, and repro generation.
- **Performance budget**: `mode='tree_edit'` on 1,000 traces must complete in ≤ 5 seconds. Benchmark and document in the docstring.

## Tech stack

- Python 3.11+ (existing)
- No new dependencies. Wagner-Fischer is ~30 lines of stdlib code; do not add `python-Levenshtein` or `rapidfuzz` unless benchmarks show the pure-Python version is unusably slow.
- Tests go in `tests/test_tree_edit.py`, `tests/test_drift.py`, `tests/test_repro.py`
- Dashboard pages go in `src/clustertrace/dashboard/templates/drift.html` (reuse `clusters.html` styling)

## Decision boundaries

**Decide and commit:**
- Internal cluster-canonical caching strategy (in-memory per-request vs. persisted)
- Exact wording of the dashboard drift UI labels
- Whether to expose `threshold` as a query param on `/api/clusters?mode=tree_edit` (recommend: yes, with default applied server-side)
- Repro file naming convention

**Stop and write `BLOCKED.md` if:**
- Tree-edit-distance algorithm is materially slower than 5s for 1k traces and no algorithmic improvement helps (would force adding `rapidfuzz` as a dep — flag for review)
- Drift detection produces nonsensical output for traces that span DST changes (timezone bug — flag for review)

## What does NOT ship in this phase

- A graph-isomorphism clustering mode (overkill)
- Storing the canonical signature in a new column (lazy compute is fine)
- Drift detection across schema migrations (only compare within the same schema version)
- Auto-repro that handles non-JSON-serializable arguments (out of scope — emit a comment instead)

## Time budget

10–14 hours wall clock. If you hit 20h, ship what works and document the rest in `STATUS.md`.

## Operational rules

- Commit every 30–60 minutes
- After each commit, append a one-line update to `STATUS.md`: timestamp + what you finished
- Bump version to `0.6.0` in `pyproject.toml` and `src/clustertrace/__init__.py` on first commit of this phase
- Tag final commit `v0.6.0`
- Final commit message lists every "must ship" checkbox with ✓ or ✗

## References

- Wagner-Fischer algorithm: https://en.wikipedia.org/wiki/Wagner%E2%80%93Fischer_algorithm
- Existing cluster module: [`src/clustertrace/cluster.py`](../../src/clustertrace/cluster.py)
- Existing dashboard pattern (clusters page): [`src/clustertrace/dashboard/templates/clusters.html`](../../src/clustertrace/dashboard/templates/clusters.html)
- ARCHITECTURE.md: explains the existing signature format

## Definition of done

```bash
pytest -q                              # green
ruff check .                           # clean
pyright src/clustertrace               # 0 errors, 0 warnings
clustertrace demo                      # opens dashboard
# Then in the dashboard:
#   /clusters?mode=tree_edit shows fewer, broader clusters
#   /drift shows at least one cluster with a non-zero delta on the demo data
# And from CLI:
clustertrace repro <some_sig_hash>     # emits a runnable pytest file
```
