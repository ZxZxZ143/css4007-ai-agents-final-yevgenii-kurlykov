from pathlib import Path
from typing import Any

import pytest

from deskpilot.model import ModelClient, TokenUsage
from deskpilot.policy.budgets import BudgetExhausted, BudgetTracker
from deskpilot.settings import Budgets, Settings
from deskpilot.tiers import (
    ESCALATION_TOOL_NAME,
    ModelPool,
    Tier,
    TierController,
    escalation_tool,
    initial_tier,
)
from deskpilot.trace import EventKind, TraceEvent, TraceRecorder, read_trace

STRONG_GOALS = [
    "напиши скрипт для переименования файлов",
    "Напиши код на Python, который считает слова",
    "создай функцию сортировки",
    "создай программу на C++",
    "отладь код из буфера",
    "исправь баг в парсере",
    "составь регулярку для телефонов",
    "write a script that renames files",
    "Create a function to parse dates",
    "write code for a binary tree",
    "debug this stack trace",
    "refactor the module",
    "add a unit test for it",
    "fix the bug in main.py",
    "```python\nprint(1)\n``` почему не работает",
    "напиши regex для email",
    "write a regex for phone numbers",
    "debug this code",
    "исправь баг в main.py",
]

FAST_GOALS = [
    "открой блокнот",
    "найди в браузере почему pip install падает и оставь страницу",
    "открой файл script.py",
    "open notepad",
    "open the browser and search for weather",
    "создай папку Отчёты",
    "переименуй файл a.txt в b.txt",
    "покажи список файлов в Загрузках",
    "write a note to my mom",
    "найди скрипт установки в папке Documents",
    "what is a function in math",
    "открой калькулятор",
    "",
    "создай папку программы",
    "create a new class folder",
    "create function.txt",
    "найди regex.txt",
    "напиши письмо про программу",
    "debug",
    "fix typo in readme.py",
    "открой папку с кодом",
    "найди файл main.py",
    "скопируй скрипт в загрузки",
]


def settings(**budgets: int) -> Settings:
    return Settings(model_id="fast-m", strong_model_id="strong-m", budgets=Budgets(**budgets))


def controller(
    tmp_path: Path, *, start: Any = None, **budgets: int
) -> tuple[TierController, BudgetTracker, Path]:
    s = settings(**budgets)
    tracker = BudgetTracker(s.budgets)
    path = tmp_path / "trace.jsonl"
    rec = TraceRecorder(path)
    return TierController(s, tracker, trace=rec, start=start), tracker, path


def events(path: Path) -> list[TraceEvent]:
    return [e for e in read_trace(path) if e.kind is EventKind.MODEL_ESCALATED]


@pytest.mark.parametrize("goal", STRONG_GOALS)
def test_initial_strong(goal: str) -> None:
    assert initial_tier(goal).tier is Tier.STRONG


@pytest.mark.parametrize("goal", FAST_GOALS)
def test_initial_fast(goal: str) -> None:
    choice = initial_tier(goal)
    assert choice.tier is Tier.FAST and choice.reason


def test_escalation_tool_shape() -> None:
    fn = escalation_tool()["function"]
    assert fn["name"] == ESCALATION_TOOL_NAME == "request_stronger_model"
    assert fn["parameters"]["required"] == ["reason"]
    assert fn["parameters"]["properties"]["reason"]["type"] == "string"
    assert fn["parameters"]["properties"]["reason"]["maxLength"] == 200


def test_starts_fast_and_model_ids(tmp_path: Path) -> None:
    c, _, _ = controller(tmp_path)
    assert c.tier is Tier.FAST and c.model_id == "fast-m" and not c.escalated
    c2, _, _ = controller(tmp_path / "b", start=initial_tier("write a script"))
    assert c2.tier is Tier.STRONG and c2.model_id == "strong-m"


def test_model_escalation_once_and_traced(tmp_path: Path) -> None:
    c, _, path = controller(tmp_path)
    assert c.request_escalation("x" * 500, "model") is True
    assert c.tier is Tier.STRONG and c.escalated and c.model_id == "strong-m"
    assert c.request_escalation("again", "model") is False
    found = events(path)
    assert len(found) == 1
    assert found[0].payload["source"] == "model"
    assert len(found[0].payload["reason"]) == 200


