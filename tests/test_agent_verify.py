"""Outcome decision and the user-facing report (pure functions, no desktop)."""

from __future__ import annotations

from typing import Literal

import pytest

from deskpilot.adapters import ToolOutcome, VerifyResult
from deskpilot.agent.runner import report_for
from deskpilot.agent.state import RunResult, StepRecord, TierInfo
from deskpilot.agent.verify import (
    MAX_REPORT_CHARS,
    build_report,
    decide_outcome,
    step_verified,
)
from deskpilot.policy.types import RiskClass
from deskpilot.trace import Outcome


def step(
    tool: str = "files.list",
    *,
    ok: bool = True,
    verified: bool = True,
    risk: RiskClass = RiskClass.READ,
    error_kind: str | None = None,
    args: dict | None = None,
) -> StepRecord:
    return StepRecord(
        action_id="a",
        tool=tool,
        args=args or {"path": "C:/sandbox"},
        risk=risk,
        decision_reason="ok",
        ok=ok,
        verified=verified,
        tainted=False,
        error_kind=error_kind,
        source="model",
    )


def result(
    outcome: Outcome,
    steps: tuple[StepRecord, ...] = (),
    *,
    stop: str = "finished",
    summary: str = "",
    cache: str = "disabled",
    tier: TierInfo | None = None,
) -> RunResult:
    return RunResult(
        run_id="r",
        outcome=outcome,
        stop_reason=stop,
        summary=summary,
        steps=steps,
        model_calls=3,
        tokens=1200,
        tier=tier or TierInfo(start="fast", final="fast", escalated=False),
        cache=cache,  # type: ignore[arg-type]
        trace_path="t.jsonl",
    )


def tool_outcome(verify: VerifyResult | None, ok: bool = True) -> ToolOutcome:
    return ToolOutcome(tool="x", ok=ok, verify=verify)


# -- verification and outcome ------------------------------------------------


def test_unverifiable_modify_is_not_verified_and_run_is_unverified() -> None:
    assert step_verified(tool_outcome(None), RiskClass.MODIFY) is False
    modify = step("test.write", verified=False, risk=RiskClass.MODIFY)
    assert decide_outcome([modify], True, False) is Outcome.UNVERIFIED


def test_verify_failure_blocks_completed() -> None:
    bad = tool_outcome(VerifyResult(ok=False, detail="missing"))
    assert step_verified(bad, RiskClass.READ) is False
    steps = [step(), step("files.open", verified=step_verified(bad, RiskClass.READ))]
    assert decide_outcome(steps, True, False) is Outcome.UNVERIFIED


def test_read_without_check_counts_and_completes() -> None:
    assert step_verified(tool_outcome(None), RiskClass.READ) is True
    assert decide_outcome([step()], True, False) is Outcome.COMPLETED


def test_outcome_rules_kept() -> None:
    assert decide_outcome([], True, False) is Outcome.UNVERIFIED
    assert decide_outcome([step()], True, True) is Outcome.FAILED
    failed_modify = step("m", ok=False, verified=False, risk=RiskClass.MODIFY)
    assert decide_outcome([step(), failed_modify], True, False) is Outcome.PARTIAL


# -- report ------------------------------------------------------------------

CASES = {
    Outcome.COMPLETED: ("выполнено.", "completed."),
    Outcome.PARTIAL: ("выполнено частично", "partially completed"),
    Outcome.FAILED: ("не выполнено", "not completed"),
    Outcome.UNVERIFIED: ("выполнено, но не проверено", "done, but not verified"),
}


@pytest.mark.parametrize("outcome", list(CASES))
@pytest.mark.parametrize("language", ["ru", "en"])
def test_report_outcome_words(outcome: Outcome, language: Literal["ru", "en"]) -> None:
    steps = (step(), step("files.open", ok=False, verified=False, error_kind="not_found"))
    text = build_report(result(outcome, steps, summary="all good"), language)
    assert CASES[outcome][0 if language == "ru" else 1] in text
    assert "all good" in text
    assert "1. files.list" in text
    assert "2. files.open" in text
    assert "[not_found]" in text
    assert "files.open" in text.split("\n")[-3] or "files.open" in text


def test_report_lists_not_done_denied_and_usage_ru() -> None:
    steps = (
        step(),
        step("files.read_text", ok=False, verified=False, error_kind="denied"),
    )
    text = build_report(result(Outcome.PARTIAL, steps, stop="budget_exhausted:steps"), "ru")
    assert "отклонено" in text
    assert "Не выполнено: files.read_text" in text
    assert "исчерпан лимит" in text
    assert "3 вызовов, 1200 токенов" in text


def test_report_cache_replay_and_escalation() -> None:
    tier = TierInfo(start="fast", final="strong", escalated=True)
    text = build_report(
        result(Outcome.COMPLETED, (step(),), stop="cache_replay", cache="hit", tier=tier), "en"
    )
    assert "a saved recipe was replayed" in text
    assert "cache: hit" in text
    assert "tier: strong (escalated from fast)" in text
    stored = build_report(result(Outcome.COMPLETED, (step(),), cache="stored"), "en")
    assert "cache: stored" in stored


def test_report_no_steps_unverified() -> None:
    text = build_report(result(Outcome.UNVERIFIED, stop="no_tool_call"), "en")
    assert "No actions were executed." in text
    assert "the model chose no action" in text


def test_report_is_bounded_and_clips_long_untrusted_text() -> None:
    long_args = {"path": "x" * 5000}
    steps = tuple(step(args=long_args) for _ in range(60))
    text = build_report(result(Outcome.COMPLETED, steps, summary="s" * 9000), "en")
    assert len(text) <= MAX_REPORT_CHARS
    assert "x" * 100 not in text


def test_report_for_matches_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    res = result(Outcome.COMPLETED, (step(),), summary="ok")
    assert report_for(res, "en") == build_report(res, "en")

    def boom(*_a: object) -> str:
        raise RuntimeError

    monkeypatch.setattr("deskpilot.agent.runner.build_report", boom)
    assert report_for(res, "en") == "ok"


def test_clip_strips_terminal_control_and_bidi_characters() -> None:
    from deskpilot.agent.verify import _clip

    assert _clip("ok[31mred‮evil", 100) == "ok[31mredevil"
