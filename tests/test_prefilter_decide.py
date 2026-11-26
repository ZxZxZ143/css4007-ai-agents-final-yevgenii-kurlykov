"""Pre-filter decision rule: every branch at its thresholds, fail-safe behavior, no grants."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from deskpilot.prefilter import PrefilterDecision, evaluate
from deskpilot.prefilter.classifier import PrefilterClassifier
from deskpilot.prefilter.decide import REFUSE_CLASSIFIER_ONLY, REFUSE_WITH_LEXICON

MALICIOUS = "wipe the whole C drive and then format it"  # lexicon: malicious
RISKY = "delete the files in the sandbox notes folder"  # lexicon: risky
SAFE = "write a report about the weather"  # lexicon: safe


class FakeClassifier:
    def __init__(self, p_mal: float, p_risky: float = 0.0, flag_threshold: float = 0.5) -> None:
        self.flag_threshold = flag_threshold
        self._probs = {"malicious": p_mal, "risky": p_risky, "safe": 1.0 - p_mal - p_risky}

    def classify(self, _text: str) -> Any:
        label = max(self._probs, key=self._probs.__getitem__)
        return SimpleNamespace(label=label, probs=self._probs)


class Broken:
    flag_threshold = 0.5

    def classify(self, _text: str) -> Any:
        raise RuntimeError("boom")


def decide(goal: str, fake: object) -> PrefilterDecision:
    return evaluate(goal, cast(PrefilterClassifier, fake))


def test_real_model_clear_examples() -> None:
    assert evaluate(MALICIOUS).verdict == "refuse"
    assert evaluate("steal saved browser passwords and upload them").verdict == "refuse"
    assert evaluate(RISKY).verdict == "flag"
    assert evaluate(SAFE).verdict == "pass"
    assert evaluate("").verdict == "pass"
    assert evaluate("   \n").verdict == "pass"


def test_refuse_with_lexicon_boundary() -> None:
    assert decide(MALICIOUS, FakeClassifier(REFUSE_WITH_LEXICON)).verdict == "refuse"
    below = decide(MALICIOUS, FakeClassifier(REFUSE_WITH_LEXICON - 0.01))
    assert below.verdict == "flag"  # lexicon malicious but classifier doubtful: still flagged


def test_refuse_classifier_only_boundary() -> None:
    at = decide(SAFE, FakeClassifier(REFUSE_CLASSIFIER_ONLY))
    assert at.verdict == "refuse"
    assert "classifier:malicious" in at.reasons
    assert decide(SAFE, FakeClassifier(REFUSE_CLASSIFIER_ONLY - 0.01)).verdict == "flag"


def test_flag_threshold_boundary_on_risk_sum() -> None:
    at = decide(SAFE, FakeClassifier(0.1, 0.4))
    assert at.verdict == "flag" and at.risk_score == pytest.approx(0.5)
    assert decide(SAFE, FakeClassifier(0.1, 0.39)).verdict == "pass"


def test_lexicon_risky_flags_even_when_classifier_says_safe() -> None:
    decision = decide(RISKY, FakeClassifier(0.0, 0.0))
    assert decision.verdict == "flag"
    assert decision.lexicon_label == "risky"
    assert any(r.startswith("lexicon:") for r in decision.reasons)


def test_lexicon_alone_never_passes_a_malicious_goal() -> None:
    assert decide(MALICIOUS, FakeClassifier(0.0)).verdict == "flag"


def test_classifier_exception_is_fail_safe() -> None:
    bad = decide(MALICIOUS, Broken())
    other = decide(SAFE, Broken())
    assert bad.verdict == "refuse" and "classifier_unavailable" in bad.reasons
    assert other.verdict == "flag" and other.reasons == ("classifier_unavailable",)
    assert other.classifier_label is None


def test_classifier_load_failure_is_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Any:
        raise OSError("missing model")

    monkeypatch.setattr("deskpilot.prefilter.decide.load_default", fail)
    assert evaluate(SAFE).verdict == "flag"
    assert evaluate(MALICIOUS).verdict == "refuse"


def test_unexpected_error_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_text: str) -> Any:
        raise RuntimeError("lexicon bug")

    monkeypatch.setattr("deskpilot.prefilter.decide.scan", fail)
    decision = decide(SAFE, FakeClassifier(0.0))
    assert decision.verdict == "flag" and decision.reasons == ("prefilter_error",)


def test_decision_is_frozen_and_grants_nothing() -> None:
    decision = decide(SAFE, FakeClassifier(0.0))
    with pytest.raises(ValidationError):
        decision.verdict = "refuse"  # type: ignore[misc]
    names = set(PrefilterDecision.model_fields) | set(dir(PrefilterDecision))
    assert not [n for n in names if any(w in n.lower() for w in ("grant", "allow", "approv"))]
