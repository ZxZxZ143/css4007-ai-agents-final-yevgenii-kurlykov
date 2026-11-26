"""Step verification and outcome decision.

Interface (A-05 extends it with goal-level evidence checks and the user-facing report):
- `step_verified(outcome, risk)`: one step is verified if it succeeded and its independent check
  passed; a READ step without a check counts as verified, a MODIFY step without one does not.
- `decide_outcome(steps, finish_success, hard_error)`: the run outcome.
- `default_summary(...)`: fallback text when the model did not provide one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from deskpilot.adapters import ToolOutcome
from deskpilot.agent.observe import _clean
from deskpilot.agent.state import RunResult, StepRecord
from deskpilot.policy.types import RiskClass
from deskpilot.trace import Outcome


def step_verified(outcome: ToolOutcome, risk: RiskClass) -> bool:
    if not outcome.ok:
        return False
    if outcome.verify is not None:
        return outcome.verify.ok
    return risk is RiskClass.READ


def _recovered(index: int, steps: Sequence[StepRecord]) -> bool:
    """A failed step is recovered by a later verified step: any read, the same tool otherwise."""
    failed = steps[index]
    return any(
        s.ok and s.verified and (failed.risk is RiskClass.READ or s.tool == failed.tool)
        for s in steps[index + 1 :]
    )


def decide_outcome(
    steps: Sequence[StepRecord], finish_success: bool | None, hard_error: bool
) -> Outcome:
    """Never `completed` without an ok and verified step; unrecovered modify failures block it."""
    if hard_error:
        return Outcome.FAILED
    done = [s for s in steps if s.ok]
    verified = [s for s in done if s.verified]
    if finish_success:
        if not steps:
            return Outcome.UNVERIFIED  # the model claims success but nothing was checked
        if not done:
            return Outcome.FAILED
        if len(verified) < len(done):
            return Outcome.UNVERIFIED
        unrecovered = any(not s.ok and not _recovered(i, steps) for i, s in enumerate(steps))
        return Outcome.PARTIAL if unrecovered else Outcome.COMPLETED
    if verified:
        return Outcome.PARTIAL
    return Outcome.UNVERIFIED if done else Outcome.FAILED


def default_summary(
    outcome: Outcome, stop_reason: str, steps: Sequence[StepRecord], language: Literal["ru", "en"]
) -> str:
    ok = sum(1 for s in steps if s.ok)
    if language == "ru":
        return f"Итог: {outcome.value} ({stop_reason}); успешных шагов: {ok} из {len(steps)}."
    return f"Result: {outcome.value} ({stop_reason}); successful steps: {ok} of {len(steps)}."


MAX_REPORT_CHARS = 2500
_MAX_LINE = 140
_MAX_SUMMARY = 500
_DENIED_KINDS = frozenset({"denied", "approval_denied"})

_OUTCOME_WORDS = {
    "ru": {
        Outcome.COMPLETED: "выполнено",
        Outcome.PARTIAL: "выполнено частично",
        Outcome.FAILED: "не выполнено",
        Outcome.UNVERIFIED: "выполнено, но не проверено",
    },
    "en": {
        Outcome.COMPLETED: "completed",
        Outcome.PARTIAL: "partially completed",
        Outcome.FAILED: "not completed",
        Outcome.UNVERIFIED: "done, but not verified",
    },
}

_STOP_WORDS = {
    "ru": {
        "finished": "задача завершена",
        "fast_path": "выполнено быстрым путём",
        "fast_path_failed": "быстрый путь не сработал",
        "cache_replay": "повторён сохранённый сценарий",
        "no_tool_call": "модель не выбрала действие",
        "budget_exhausted": "исчерпан лимит",
        "model_error": "ошибка модели",
        "internal_error": "внутренняя ошибка",
        "interrupted": "прервано пользователем",
        "prefilter_refused": "отклонено фильтром безопасности",
    },
    "en": {
        "finished": "the task finished",
        "fast_path": "done by the fast path",
        "fast_path_failed": "the fast path failed",
        "cache_replay": "a saved recipe was replayed",
        "no_tool_call": "the model chose no action",
        "budget_exhausted": "a limit was reached",
        "model_error": "model error",
        "internal_error": "internal error",
        "interrupted": "interrupted by the user",
        "prefilter_refused": "refused by the safety pre-filter",
    },
}

_LABELS = {
    "ru": {
        "ok": "успешно",
        "failed": "ошибка",
        "denied": "отклонено",
        "unchecked": "не проверено",
        "actions": "Действия:",
        "none": "Действий не выполнено.",
        "not_done": "Не выполнено:",
        "stop": "Причина остановки",
        "usage": "Модель: {calls} вызовов, {tokens} токенов; кэш: {cache}; уровень: {tier}",
        "escalated": "повышен с {start}",
        "result": "Результат",
    },
    "en": {
        "ok": "ok",
        "failed": "failed",
        "denied": "denied",
        "unchecked": "unchecked",
        "actions": "Actions:",
        "none": "No actions were executed.",
        "not_done": "Not done:",
        "stop": "Stop reason",
        "usage": "Model: {calls} calls, {tokens} tokens; cache: {cache}; tier: {tier}",
        "escalated": "escalated from {start}",
        "result": "Result",
    },
}


def _clip(text: str, limit: int) -> str:
    # Terminal-safe: no control or format chars (ANSI escapes, bidi overrides).
    one_line = " ".join(_clean(text).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _step_status(step: StepRecord, labels: dict[str, str]) -> str:
    if step.error_kind in _DENIED_KINDS:
        return labels["denied"]
    if not step.ok:
        return labels["failed"]
    return labels["ok"] if step.verified else labels["unchecked"]


def _step_line(number: int, step: StepRecord, labels: dict[str, str]) -> str:
    args = ", ".join(f"{k}={_clip(str(v), 60)}" for k, v in step.args.items())
    line = f"{number}. {step.tool}({args}) - {_step_status(step, labels)}"
    if step.error_kind and step.error_kind not in _DENIED_KINDS:
        line += f" [{step.error_kind}]"
    return _clip(line, _MAX_LINE)


def _stop_text(stop_reason: str, language: Literal["ru", "en"]) -> str:
    head = stop_reason.split(":", 1)[0]
    return _STOP_WORDS[language].get(head) or _clip(stop_reason, 80)


def build_report(result: RunResult, language: Literal["ru", "en"]) -> str:
    """Short user-facing report. Only tool names and short argument summaries come from steps."""
    labels = _LABELS[language]
    lines = [f"{labels['result']}: {_OUTCOME_WORDS[language][result.outcome]}."]
    if result.summary:
        lines.append(_clip(result.summary, _MAX_SUMMARY))
    if result.steps:
        lines.append(labels["actions"])
        lines.extend(_step_line(i, s, labels) for i, s in enumerate(result.steps, 1))
    else:
        lines.append(labels["none"])
    failed = [s for s in result.steps if not s.ok]
    if failed:
        names = ", ".join(sorted({s.tool for s in failed}))
        lines.append(f"{labels['not_done']} {_clip(names, 200)}")
    lines.append(f"{labels['stop']}: {_stop_text(result.stop_reason, language)}.")
    tier = result.tier.final
    if result.tier.escalated:
        tier += f" ({labels['escalated'].format(start=result.tier.start)})"
    lines.append(
        labels["usage"].format(
            calls=result.model_calls, tokens=result.tokens, cache=result.cache, tier=tier
        )
    )
    return "\n".join(lines)[:MAX_REPORT_CHARS]
