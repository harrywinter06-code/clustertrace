"""Prompt-engineering analysis engine.

Two surfaces:
- ``analyse_prompt(text)`` — single-prompt critique (used by the /prompts critique tab)
- ``patterns_from_traces(succeeded, dead_ended)`` — compare two corpora of past
  prompts and report which heuristics correlate with success vs dead-end
  (used by the /prompts patterns-from-data tab)

Both are pure functions over text + integers. No LLM call, no I/O. Returns
plain dicts the dashboard serialises straight to JSON.

LLM-deepen lives in `dashboard/app.py` because it talks to the Anthropic SDK
and needs request-time config.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Heuristic catalogue
# ---------------------------------------------------------------------------
#
# Each heuristic is a (name, check) pair. `check(prompt)` returns True if the
# heuristic FIRES on the prompt. Heuristics are intentionally cheap and
# explainable — they document what we count, not what we recommend, so a user
# can disagree with the rule rather than the conclusion.

_VAGUE_VERBS = {
    "fix",
    "improve",
    "clean",
    "cleanup",
    "refactor",
    "polish",
    "tidy",
    "tweak",
    "update",
    "modernise",
    "modernize",
    "rework",
    "redo",
    "make better",
    "make it better",
    "make this better",
}

_ACCEPTANCE_MARKERS = (
    "should",
    "must",
    "must not",
    "should not",
    "if ",
    "until ",
    "when ",
    "expect ",
    "acceptance",
    "criteria",
    "passes if",
    "definition of done",
)

_SCOPE_MARKERS = (
    "only",
    " just ",
    "specifically",
    "limit to",
    "don't touch",
    "do not touch",
    "leave",
    "scope",
    "in scope",
    "out of scope",
)

_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(/])"
    r"(?:[A-Za-z]:\\|\.{1,2}\\|/)?"
    r"[\w\-./\\]+\.[A-Za-z0-9]{1,8}"
    r"(?:[\s`'\")]|$)"
)

_CONNECTIVE_RE = re.compile(r"\b(?:and|also|plus|then|after that|additionally)\b", re.IGNORECASE)

_TESTS_MARKERS = ("test", "pytest", "unittest", "vitest", "jest", "rspec")
_ERROR_QUOTED_RE = re.compile(r"`[^`]{6,}`|\"[^\"]{6,}\"|'[^']{6,}'")
_HEDGE_WORDS = ("maybe", "perhaps", "i think", "i guess", "kinda", "sort of", "somehow")


def _lower(text: str) -> str:
    return text.lower()


def heuristic_results(prompt: str) -> dict[str, bool]:
    """Run every heuristic on a single prompt. Returns name -> bool."""
    p = prompt or ""
    lower = _lower(p)
    words = p.split()
    n_chars = len(p)

    starts_vague = False
    if words:
        first_two = " ".join(words[:2]).lower()
        first_word = words[0].lower()
        starts_vague = first_word in _VAGUE_VERBS or first_two in _VAGUE_VERBS

    return {
        "very_short": n_chars > 0 and n_chars < 40,
        "very_long": n_chars > 1200,
        "mentions_file_path": bool(_FILE_PATH_RE.search(p)),
        "starts_with_vague_verb": starts_vague,
        "has_acceptance_criteria": any(m in lower for m in _ACCEPTANCE_MARKERS),
        "has_scope_marker": any(m in lower for m in _SCOPE_MARKERS),
        "multiple_unrelated_asks": len(_CONNECTIVE_RE.findall(p)) >= 3,
        "mentions_tests": any(t in lower for t in _TESTS_MARKERS),
        "quotes_error_message": bool(_ERROR_QUOTED_RE.search(p)),
        "uses_hedging": any(h in lower for h in _HEDGE_WORDS),
        "is_a_question": p.rstrip().endswith("?"),
    }


# ---------------------------------------------------------------------------
# Single-prompt critique
# ---------------------------------------------------------------------------

# Each rule is (heuristic_key, polarity, severity, message).
# polarity = "fires_is_bad" if heuristic firing means the prompt is weaker,
#            "fires_is_good" if firing means the prompt is stronger,
#            "neutral" — surface but don't grade.
_CRITIQUE_RULES: list[tuple[str, str, str, str]] = [
    ("very_short", "fires_is_bad", "high", "Very short prompt — under 40 characters. Most short prompts are missing context (what file, which constraint, what 'done' looks like)."),
    ("very_long", "fires_is_bad", "low", "Quite long (over 1200 characters). Either bundling multiple tasks, or adding context that won't be cached on re-runs."),
    ("starts_with_vague_verb", "fires_is_bad", "high", "Starts with a vague verb ('fix', 'improve', 'refactor', etc.) — be explicit about what's wrong and what 'fixed' means."),
    ("multiple_unrelated_asks", "fires_is_bad", "med", "Three or more connectives detected ('and', 'also', 'then'…). If unrelated tasks, split into separate sessions — cache hits work better and context stays focused."),
    ("uses_hedging", "fires_is_bad", "low", "Hedging language ('maybe', 'I think', 'sort of') leaves ambiguity. Pick a position."),
    ("mentions_file_path", "fires_is_good", "high", "Names a file — gives a clear anchor and limits scope."),
    ("has_acceptance_criteria", "fires_is_good", "med", "States acceptance criteria ('should', 'must', 'if X then'…) — makes 'done' checkable."),
    ("has_scope_marker", "fires_is_good", "med", "Has a scope marker ('only', 'just', 'don't touch')."),
    ("quotes_error_message", "fires_is_good", "med", "Quotes a specific error / output — gives the model an exact target."),
    ("mentions_tests", "fires_is_good", "low", "Mentions tests — useful as ground truth."),
]


def analyse_prompt(prompt: str) -> dict[str, Any]:
    """Critique a single prompt against the catalogue. Returns:
        {
          "char_count": int,
          "word_count": int,
          "findings": [
            {"key": "...", "severity": "high|med|low",
             "polarity": "weak|strong", "message": "..."},
            ...
          ],
        }
    """
    p = prompt or ""
    fired = heuristic_results(p)
    findings: list[dict[str, Any]] = []
    for key, polarity, severity, message in _CRITIQUE_RULES:
        if not fired.get(key):
            continue
        findings.append(
            {
                "key": key,
                "severity": severity,
                "polarity": "weak" if polarity == "fires_is_bad" else "strong",
                "message": message,
            }
        )
    # Sort: weaknesses first (so the eye lands on what to fix), then by severity.
    sev_order = {"high": 0, "med": 1, "low": 2}
    findings.sort(key=lambda f: (f["polarity"] != "weak", sev_order[f["severity"]]))
    return {
        "char_count": len(p),
        "word_count": len(p.split()),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Corpus comparison — patterns from data
# ---------------------------------------------------------------------------


def _corpus_rates(prompts: list[str]) -> dict[str, float]:
    """Per-heuristic firing rate across a corpus."""
    if not prompts:
        return {k: 0.0 for k in heuristic_results("").keys()}
    total = len(prompts)
    out: dict[str, float] = {k: 0 for k in heuristic_results("").keys()}
    for p in prompts:
        for k, v in heuristic_results(p).items():
            if v:
                out[k] += 1
    return {k: out[k] / total for k in out}


_HEURISTIC_LABELS: dict[str, str] = {
    "very_short": "Very short (under 40 chars)",
    "very_long": "Very long (over 1200 chars)",
    "mentions_file_path": "Names a file path",
    "starts_with_vague_verb": "Starts with vague verb",
    "has_acceptance_criteria": "Has acceptance criteria",
    "has_scope_marker": "Has scope marker",
    "multiple_unrelated_asks": "3+ connectives (likely bundled)",
    "mentions_tests": "Mentions tests",
    "quotes_error_message": "Quotes an error / output",
    "uses_hedging": "Uses hedging language",
    "is_a_question": "Is phrased as a question",
}


def patterns_from_traces(succeeded: list[str], dead_ended: list[str]) -> dict[str, Any]:
    """Compare two prompt corpora; return per-heuristic prevalence + delta.

    Sort the table by absolute delta so the most-discriminating heuristics
    surface first — those are the ones with predictive signal for the user.
    """
    succ = _corpus_rates(succeeded)
    dead = _corpus_rates(dead_ended)
    rows: list[dict[str, Any]] = []
    for key, label in _HEURISTIC_LABELS.items():
        s_rate = succ.get(key, 0.0)
        d_rate = dead.get(key, 0.0)
        rows.append(
            {
                "key": key,
                "label": label,
                "succeeded_rate": s_rate,
                "dead_ended_rate": d_rate,
                "delta": s_rate - d_rate,
            }
        )
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return {
        "succeeded_count": len(succeeded),
        "dead_ended_count": len(dead_ended),
        "rows": rows,
    }
