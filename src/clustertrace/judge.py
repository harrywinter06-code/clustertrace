"""LLM-as-judge on cluster representatives.

The point of this module: instead of running an evaluator on every trace
(Phoenix/LangSmith/Braintrust default), sample N traces per cluster, judge
those, and inherit the verdict to the whole cluster. clustertrace already
groups thousands of traces into ~30 patterns — judging the patterns is
massively cheaper and the right level of abstraction for an agent that
runs 10,000 times a day.

Public surface:
  - `JudgeVerdict` — dataclass returned by every evaluator run
  - `evaluate_cluster(sig_hash, evaluator, n_samples=3, ...)` — single cluster
  - `evaluate_all_clusters(evaluator, ...)` — every cluster currently in the DB
  - `no_exceptions_evaluator(trace)` — free, deterministic, default
  - `llm_judge_evaluator(rubric, model=...)` — paid, requires ANTHROPIC_API_KEY

An "evaluator" is `(trace: dict) -> {"pass": bool, "reason": str}`. trace is
the same shape `clustertrace.dashboard.app:api_trace` returns:
`{"trace": <row>, "spans": [...], "tags": {...}}`.

Storage: every run writes one row to `cluster_judgments` with sig_hash,
evaluator_name, pass_count, fail_count, and a notes_json blob summarizing
the failures (or LLM-written notes when the rubric-judge is used).
"""
from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from clustertrace import cluster, storage

# --- Evaluator protocol ---------------------------------------------------

Trace = dict[str, Any]
EvaluatorResult = dict[str, Any]  # {"pass": bool, "reason": str}
Evaluator = Callable[[Trace], EvaluatorResult]


@dataclass
class JudgeVerdict:
    """Result of running an evaluator on a cluster's sampled traces.

    `pass_count + fail_count` equals `samples_evaluated`. `notes` is a
    short, human-readable summary; for `llm_judge_evaluator` it includes the
    LLM's per-trace reason strings.
    """

    sig_hash: str
    samples_evaluated: int
    pass_count: int
    fail_count: int
    notes: str
    representative_failures: list[str] = field(default_factory=list)
    evaluator_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Built-in: no-exceptions ----------------------------------------------


def no_exceptions_evaluator(trace: Trace) -> EvaluatorResult:
    """Pass if no span in the trace has status='error'. Free, deterministic."""
    spans = trace.get("spans") or []
    for s in spans:
        if s.get("status") == "error":
            name = s.get("name") or "<unnamed>"
            err = s.get("error_message") or s.get("error_type") or ""
            return {
                "pass": False,
                "reason": f"span {name!r} errored: {err}".strip().rstrip(":"),
            }
    # Also check root-trace level — some importers only set trace.status
    t = trace.get("trace") or {}
    if t.get("status") == "error":
        return {
            "pass": False,
            "reason": f"trace status=error: {t.get('error_message') or t.get('error_type') or ''}".strip().rstrip(":"),
        }
    return {"pass": True, "reason": "no span errored"}


no_exceptions_evaluator.__name__ = "no_exceptions"


# --- Built-in: LLM judge --------------------------------------------------


_DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"


def llm_judge_evaluator(
    rubric: str,
    model: str = _DEFAULT_JUDGE_MODEL,
    *,
    max_tokens: int = 256,
) -> Evaluator:
    """Return an evaluator that asks `model` to judge each trace against `rubric`.

    `rubric` is a plain-English question, e.g. "did the agent answer the
    user's question?". The judge model returns JSON `{"pass": bool, "reason": str}`.

    Requires `ANTHROPIC_API_KEY` to be set. The check is deferred until the
    evaluator is *called*, so importing this module without credentials is
    safe — matches the rest of clustertrace's optional-dep behaviour.
    """
    rubric_text = (rubric or "").strip()
    if not rubric_text:
        raise ValueError("rubric must be a non-empty string")

    def _judge(trace: Trace) -> EvaluatorResult:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set — llm_judge_evaluator needs an API key. "
                "Use no_exceptions_evaluator for a free default."
            )
        try:
            import anthropic  # type: ignore[import-untyped]
        except ImportError as e:
            raise RuntimeError(
                "the `anthropic` package is required for llm_judge_evaluator. "
                "Install it with `pip install clustertrace[anthropic]`."
            ) from e
        client = anthropic.Anthropic(api_key=api_key)
        prompt = _build_judge_prompt(trace, rubric_text)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text  # type: ignore[union-attr]
            for block in msg.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return _parse_judge_response(text)

    _judge.__name__ = f"llm_judge[{model}]"  # type: ignore[attr-defined]
    _judge.rubric = rubric_text  # type: ignore[attr-defined]
    _judge.model = model  # type: ignore[attr-defined]
    return _judge