def test_no_escalation_when_started_strong(tmp_path: Path) -> None:
    c, _, path = controller(tmp_path, start=initial_tier("debug this code"))
    assert c.request_escalation("r", "model") is False
    assert [e.payload["source"] for e in events(path)] == ["rule"]


def test_rule_start_is_traced(tmp_path: Path) -> None:
    _, _, path = controller(tmp_path, start=initial_tier("write a script"))
    found = events(path)
    assert len(found) == 1
    assert found[0].payload["source"] == "rule" and found[0].payload["tier"] == "strong"
    _, _, fast_path = controller(tmp_path / "f", start=initial_tier("open notepad"))
    assert events(fast_path) == []


def test_fallback_is_permanent_for_rule_started_run(tmp_path: Path) -> None:
    c, _, path = controller(tmp_path, start=initial_tier("write a script"), strong_model_calls=1)
    assert c.select_for_call() is Tier.STRONG
    c.charge()
    assert c.select_for_call() is Tier.FAST
    assert c.request_escalation("again", "model") is False
    c.record_assessment(False)
    c.record_assessment(False)
    assert c.tier is Tier.FAST
    assert c.model_tools([]) == []
    assert [e.payload["source"] for e in events(path) if "source" in e.payload] == ["rule"]


def test_auto_escalation_after_two_consecutive_failures(tmp_path: Path) -> None:
    c, _, path = controller(tmp_path)
    c.record_assessment(False)
    c.record_assessment(True)
    c.record_assessment(False)
    assert c.tier is Tier.FAST
    c.record_assessment(False)
    assert c.tier is Tier.STRONG
    assert [e.payload["source"] for e in events(path)] == ["auto"]
    c.record_assessment(False)
    c.record_assessment(False)
    assert len(events(path)) == 1


def test_model_tools_include_escalation_only_on_fast(tmp_path: Path) -> None:
    c, _, _ = controller(tmp_path)
    base = [{"type": "function", "function": {"name": "t"}}]
    assert c.model_tools(base) == [*base, escalation_tool()]
    c.request_escalation("r", "model")
    assert c.model_tools(base) == base
    assert len(base) == 1


def test_charge_counts_strong_calls(tmp_path: Path) -> None:
    c, tracker, _ = controller(tmp_path, strong_model_calls=2)
    c.charge(TokenUsage(total_tokens=5))
    used = tracker.snapshot()["used"]
    assert used["model_calls"] == 1 and used["strong_model_calls"] == 0
    c.request_escalation("r", "model")
    c.charge()
    assert tracker.snapshot()["used"]["strong_model_calls"] == 1


def test_fallback_to_fast_when_strong_budget_exhausted(tmp_path: Path) -> None:
    c, _, path = controller(tmp_path, strong_model_calls=1)
    c.request_escalation("r", "model")
    assert c.select_for_call() is Tier.STRONG
    c.charge()
    assert c.select_for_call() is Tier.FAST
    assert c.model_id == "fast-m"
    assert c.select_for_call() is Tier.FAST
    fallbacks = [e for e in events(path) if e.payload.get("fallback")]
    assert len(fallbacks) == 1
    assert c.request_escalation("again", "model") is False
    assert escalation_tool() not in c.model_tools([])


def test_strong_charge_raises_when_strong_budget_exhausted(tmp_path: Path) -> None:
    c, _, _ = controller(tmp_path, strong_model_calls=1)
    c.request_escalation("r", "model")
    c.charge()
    with pytest.raises(BudgetExhausted):
        c.charge()


def test_model_pool_caches_per_model_id() -> None:
    made: list[str] = []

    def factory(model_id: str) -> ModelClient:
        made.append(model_id)
        return object()  # type: ignore[return-value]

    pool = ModelPool(factory)
    assert pool.get("a") is pool.get("a")
    assert pool.get("a") is not pool.get("b")
    assert made == ["a", "b"]
