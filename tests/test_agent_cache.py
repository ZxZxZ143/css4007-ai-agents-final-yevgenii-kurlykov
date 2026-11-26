"""Recipe cache in the agent loop: store, replay through the gate, miss, invalidation."""

from __future__ import annotations

import sys
from pathlib import Path

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
)

from deskpilot.adapters import ToolRegistry, register_file_tools, register_system_tools
from deskpilot.cache import CandidateStep, RecipeCache, context_fingerprint
from deskpilot.fake_model import ai_tool_call
from deskpilot.policy import RiskClass
from deskpilot.settings import CacheSettings
from deskpilot.trace import EventKind, Outcome

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

GOAL = "show me what is inside the sandbox"


def new_cache(tmp_path: Path) -> RecipeCache:
    return RecipeCache(tmp_path / "cache" / "recipes.json", ttl_s=10_000.0, max_entries=10)


def listing(root: Path) -> list[object]:
    return [PLAN, ai_tool_call("files__open", {"path": str(root / "a.txt")}), finish()]


def first_run(tmp_path: Path, cache: RecipeCache, root: Path) -> None:
    settings = make_settings(root)
    result, _, _ = run(
        tmp_path, GOAL, Chats(settings, fast=listing(root)), settings=settings, cache=cache
    )
    assert result.outcome is Outcome.COMPLETED
    assert result.cache == "stored"
    assert len(cache) == 1


def test_second_run_replays_without_the_model(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    first_run(tmp_path, cache, root)
    settings = make_settings(root)
    chats = Chats(settings)  # empty scripts: any model call would fail the run
    result, deps, _ = run(tmp_path, GOAL.upper() + "!", chats, settings=settings, cache=cache)
    assert result.outcome is Outcome.COMPLETED
    assert result.stop_reason == "cache_replay"
    assert result.cache == "hit"
    assert result.model_calls == 0
    assert chats.calls == []
    assert [s.source for s in result.steps] == ["cache"]
    assert deps.budgets.snapshot()["used"]["tool_calls"] == 1
    decisions = [e for e in events(result, EventKind.POLICY_DECISION) if e.payload.get("tool")]
    assert decisions and all("cache_replay" in d.payload["provenance"] for d in decisions)
    assert {e.kind for e in events(result)} >= {EventKind.CACHE_HIT, EventKind.TOOL_RESULT}


def test_changed_settings_miss(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    first_run(tmp_path, cache, root)
    other = tmp_path / "other"
    other.mkdir()
    settings = make_settings(root).model_copy(update={"allowed_roots": (root, other)})
    chats = Chats(settings, fast=listing(root))
    result, _, _ = run(tmp_path, GOAL, chats, settings=settings, cache=cache)
    assert result.cache == "stored"  # a miss, then a fresh recipe for the new context
    assert chats.calls == [settings.model_id]
    assert EventKind.CACHE_MISS in {e.kind for e in events(result)}
    assert len(cache) == 2


def test_replay_failure_invalidates_and_falls_back_to_the_model(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    first_run(tmp_path, cache, root)
    (root / "a.txt").unlink()  # files.open now fails with not_found
    settings = make_settings(root)
    chats = Chats(settings, fast=[PLAN, finish(False, "the folder is gone")])
    result, _, _ = run(tmp_path, GOAL, chats, settings=settings, cache=cache)
    assert result.cache == "invalidated"
    assert len(cache) == 0
    assert result.model_calls == 2
    assert result.outcome is Outcome.FAILED
    assert EventKind.CACHE_INVALIDATED in {e.kind for e in events(result)}
    assert "saved procedure" in sent_text(chats.fast)
    assert "files.open:failed" in sent_text(chats.fast)


def test_tampered_recipe_cannot_bypass_the_gate(tmp_path: Path) -> None:
    settings = make_settings(sandbox(tmp_path))
    registry = ToolRegistry()
    register_system_tools(registry)
    register_file_tools(registry)
    cache = new_cache(tmp_path)
    evil = CandidateStep(
        tool="files.read_text",
        args={"path": r"C:\Windows\win.ini"},
        risk_class=RiskClass.READ,
        ok=True,
        verified=True,
        tainted=False,
    )
    cache.store(GOAL, context_fingerprint(settings, registry.version()), [evil])
    assert len(cache) == 1
    chats = Chats(settings, fast=[PLAN, finish(False, "nothing to do")])
    result, deps, _ = run(tmp_path, GOAL, chats, settings=settings, cache=cache)
    assert result.cache == "invalidated"
    assert len(cache) == 0
    assert deps.budgets.snapshot()["used"]["tool_calls"] == 0
    assert result.steps[0].decision_reason == "path_hard_deny"
    assert result.steps[0].source == "cache"


def test_tainted_or_denied_runs_are_not_stored(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    settings = make_settings(root)
    read = ai_tool_call("files__read_text", {"path": str(root / "a.txt")})
    lst = ai_tool_call("files__list", {"path": str(root)})
    result, _, _ = run(
        tmp_path,
        "tainted run",
        Chats(settings, fast=[PLAN, read, lst, finish()]),
        settings=settings,
        cache=cache,
    )
    assert result.outcome is Outcome.COMPLETED
    assert result.cache == "miss"
    assert [s.tainted for s in result.steps] == [False, True]
    denied = ai_tool_call("files__read_text", {"path": r"C:\Windows\win.ini"})
    result, _, _ = run(
        tmp_path,
        "denied run",
        Chats(settings, fast=[PLAN, denied, lst, finish()]),
        settings=settings,
        cache=cache,
    )
    assert result.cache == "miss"
    assert len(cache) == 0


def test_disabled_cache_is_reported(tmp_path: Path) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    settings = make_settings(root, cache=CacheSettings(enabled=False))
    result, _, _ = run(
        tmp_path, GOAL, Chats(settings, fast=listing(root)), settings=settings, cache=cache
    )
    assert result.cache == "disabled"
    assert len(cache) == 0


def test_user_denial_during_replay_stops_without_invalidating(tmp_path: Path) -> None:
    from test_agent_graph import touch_tools

    settings = make_settings(sandbox(tmp_path))
    registry = ToolRegistry()
    register_system_tools(registry)
    register_file_tools(registry)
    touch_tools([])(registry)
    cache = new_cache(tmp_path)
    step = CandidateStep(
        tool="test.touch",
        args={"name": "x"},
        risk_class=RiskClass.MODIFY,
        ok=True,
        verified=True,
        tainted=False,
    )
    cache.store(GOAL, context_fingerprint(settings, registry.version()), [step])
    executed: list[str] = []
    chats = Chats(settings)
    result, _, _ = run(
        tmp_path,
        GOAL,
        chats,
        settings=settings,
        cache=cache,
        approver=lambda _r: False,
        extra_tools=touch_tools(executed),
    )
    assert result.stop_reason == "approval_denied"
    assert len(cache) == 1
    assert chats.calls == [] and executed == []
    assert result.cache == "miss"


@pytest.mark.parametrize("tool", ["files__list", "files__read_text"])
def test_content_reads_are_never_stored(tmp_path: Path, tool: str) -> None:
    root = sandbox(tmp_path)
    cache = new_cache(tmp_path)
    settings = make_settings(root)
    path = root if tool == "files__list" else root / "a.txt"
    script = [PLAN, ai_tool_call(tool, {"path": str(path)}), finish()]
    result, _, _ = run(tmp_path, GOAL, Chats(settings, fast=script), settings=settings, cache=cache)
    assert result.outcome is Outcome.COMPLETED
    assert result.cache == "miss"
    assert len(cache) == 0
