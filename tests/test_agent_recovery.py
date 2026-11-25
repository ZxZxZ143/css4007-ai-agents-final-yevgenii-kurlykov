"""Failure recovery: repeated-failure detection and safe stop (G2). Offline, scripted model."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from test_agent_graph import (
    PLAN,
    Chats,
    events,
    finish,
    make_settings,
    run,
    sandbox,
    sent_text,
    touch_tools,
)

from deskpilot.agent import recovery
from deskpilot.fake_model import ai_tool_call
from deskpilot.settings import Budgets
from deskpilot.trace import EventKind, Outcome

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


def read(root: Path, name: str) -> Any:
    return ai_tool_call("files__read_text", {"path": str(root / name)})


def scripted(settings: Any, *script: Any) -> Chats:
    """Two consecutive failures escalate to the strong model: it plays the rest of the script."""
    steps = list(script[0]) if len(script) == 1 and isinstance(script[0], list) else list(script)
    return Chats(settings, fast=steps[:3], strong=steps[3:])


def recovery_events(result: Any, kind: str) -> list[Any]:
    found = events(result, EventKind.ERROR)
    return [e for e in found if e.payload.get("source") == "recovery" and e.payload["type"] == kind]


def test_ledger_functions_are_pure() -> None:
    key = recovery.action_key("files.read_text", '{"path":"x"}')
    assert key == recovery.action_key("files.read_text", '{"path":"x"}')
    assert key != recovery.action_key("files.read_text", '{"path":"y"}')
    ledger = recovery.note_failure({}, key, "not_found")
    ledger = recovery.note_failure(ledger, key, "timeout")
    assert ledger[key] == {"count": 2, "last_kind": "timeout"}
    assert recovery.is_blocked(ledger, key, 1) and not recovery.is_blocked(ledger, key, 2)
    assert "failed 2 times (last error kind timeout)" in recovery.block_note(ledger, key)
    assert recovery.note_success(ledger, key) == {}
    assert recovery.too_many_in_a_row(5) and not recovery.too_many_in_a_row(4)


def test_insistent_model_is_blocked_then_stopped(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(
        root, budgets=Budgets(strong_model_calls=20)
    )  # retries_per_operation = 2 -> blocked after 3 failures
    missing = read(root, "nope.txt")
    chats = scripted(settings, [PLAN, missing, missing, missing, missing, finish()])
    result, deps, _ = run(tmp_path, "read nope", chats, settings=settings)
    assert result.stop_reason == "repeated_failure"
    assert result.outcome is Outcome.FAILED
    assert len(result.steps) == 3 and not any(s.ok for s in result.steps)
    assert {s.error_kind for s in result.steps} == {"not_found"}
    assert "failed 3 times (last error kind not_found); do not repeat it" in sent_text(chats.strong)
    assert recovery_events(result, "action_blocked")[0].payload["count"] == 3
    stop = recovery_events(result, "repeated_failure")[0]
    assert stop.payload["tool"] == "files.read_text" and "nope" not in str(stop.payload)
    assert deps.budgets.snapshot()["used"]["tool_calls"] == 3


def test_zero_retries_blocks_after_first_failure(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(root, budgets=Budgets(retries_per_operation=0, strong_model_calls=20))
    missing = read(root, "nope.txt")
    chats = scripted(settings, [PLAN, missing, missing, finish()])
    result, _, _ = run(tmp_path, "read nope", chats, settings=settings)
    assert result.stop_reason == "repeated_failure"
    assert len(result.steps) == 1
    assert "failed 1 times" in sent_text(chats.fast)


def test_model_that_switches_tool_after_the_note_completes(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(root, budgets=Budgets(strong_model_calls=20))
    missing = read(root, "nope.txt")
    listing = ai_tool_call("files__list", {"path": str(root)})
    chats = scripted(settings, [PLAN, missing, missing, missing, listing, finish()])
    result, _, _ = run(tmp_path, "find the notes", chats, settings=settings)
    assert result.outcome is Outcome.COMPLETED
    assert result.stop_reason == "finished"
    assert [s.ok for s in result.steps] == [False, False, False, True]
    assert recovery_events(result, "repeated_failure") == []


def test_five_different_failures_in_a_row_stop(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(root, budgets=Budgets(strong_model_calls=20))
    script = [PLAN, *(read(root, f"n{i}.txt") for i in range(6)), finish()]
    result, _, _ = run(tmp_path, "read many", scripted(settings, script), settings=settings)
    assert result.stop_reason == "repeated_failure"
    assert len(result.steps) == 5
    assert recovery_events(result, "consecutive_failures")[0].payload["count"] == 5
    assert recovery_events(result, "action_blocked") == []


def test_a_success_resets_the_consecutive_count(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(root, budgets=Budgets(strong_model_calls=20))
    ok = read(root, "a.txt")
    script = [PLAN, *(read(root, f"n{i}.txt") for i in range(4)), ok]
    script += [*(read(root, f"m{i}.txt") for i in range(4)), finish()]
    result, _, _ = run(tmp_path, "read many", scripted(settings, script), settings=settings)
    assert result.stop_reason == "finished"
    assert len(result.steps) == 9


def test_transient_read_error_succeeds_on_retry(tmp_path: Path) -> None:
    executed: list[str] = []
    settings = make_settings(sandbox(tmp_path))
    script = [PLAN, ai_tool_call("test__flaky", {"name": "q"}), finish()]
    result, _, _ = run(
        tmp_path,
        "read the flaky thing",
        scripted(settings, script),
        settings=settings,
        extra_tools=touch_tools(executed, flaky=[1]),
    )
    assert [s.ok for s in result.steps] == [False, True]
    assert result.outcome is Outcome.COMPLETED
    assert recovery_events(result, "action_blocked") == []


def test_failed_modify_is_never_retried_automatically(tmp_path: Path) -> None:
    executed: list[str] = []
    settings = make_settings(sandbox(tmp_path))
    flaky = ai_tool_call("test__mflaky", {"name": "x"})
    result, _, _ = run(
        tmp_path,
        "make a marker",
        scripted(settings, [PLAN, flaky, finish(False, "gave up")]),
        settings=settings,
        approver=lambda _r: True,
        extra_tools=touch_tools(executed, flaky=[5]),
    )
    assert len(result.steps) == 1 and executed == []


def test_step_budget_still_stops_the_run(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    settings = make_settings(root, budgets=Budgets(steps=2, strong_model_calls=20))
    script = [PLAN, *(read(root, f"n{i}.txt") for i in range(4))]
    result, _, _ = run(tmp_path, "read many", scripted(settings, script), settings=settings)
    assert result.stop_reason.startswith("budget_exhausted")
    assert len(result.steps) <= 2