def _build_judge_prompt(trace: Trace, rubric: str) -> str:
    """Render the trace into a compact prompt the judge model can score.

    We summarize rather than dump raw JSON: trace name + tags + ordered
    span list with (name, status, key inputs/outputs). Keeps tokens low so
    a sample of 3 stays well under the cost cap.
    """
    import json

    t = trace.get("trace") or {}
    tags = trace.get("tags") or {}
    spans = trace.get("spans") or []
    lines: list[str] = []
    lines.append(f"# Trace: {t.get('name', '<unnamed>')}")
    lines.append(f"Status: {t.get('status', 'unknown')}")
    if tags:
        lines.append(f"Tags: {json.dumps(tags, ensure_ascii=False)}")
    lines.append("")
    lines.append("## Spans (in order)")
    for s in spans:
        name = s.get("name") or "<unnamed>"
        status = s.get("status") or "unknown"
        line = f"- [{status}] {name}"
        # Inline minimal input/output snippets so the judge can score content.
        inp = _short_repr(s.get("input"))
        outp = _short_repr(s.get("output"))
        if inp:
            line += f"\n  input: {inp}"
        if outp:
            line += f"\n  output: {outp}"
        err = s.get("error_message")
        if err:
            line += f"\n  error: {str(err)[:200]}"
        lines.append(line)
    rendered = "\n".join(lines)
    return (
        "You are evaluating an agent's execution trace against a rubric.\n"
        "Respond ONLY with a single JSON object: "
        '{"pass": true|false, "reason": "<one-sentence reason>"}.\n'
        "No prose outside the JSON.\n\n"
        f"# Rubric\n{rubric}\n\n"
        f"{rendered}\n"
    )


def _short_repr(v: Any, limit: int = 240) -> str:
    """Render a value for the judge prompt, capped at `limit` chars."""
    if v is None:
        return ""
    import json

    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(v)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def _parse_judge_response(text: str) -> EvaluatorResult:
    """Pull `{"pass": bool, "reason": str}` out of the judge's reply.

    Tolerates extra prose around the JSON (some models add a preamble). If
    we can't find a JSON object we treat the response as a soft fail with
    the raw text as the reason — surfaces a model glitch without crashing
    the whole evaluation.
    """
    import json
    import re

    candidate = text
    # Strip code fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1)
    # Try to locate the first {...} block.
    obj_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not obj_match:
        return {"pass": False, "reason": f"judge gave no JSON: {text[:200]}"}
    try:
        parsed = json.loads(obj_match.group(0))
    except (ValueError, TypeError):
        return {"pass": False, "reason": f"judge JSON unparseable: {text[:200]}"}
    if not isinstance(parsed, dict):
        return {"pass": False, "reason": f"judge JSON not an object: {text[:200]}"}
    raw_pass = parsed.get("pass")
    if isinstance(raw_pass, bool):
        passed = raw_pass
    elif isinstance(raw_pass, str):
        passed = raw_pass.strip().lower() in {"true", "yes", "pass", "1"}
    else:
        passed = False
    reason = str(parsed.get("reason") or parsed.get("explanation") or "").strip()
    if not reason:
        reason = "(no reason given)"
    return {"pass": passed, "reason": reason}


# --- Sampling -------------------------------------------------------------


def _sample_trace_ids_for_cluster(
    sig_hash: str,
    n_samples: int,
    *,
    mode: str = "ordered",
    rng: random.Random | None = None,
) -> list[str]:
    """Pick up to `n_samples` traces from the cluster identified by sig_hash.

    For mode='ordered' (the common case) we filter by the stored signature
    column directly. For mode='tree_edit' we list the cluster and intersect
    with its representative — keeps the sampling simple and stable.
    """
    if mode in ("set", "tree_edit"):
        clusters = cluster.list_clusters(mode=mode, limit=1000)
        match = next((c for c in clusters if c.sig_hash == sig_hash), None)
        if match is None:
            return []
        signature = match.signature
    else:
        signature = None

    with storage.connect() as conn:
        if signature is None:
            # All matching signatures hash to the requested value. Computing
            # the hash for every distinct signature is cheap.
            sig_rows = conn.execute(
                """SELECT DISTINCT signature FROM traces
                   WHERE signature IS NOT NULL AND status != 'running'"""
            ).fetchall()
            matching = [
                r["signature"] for r in sig_rows
                if cluster.signature_hash(r["signature"]) == sig_hash
            ]
            if not matching:
                return []
            placeholders = ",".join("?" for _ in matching)
            trace_rows = conn.execute(
                f"""SELECT id FROM traces
                    WHERE signature IN ({placeholders})
                      AND status != 'running'
                    ORDER BY started_at DESC""",
                matching,
            ).fetchall()
        else:
            trace_rows = conn.execute(
                """SELECT id FROM traces
                   WHERE signature = ? AND status != 'running'
                   ORDER BY started_at DESC""",
                (signature,),
            ).fetchall()
    ids = [r["id"] for r in trace_rows]
    if not ids:
        return []
    if len(ids) <= n_samples:
        return ids
    r = rng if rng is not None else random.Random(sig_hash)  # deterministic per cluster
    return r.sample(ids, n_samples)


