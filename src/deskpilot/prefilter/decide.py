"""Combine the lexicon and the classifier into one pre-filter decision.

The decision can refuse a request or flag it as risky. It never grants, approves or relaxes
anything: a "pass" only means the normal action gate and approvals apply as usual.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict

from deskpilot.prefilter.classifier import PrefilterClassifier, load_default
from deskpilot.prefilter.lexicon import scan, strongest

log = logging.getLogger(__name__)

# Fixed by design before the combined holdout run; not tuned on the holdout.
REFUSE_WITH_LEXICON = 0.25  # classifier p(malicious) needed to refuse when the lexicon agrees
REFUSE_CLASSIFIER_ONLY = 0.85  # p(malicious) needed to refuse on the classifier alone

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


def _lexicon_part(goal: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    hits = scan(goal)
    label = strongest(hits)
    intents = tuple(dict.fromkeys(h.intent for h in hits if h.label != "safe"))
    return label, intents, tuple(f"lexicon:{i}" for i in intents)


def evaluate(goal: str, classifier: PrefilterClassifier | None = None) -> PrefilterDecision:
    """Decide refuse / flag / pass for a goal text. Fail-safe: never passes on an error."""
    if not goal.strip():
        return PrefilterDecision(verdict="pass")
    try:
        return _evaluate(goal, classifier)
    except Exception:
        log.warning("pre-filter failed; flagging the request")
        return PrefilterDecision(verdict="flag", reasons=("prefilter_error",))


def _evaluate(goal: str, classifier: PrefilterClassifier | None) -> PrefilterDecision:
    lex_label, intents, lex_reasons = _lexicon_part(goal)
    try:
        model = classifier or load_default()
        result = model.classify(goal)
        flag_threshold = model.flag_threshold
    except Exception:
        log.warning("classifier unavailable; using the lexicon only")
        return PrefilterDecision(
            verdict="refuse" if lex_label == "malicious" else "flag",
            reasons=(*lex_reasons, "classifier_unavailable"),
            lexicon_label=lex_label,
            lexicon_intents=intents,
        )
    p_mal = float(result.probs["malicious"])
    risk = p_mal + float(result.probs["risky"])
    reasons = list(lex_reasons)
    if result.label != "safe":
        reasons.append(f"classifier:{result.label}")
    refuse = (lex_label == "malicious" and p_mal >= REFUSE_WITH_LEXICON) or (
        p_mal >= REFUSE_CLASSIFIER_ONLY
    )
    flag = lex_label in ("risky", "malicious") or risk >= flag_threshold
    verdict: Verdict = "refuse" if refuse else "flag" if flag else "pass"
    return PrefilterDecision(
        verdict=verdict,
        reasons=tuple(reasons),
        lexicon_label=lex_label,
        lexicon_intents=intents,
        classifier_label=result.label,
        malicious_score=p_mal,
        risk_score=risk,
    )
