"""Model tiers: rule-based start, model-requested and automatic escalation (ARCHITECTURE 13)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from deskpilot.model import ModelClient, TokenUsage
from deskpilot.policy.budgets import BudgetTracker
from deskpilot.settings import Settings
from deskpilot.trace import EventKind, TraceRecorder

ESCALATION_TOOL_NAME: Final = "request_stronger_model"
REASON_MAX_CHARS: Final = 200
FAILURES_BEFORE_ESCALATION: Final = 2

EscalationSource = Literal["model", "auto", "rule"]


class Tier(StrEnum):
    FAST = "fast"
    STRONG = "strong"


class TierChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier: Tier
    reason: str


# The code noun must be the direct object of a write/create/fix verb and must not be a
# file name (`function.txt`) or the modifier of a folder/file/letter.
_NOT_A_FILE = r"(?!\s+(?:папк|файл|письм|folder|file|director)|\.\w)"
_CODE_SIGNALS: Final = (
    ("code_fence", re.compile(r"```")),
    (
        "write_code_ru",
        re.compile(
            r"\b(?:напиши|написать|создай|создать|составь)\s+"
            r"(?:(?:мне|новый|новую|простой|простую|небольшой|небольшую|короткий|короткую)\s+)*"
            r"(?:код|скрипт\w*|функци\w+|программ\w+|класс\w*|регулярк\w+"
            r"|регулярное\s+выражение|regex)" + _NOT_A_FILE
        ),
    ),
    (
        "write_code_en",
        re.compile(
            r"\b(?:write|create|generate)\s+(?:(?:a|an|the|me|some)\s+)?"
            r"(?:(?:new|simple|small|short|python|powershell)\s+)?"
            r"(?:script|function|program|code|class|regex)\b" + _NOT_A_FILE
        ),
    ),
    (
        "fix_code_ru",
        re.compile(r"\b(?:отладь|исправь)\s+(?:\S+\s+){0,2}(?:код|баг\w*)" + _NOT_A_FILE),
    ),
    (
        "fix_code_en",
        re.compile(
            r"\b(?:fix|debug)\s+(?:(?:the|this|my|a)\s+)?"
            r"(?:bug|code|script|function|program|error|stack trace)\b" + _NOT_A_FILE
        ),
    ),
    (
        "refactor_en",
        re.compile(
            r"\brefactor\s+(?:(?:the|this|my)\s+)?(?:code|module|function|class|script|program)\b"
        ),
    ),
    ("unit_test_en", re.compile(r"\b(?:add|write|create)\s+(?:(?:a|some)\s+)?unit tests?\b")),
    (
        "source_file",
        re.compile(r"\b(?:write|create|напиши|создай)\s+(?:\S+\s+)?\S+\.(?:py|js|ts|cs|cpp|ps1)\b"),
    ),
)


def initial_tier(goal: str) -> TierChoice:
    """Conservative rule: strong only on clear code signals, everything else fast."""
    text = goal.casefold().replace("ё", "е")
    for name, pattern in _CODE_SIGNALS:
        if pattern.search(text):
            return TierChoice(tier=Tier.STRONG, reason=name)
    return TierChoice(tier=Tier.FAST, reason="default")


def escalation_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": ESCALATION_TOOL_NAME,
            "description": (
                "Ask for a stronger model when the task is too hard for you. "
                "Has no side effects; usable once per run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "maxLength": REASON_MAX_CHARS,
                        "description": "Why a stronger model is needed.",
                    }
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    }


class TierController:
    """Per-run tier state. Escalation is one-way and happens at most once."""

    def __init__(
        self,
        settings: Settings,
        budgets: BudgetTracker,
        *,
        trace: TraceRecorder | None = None,
        start: TierChoice | None = None,
    ) -> None:
        self._settings = settings
        self._budgets = budgets
        self._trace = trace
        self._tier = start.tier if start is not None else Tier.FAST
        self._escalated = False
        self._failures = 0
        self._no_strong = False
        if self._tier is Tier.STRONG and start is not None:
            self._emit({"tier": "strong", "source": "rule", "reason": start.reason})

    @property
    def tier(self) -> Tier:
        return self._tier

    @property
    def model_id(self) -> str:
        if self._tier is Tier.STRONG:
            return self._settings.strong_model_id
        return self._settings.model_id

    @property
    def escalated(self) -> bool:
        return self._escalated

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._trace is not None:
            self._trace.emit(EventKind.MODEL_ESCALATED, payload)

    def request_escalation(self, reason: str, source: EscalationSource) -> bool:
        if self._escalated or self._no_strong or self._tier is Tier.STRONG:
            return False
        self._tier = Tier.STRONG
        self._escalated = True
        self._emit({"tier": "strong", "source": source, "reason": reason[:REASON_MAX_CHARS]})
        return True

    def record_assessment(self, success: bool) -> None:
        if self._tier is not Tier.FAST:
            return
        self._failures = 0 if success else self._failures + 1
        if self._failures >= FAILURES_BEFORE_ESCALATION:
            self.request_escalation("two consecutive failed assessments", "auto")

    def select_for_call(self) -> Tier:
        if self._tier is Tier.STRONG and not self._budgets.check_model_call(strong=True).ok:
            self._tier = Tier.FAST
            if not self._no_strong:
                self._no_strong = True
                self._emit({"tier": "fast", "fallback": True, "reason": "strong budget exhausted"})
        return self._tier

    def charge(self, usage: TokenUsage | None = None) -> None:
        self._budgets.charge_model_call(usage, strong=self._tier is Tier.STRONG)

    def model_tools(self, base_tools: Sequence[Any]) -> list[Any]:
        tools = list(base_tools)
        if self._tier is Tier.FAST and not self._escalated and not self._no_strong:
            tools.append(escalation_tool())
        return tools


class ModelPool:
    """One cached client per model id."""

    def __init__(self, factory: Callable[[str], ModelClient]) -> None:
        self._factory = factory
        self._clients: dict[str, ModelClient] = {}

    def get(self, model_id: str) -> ModelClient:
        if model_id not in self._clients:
            self._clients[model_id] = self._factory(model_id)
        return self._clients[model_id]
