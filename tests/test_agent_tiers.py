"""Model tiers in the agent loop: rule-based start, escalation, auto-escalation, fallback."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from test_agent_graph import PLAN, Chats, events, finish, make_settings, run, sandbox

from deskpilot.agent import RunResult
from deskpilot.fake_model import ai_tool_call
from deskpilot.settings import Budgets
from deskpilot.tiers import ESCALATION_TOOL_NAME
from deskpilot.trace import EventKind, Outcome

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

CODE_GOAL = "write a script that lists files"
PLAIN_GOAL = "tell me about the sandbox"


def escalations(result: RunResult) -> list[dict[str, Any]]:
    return [e.payload for e in events(result, EventKind.MODEL_ESCALATED)]


def test_code_goal_starts_on_the_strong_model(tmp_path: Path) -> None:
    settings = make_settings(sandbox(tmp_path))
    chats = Chats(settings, strong=[PLAN, finish()])
    result, _, _ = run(tmp_path, CODE_GOAL, chats, settings=settings)
    assert chats.calls == [settings.strong_model_id]
    assert result.tier.start == "strong" and result.tier.final == "strong"
    assert result.outcome is Outcome.UNVERIFIED  # finish(success) without any checked step
    assert escalations(result)[0]["source"] == "rule"


def test_model_requests_a_stronger_model(tmp_path: Path) -> None:
    settings = make_settings(sandbox(tmp_path))
    ask = ai_tool_call(ESCALATION_TOOL_NAME, {"reason": "needs careful reasoning"})
    chats = Chats(settings, fast=[PLAN, ask], strong=[finish()])
    result, _, _ = run(tmp_path, PLAIN_GOAL, chats, settings=settings)
    assert chats.calls == [settings.model_id, settings.strong_model_id]
    assert result.tier.escalated and result.tier.final == "strong"
    assert result.model_calls == 3  # plan, the request, the answer on the strong model
    assert [p["source"] for p in escalations(result)] == ["model"]
    assert ESCALATION_TOOL_NAME in chats.fast.bind_calls[-1]["tools"]
    assert ESCALATION_TOOL_NAME not in chats.strong.bind_calls[-1]["tools"]


def test_two_failed_assessments_escalate_automatically(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(root)
    missing = [
        ai_tool_call("files__read_text", {"path": str(root / name)}) for name in ("x.txt", "y.txt")
    ]
    chats = Chats(settings, fast=[PLAN, *missing], strong=[finish(False, "both missing")])
    result, _, _ = run(tmp_path, PLAIN_GOAL, chats, settings=settings)
    assert [s.ok for s in result.steps] == [False, False]
    assert result.tier.escalated
    assert [p["source"] for p in escalations(result)] == ["auto"]
    assert chats.calls == [settings.model_id, settings.strong_model_id]
    assert result.outcome is Outcome.FAILED


def test_exhausted_strong_budget_falls_back_to_fast(tmp_path: Path) -> None:
    settings = make_settings(sandbox(tmp_path), budgets=Budgets(strong_model_calls=1))
    chats = Chats(settings, fast=[finish()], strong=[PLAN])
    result, _, _ = run(tmp_path, CODE_GOAL, chats, settings=settings)
    assert chats.calls == [settings.strong_model_id, settings.model_id]
    assert result.tier.final == "fast"
    assert result.outcome is Outcome.UNVERIFIED  # finish(success) without any checked step
    assert any(p.get("fallback") for p in escalations(result))
