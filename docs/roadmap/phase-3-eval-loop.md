# Phase 3 — Eval loop on top of clusters

> **Self-contained brief.** Depends on Phase 1 (cluster-canonical concept). Do not start until Phase 1 lands on `main`.

## What this is

Three additions that turn the clusters page into an eval workflow that runs at the right abstraction level — clusters, not individual traces:

1. **LLM-as-judge on cluster representatives** — sample N traces per cluster, run an evaluator on those, generalize the verdict to the whole cluster
2. **Cluster annotations** — mark a cluster as `expected-failure`, `wontfix`, `priority`, or with free-form notes; persistent across runs
3. **Cluster pass/fail assertions** — persist rules like "cluster X must maintain success rate ≥ 90% over the last 100 runs" and surface failures as exit-code-non-zero in a new `clustertrace check` CLI

## Why it exists

Existing eval tools (Phoenix, LangSmith, Braintrust) run evaluators on every trace. For an agent that runs 10,000 times a day, this is expensive and the results blur. clustertrace already partitions traces into ~30 patterns; running the evaluator on 30 representatives and inheriting the verdict to thousands of similar traces is **massively cheaper and structurally clearer**.

Cluster annotations close the "I know this failure is expected" gap that frustrates anyone using Phoenix/Langfuse at scale — you can't currently say "this cluster is fine, stop flagging it."

Pass/fail assertions on clusters turn clustertrace into a **CI integration**: your eval suite runs as a daily cron, fails the build if a cluster regressed.

## Pre-flight checks

1. **Confirm Phase 1 has landed.** `mode='tree_edit'` should work; `/drift` should exist; `clustertrace repro` should exist. If not, stop and run Phase 1 first.
2. **Read** `src/clustertrace/cluster.py` after Phase 1's changes — the cluster-canonical concept is what this phase builds on.
3. Verify `pytest -q` is green on `main`.

## What ships (binary, all must pass)

### LLM-as-judge on cluster representatives

- [ ] New module `src/clustertrace/judge.py` with:
  - `evaluate_cluster(sig_hash, evaluator, n_samples=3, judge_model="claude-haiku-4-5-20251001") -> JudgeVerdict`
  - `evaluate_all_clusters(evaluator, mode='ordered', ...) -> dict[sig_hash, JudgeVerdict]`
  - `JudgeVerdict` dataclass: `sig_hash`, `samples_evaluated`, `pass_count`, `fail_count`, `notes` (LLM-written summary), `representative_failures` (list of trace IDs)
- [ ] An `evaluator` is a Python callable `(trace: dict) -> {"pass": bool, "reason": str}`. Two built-ins provided:
  - `clustertrace.judge.no_exceptions_evaluator(trace)` — pass if no span in trace has `status='error'`
  - `clustertrace.judge.llm_judge_evaluator(rubric)` — returns a callable that asks the judge_model to evaluate the trace against the rubric (text)
- [ ] CLI: `clustertrace judge --rubric "did the agent answer the user's question?" --samples 3` → writes a markdown report to stdout
- [ ] Cost-cap built in: `--max-cost-usd 0.50` aborts if running evaluator on all clusters would exceed this
- [ ] Storage: a new `cluster_judgments` table created in v4 migration (sig_hash, evaluator_name, timestamp, pass_count, fail_count, notes_json)
- [ ] Dashboard: cluster cards on `/clusters` show the latest judgment if one exists

### Cluster annotations

- [ ] New API: `clustertrace.annotate_cluster(sig_hash, *, status=None, note=None, tag=None)` where:
  - `status` ∈ `{"expected-failure", "wontfix", "priority", "acceptable", None}` (None = clear)
  - `note` = free-form text up to 4096 chars
  - `tag` = string label, multiple allowed
