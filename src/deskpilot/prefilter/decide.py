"""Combine the lexicon and the classifier into one pre-filter decision.

The decision can refuse a request or flag it as risky. It never grants, approves or relaxes
anything: a "pass" only means the normal action gate and approvals apply as usual.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict

from deskpilot.prefilter.classifier import MAX_INPUT_CHARS, PrefilterClassifier, load_default
from deskpilot.prefilter.lexicon import scan, strongest

log = logging.getLogger(__name__)

# Fixed by design before the combined holdout run; not tuned on the holdout.
REFUSE_WITH_LEXICON = 0.25  # classifier p(malicious) needed to refuse when the lexicon agrees
REFUSE_CLASSIFIER_ONLY = 0.85  # p(malicious) needed to refuse on the classifier alone

MAX_GOAL_CHARS = 20_000  # longer goals are refused without further work
_STRIDE = MAX_INPUT_CHARS // 2
_RANK = {"safe": 0, "risky": 1, "malicious": 2}

Verdict = Literal["refuse", "flag", "pass"]


class PrefilterDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    lexicon_label: str = "safe"
    lexicon_intents: tuple[str, ...] = ()
    classifier_label: str | None = None
    malicious_score: float | None = None
    risk_score: float | None = None


def _windows(goal: str) -> list[str]:
    """The whole goal if short, else overlapping windows so no part escapes the input cap."""
    if len(goal) <= MAX_INPUT_CHARS:
        return [goal]
    starts = range(0, len(goal) - MAX_INPUT_CHARS + _STRIDE, _STRIDE)
    return [goal[i : i + MAX_INPUT_CHARS] for i in starts]


def evaluate(goal: str, classifier: PrefilterClassifier | None = None) -> PrefilterDecision:
    """Decide refuse / flag / pass for a goal text. Fail-safe: never passes on an error."""
    if not goal.strip():
        return PrefilterDecision(verdict="pass")
    if len(goal) > MAX_GOAL_CHARS:
        return PrefilterDecision(verdict="refuse", reasons=("goal_too_long",))
    try:
        return _evaluate(goal, classifier)
    except Exception:
        log.warning("pre-filter failed; flagging the request")
        return PrefilterDecision(verdict="flag", reasons=("prefilter_error",))


def _evaluate(goal: str, classifier: PrefilterClassifier | None) -> PrefilterDecision:
    windows = _windows(goal)
    lex_label = "safe"
    intents: dict[str, None] = {}
    for window in windows:
        hits = scan(window)
        label = strongest(hits)
        if _RANK[label] > _RANK[lex_label]:
            lex_label = label
        intents.update(dict.fromkeys(h.intent for h in hits if h.label != "safe"))
    lex_reasons = tuple(f"lexicon:{i}" for i in intents)
    too_long = ("goal_too_long",) if len(windows) > 1 else ()
    try:
        model = classifier or load_default()
        results = [model.classify(w) for w in windows]
        flag_threshold = model.flag_threshold
    except Exception:
        log.warning("classifier unavailable; using the lexicon only")
        return PrefilterDecision(
            verdict="refuse" if lex_label == "malicious" else "flag",
            reasons=(*lex_reasons, *too_long, "classifier_unavailable"),
            lexicon_label=lex_label,
            lexicon_intents=tuple(intents),
        )
    p_mal = max(float(r.probs["malicious"]) for r in results)
    risk = max(float(r.probs["malicious"]) + float(r.probs["risky"]) for r in results)
    worst = max(results, key=lambda r: float(r.probs["malicious"]) + float(r.probs["risky"]))
    reasons = [*lex_reasons, *too_long]
    if worst.label != "safe":
        reasons.append(f"classifier:{worst.label}")
    refuse = (lex_label == "malicious" and p_mal >= REFUSE_WITH_LEXICON) or (
        p_mal >= REFUSE_CLASSIFIER_ONLY
    )
    flag = bool(too_long) or lex_label in ("risky", "malicious") or risk >= flag_threshold
    verdict: Verdict = "refuse" if refuse else "flag" if flag else "pass"
    return PrefilterDecision(
        verdict=verdict,
        reasons=tuple(reasons),
        lexicon_label=lex_label,
        lexicon_intents=tuple(intents),
        classifier_label=worst.label,
        malicious_score=p_mal,
        risk_score=risk,
    )