def _load_trace(trace_id: str) -> Trace | None:
    """Hydrate a trace into the dict shape evaluators expect."""
    import json

    with storage.connect() as conn:
        t = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
        if t is None:
            return None
        spans = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at ASC",
            (trace_id,),
        ).fetchall()
        tags = conn.execute(
            "SELECT key, value FROM trace_tags WHERE trace_id = ?", (trace_id,)
        ).fetchall()

    def _maybe_json(value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value

    spans_out: list[dict[str, Any]] = []
    for s in spans:
        d = {k: s[k] for k in s.keys()}
        d["input"] = _maybe_json(d.pop("input_json", None))
        d["output"] = _maybe_json(d.pop("output_json", None))
        d["attrs"] = _maybe_json(d.pop("attrs_json", None))
        spans_out.append(d)
    return {
        "trace": {k: t[k] for k in t.keys()},
        "spans": spans_out,
        "tags": {r["key"]: r["value"] for r in tags},
    }


# --- Cost estimation -------------------------------------------------------


# Conservative defaults for the LLM-judge cost-cap. The cost-cap is meant as
# a hard guardrail, not a precise prediction — we err on the side of
# overestimating so the budget triggers before runaway spend, not after.
_LLM_JUDGE_AVG_INPUT_TOKENS = 1500
_LLM_JUDGE_AVG_OUTPUT_TOKENS = 80


def _estimate_judge_cost_usd(
    evaluator: Evaluator,
    n_clusters: int,
    n_samples: int,
) -> float:
    """Cost in USD if we run `evaluator` on `n_clusters * n_samples` traces.

    Returns 0 for non-LLM evaluators (no API calls). For the LLM judge we
    multiply expected token counts by the cluster's price-per-token from
    `clustertrace.cost`.
    """
    if getattr(evaluator, "model", None) is None:
        return 0.0
    from clustertrace import cost as _cost

    price = _cost._match_model(str(getattr(evaluator, "model", "")))
    if price is None:
        # Unknown pricing — assume the worst Anthropic model so we still cap.
        price = (15.0, 75.0)
    per_call = (
        (_LLM_JUDGE_AVG_INPUT_TOKENS / 1_000_000.0) * price[0]
        + (_LLM_JUDGE_AVG_OUTPUT_TOKENS / 1_000_000.0) * price[1]
    )
    return per_call * n_clusters * n_samples


class JudgeCostCapExceeded(RuntimeError):
    """Raised before any LLM calls when the predicted cost exceeds the cap."""


# --- Public API -----------------------------------------------------------


def evaluate_cluster(
    sig_hash: str,
    evaluator: Evaluator,
    n_samples: int = 3,
    *,
    mode: str = "ordered",
    judge_model: str | None = None,  # noqa: ARG001 — kept for API symmetry with brief
    persist: bool = True,
    rng: random.Random | None = None,
) -> JudgeVerdict:
    """Run `evaluator` on up to `n_samples` traces from the named cluster.

    Returns a JudgeVerdict. If `persist=True` (default) the verdict is
    written to `cluster_judgments` so the dashboard can surface it.
    """
    if n_samples < 1:
        raise ValueError("n_samples must be >= 1")
    ids = _sample_trace_ids_for_cluster(sig_hash, n_samples, mode=mode, rng=rng)
    evaluator_name = getattr(evaluator, "__name__", "evaluator")
    if not ids:
        verdict = JudgeVerdict(
            sig_hash=sig_hash,
            samples_evaluated=0,
            pass_count=0,
            fail_count=0,
            notes="no traces available in cluster",
            representative_failures=[],
            evaluator_name=evaluator_name,
        )
        if persist:
            _persist_verdict(verdict)
        return verdict

    pass_count = 0
    fail_count = 0
    failures: list[str] = []
    fail_reasons: list[str] = []
    for tid in ids:
        trace = _load_trace(tid)
        if trace is None:
            fail_count += 1
            failures.append(tid)
            fail_reasons.append(f"{tid}: trace vanished mid-evaluation")
            continue
        try:
            result = evaluator(trace)
        except Exception as e:
            # An evaluator that itself crashes counts as a fail with the
            # exception text as the reason — never silently swallowed.
            fail_count += 1
            failures.append(tid)
            fail_reasons.append(f"{tid}: evaluator raised {type(e).__name__}: {e}")
            continue
        if not isinstance(result, dict) or "pass" not in result:
            fail_count += 1
            failures.append(tid)
            fail_reasons.append(
                f"{tid}: evaluator returned non-conforming result: {result!r}"
            )
            continue
        if bool(result["pass"]):
            pass_count += 1
        else:
            fail_count += 1
            failures.append(tid)
            reason = str(result.get("reason") or "").strip()
            fail_reasons.append(f"{tid}: {reason}" if reason else f"{tid}: (no reason)")

    notes = _summarize_notes(pass_count, fail_count, fail_reasons)
    verdict = JudgeVerdict(
        sig_hash=sig_hash,
        samples_evaluated=len(ids),
        pass_count=pass_count,
        fail_count=fail_count,
        notes=notes,
        representative_failures=failures,
        evaluator_name=evaluator_name,
    )
    if persist:
        _persist_verdict(verdict)
    return verdict


def _summarize_notes(pass_count: int, fail_count: int, fail_reasons: list[str]) -> str:
    if fail_count == 0:
        return f"{pass_count}/{pass_count + fail_count} pass"
    head = f"{pass_count}/{pass_count + fail_count} pass — failure reasons:"
    bullets = "\n".join(f"  - {r}" for r in fail_reasons[:5])
    if len(fail_reasons) > 5:
        bullets += f"\n  - …and {len(fail_reasons) - 5} more"
    return head + "\n" + bullets


def _persist_verdict(verdict: JudgeVerdict) -> None:
    storage.insert_cluster_judgment(
        sig_hash=verdict.sig_hash,
        evaluator_name=verdict.evaluator_name,
        timestamp=time.time(),
        pass_count=verdict.pass_count,
        fail_count=verdict.fail_count,
        notes={
            "notes": verdict.notes,
            "representative_failures": verdict.representative_failures,
            "samples_evaluated": verdict.samples_evaluated,
        },
    )


def evaluate_all_clusters(
    evaluator: Evaluator,
    *,
    mode: str = "ordered",
    n_samples: int = 3,
    max_cost_usd: float | None = None,
    persist: bool = True,
    on_progress: Callable[[str, JudgeVerdict], None] | None = None,
    rng: random.Random | None = None,
) -> dict[str, JudgeVerdict]:
    """Run `evaluator` on every cluster currently in the DB.

    The cost-cap is enforced *before* any LLM calls — predicted cost is
    `n_clusters × n_samples × per_call_cost`. For the free no-exceptions
    evaluator this is always 0 and the cap is a no-op.

    Raises JudgeCostCapExceeded if the predicted cost would breach the cap.
    """
    clusters = cluster.list_clusters(mode=mode, limit=1000)
    if max_cost_usd is not None and max_cost_usd >= 0:
        predicted = _estimate_judge_cost_usd(evaluator, len(clusters), n_samples)
        if predicted > max_cost_usd:
            raise JudgeCostCapExceeded(
                f"predicted cost ${predicted:.4f} exceeds cap ${max_cost_usd:.4f} "
                f"({len(clusters)} clusters × {n_samples} samples). "
                "Lower --samples, raise --max-cost-usd, or use no_exceptions_evaluator."
            )
    out: dict[str, JudgeVerdict] = {}
    for cl in clusters:
        verdict = evaluate_cluster(
            cl.sig_hash,
            evaluator,
            n_samples=n_samples,
            mode=mode,
            persist=persist,
            rng=rng,
        )
        out[cl.sig_hash] = verdict
        if on_progress is not None:
            try:
                on_progress(cl.sig_hash, verdict)
            except Exception:
                pass
    return out


def get_latest_judgment(sig_hash: str) -> dict[str, Any] | None:
    """Convenience pass-through to storage for dashboard/CLI consumers."""
    return storage.get_latest_cluster_judgment(sig_hash)