- [ ] Storage: new table `cluster_annotations` (sig_hash, status, note, tags_json, updated_at) in v4 migration
- [ ] Dashboard `/clusters` shows annotation status as a badge on each cluster; clicking it opens an inline editor
- [ ] `POST /api/cluster-annotations` accepts `{sig_hash, status, note, tag}`
- [ ] `expected-failure` clusters are excluded from the "Failure summary" header counts on `/clusters` (with a "+N expected" footnote)
- [ ] Annotations survive `clustertrace vacuum` (CASCADE delete only when ALL traces in the cluster are deleted)

### Cluster pass/fail assertions

- [ ] New CLI: `clustertrace check` runs all persisted assertions and exits 0 if all pass, 1 if any fail
- [ ] New CLI: `clustertrace assert <sig_hash> --success-rate-above 0.9 --over-last 100` persists a rule
- [ ] Rules table in v4 migration: `cluster_assertions` (id, sig_hash, rule_json, created_at)
- [ ] `clustertrace check` output is human-readable AND has `--format json` for CI consumption
- [ ] Assertion types supported: `--success-rate-above X`, `--avg-latency-below MS`, `--avg-cost-below USD`, `--no-new-traces` (cluster must have no traces in last N)
- [ ] Documented intended use: run in CI as `clustertrace check --format json | jq` after every nightly eval

## Hard rules

- **Schema v4 migration required.** Append a single migration script to `_MIGRATIONS` in `src/clustertrace/storage.py` that adds `cluster_judgments`, `cluster_annotations`, `cluster_assertions` tables. Idempotent.
- **No LLM calls without explicit opt-in.** `clustertrace.judge.llm_judge_evaluator` requires `ANTHROPIC_API_KEY`; without it, fail with a clear message. The built-in `no_exceptions_evaluator` is the default and is free.
- **Judgments are advisory, never deletion-triggering.** A `wontfix` cluster is hidden from headlines but its data stays in the DB.

## Tech stack

- Python 3.11+ (existing)
- `anthropic` SDK (already an optional extra)
- Tests: `tests/test_judge.py`, `tests/test_annotations.py`, `tests/test_check_cli.py`

## Decision boundaries

**Decide and commit:**
- The exact assertion syntax (`--success-rate-above 0.9` vs `--ge success_rate 0.9` vs something fancier)
- Whether `--over-last N` is in traces or in time units
- Default judge model (recommend `claude-haiku-4-5` for cost)

**Stop and write `BLOCKED.md` if:**
- The LLM-as-judge evaluator's accuracy on the bundled demo data is below 70% (would need rubric tuning or model change — flag for review)
- Cost-cap doesn't actually prevent runaway spend in your testing — flag, don't ship

## What does NOT ship in this phase

- A general-purpose "datasets" feature like Phoenix/Braintrust have. clustertrace's "dataset" IS the set of clusters. We don't ship a separate primitive.
- Real-time eval (judge runs on demand or scheduled; not on every trace as it arrives)
- A UI for editing assertions (CLI-only in v0.8; UI is v0.9)
- Multi-judge ensembles
- Auto-rubric-generation

## Time budget

12–18 hours wall clock.

## Operational rules

- Commit per ship-checkbox (each of the three subsystems is a separate commit minimum)
- Append to `STATUS.md` per commit
- Bump version to `0.8.0`
- Tag `v0.8.0`

## References

- Existing cluster module: [`src/clustertrace/cluster.py`](../../src/clustertrace/cluster.py)
- Existing migration pattern: [`src/clustertrace/storage.py`](../../src/clustertrace/storage.py)
- Cost module (for LLM judge cost capping): [`src/clustertrace/cost.py`](../../src/clustertrace/cost.py)

## Definition of done

```bash
clustertrace judge --rubric "did it answer correctly?" --samples 2 --max-cost-usd 0.10
# > Cluster A: 2/2 pass.  Cluster B: 0/2 pass — reason: ...
clustertrace annotate <sig_hash> --status expected-failure --note "rate limit known"
clustertrace assert <sig_hash> --success-rate-above 0.9 --over-last 100
clustertrace check                          # exit 0 if all assertions pass
clustertrace check --format json | jq '.'   # machine-readable for CI
pytest -q                                    # green
```
