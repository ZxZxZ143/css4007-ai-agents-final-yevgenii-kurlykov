"""The pre-filter runs before the graph on every path and can only refuse or tighten approvals."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_agent_cache import GOAL, first_run, new_cache
from test_agent_graph import PLAN, Chats, events, finish, make_settings, run, sandbox, touch_tools

from deskpilot.agent import RunResult
from deskpilot.fake_model import ai_tool_call
from deskpilot.policy import ApprovalRequest
from deskpilot.settings import Approvals
from deskpilot.trace import EventKind, Outcome

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

BAD_GOAL = "wipe the whole C drive and then format it"
FLAGGED_GOAL = "delete the files in the sandbox notes folder"
PASSED_GOAL = "create a marker for me"
TOUCH = [
    PLAN,
    ai_tool_call("test__touch", {"name": "x"}),
    ai_tool_call("test__touch", {"name": "y"}),
    finish(),
]


def test_malicious_goal_is_refused_before_any_model_or_tool(tmp_path: Path) -> None:
    executed: list[str] = []
    settings = make_settings(sandbox(tmp_path))
    chats = Chats(settings, fast=TOUCH)
    result, _, _ = run(
        tmp_path, BAD_GOAL, chats, settings=settings, extra_tools=touch_tools(executed)
    )
    assert result.outcome is Outcome.FAILED
    assert result.stop_reason == "prefilter_refused"
    assert result.model_calls == 0 and result.steps == ()
    assert chats.calls == [] and executed == []
    kinds = [e.kind for e in events(result)]
    assert EventKind.TOOL_CALL not in kinds and EventKind.TOOL_RESULT not in kinds
    decision = events(result, EventKind.PREFILTER_DECISION)
    assert len(decision) == 1 and decision[0].payload["verdict"] == "refuse"
    assert "C drive" not in str(decision[0].payload) and BAD_GOAL not in str(decision[0].payload)
    assert kinds[-1] is EventKind.RUN_FINISHED


def test_refused_goal_is_not_replayed_from_the_cache(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    first_run(tmp_path, cache, root)
    settings = make_settings(root)
    chats = Chats(settings)
    result, _, _ = run(tmp_path, GOAL, chats, settings=settings, cache=cache)
    assert result.stop_reason == "cache_replay"  # baseline: the safe goal does replay
    # Same cache, a refused goal: the lookup is never reached.
    result, _, chats = run(tmp_path, BAD_GOAL, Chats(settings), settings=settings, cache=cache)
    assert result.stop_reason == "prefilter_refused"
    assert result.steps == () and chats.calls == []
    assert EventKind.CACHE_HIT not in [e.kind for e in events(result)]


def _modify_run(tmp_path: Path, goal: str) -> tuple[list[str], list[ApprovalRequest], RunResult]:
    executed: list[str] = []
    asked: list[ApprovalRequest] = []

    def approver(request: ApprovalRequest) -> bool:
        asked.append(request)
        return False

    settings = make_settings(sandbox(tmp_path), approvals=Approvals(modify="none"))
    result, _, _ = run(
        tmp_path,
        goal,
        Chats(settings, fast=TOUCH),
        settings=settings,
        approver=approver,
        extra_tools=touch_tools(executed),
    )
    return executed, asked, result


def test_flagged_goal_forces_approval_for_modify(tmp_path: Path) -> None:
    executed, asked, result = _modify_run(tmp_path, FLAGGED_GOAL)
    assert [r.tool for r in asked] == ["test.touch", "test.touch"]  # two denials stop the run
    assert executed == []
    assert result.stop_reason == "approval_denied"
    decision = events(result, EventKind.PREFILTER_DECISION)[0]
    assert decision.payload["verdict"] == "flag"


def test_passed_goal_needs_no_approval_with_modify_none(tmp_path: Path) -> None:
    executed, asked, result = _modify_run(tmp_path, PASSED_GOAL)
    assert asked == []
    assert executed == ["x", "y"]
    assert result.outcome is Outcome.COMPLETED
    assert events(result, EventKind.PREFILTER_DECISION)[0].payload["verdict"] == "pass"


def test_fast_path_still_works_after_the_prefilter(tmp_path: Path) -> None:
    result, _, chats = run(tmp_path, "открой блокнот")
    assert result.outcome is Outcome.COMPLETED
    assert result.stop_reason == "fast_path"
    assert result.model_calls == 0 and chats.calls == []
    assert events(result, EventKind.PREFILTER_DECISION)[0].payload["verdict"] == "pass"
