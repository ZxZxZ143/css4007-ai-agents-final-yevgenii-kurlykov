import json
import threading

import pytest

from deskpilot.limits import HARD_MAX
from deskpilot.model import TokenUsage
from deskpilot.policy.budgets import BudgetExhausted, BudgetKind, BudgetTracker
from deskpilot.settings import Budgets


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make(**kw: int) -> tuple[BudgetTracker, Clock]:
    clock = Clock()
    return BudgetTracker(Budgets(**kw), clock=clock), clock


def test_steps() -> None:
    t, _ = make(steps=2)
    t.charge_step()
    assert t.check_step().ok
    t.charge_step()
    c = t.check_step()
    assert not c.ok and c.kind is BudgetKind.STEPS
    assert c.stop_reason == "budget_exhausted:steps"
    with pytest.raises(BudgetExhausted) as e:
        t.charge_step()
    assert e.value.stop_reason == "budget_exhausted:steps"
    assert (e.value.limit, e.value.used) == (2, 2)


def test_model_calls() -> None:
    t, _ = make(model_calls=1)
    assert t.check_model_call().ok
    t.charge_model_call()
    assert t.check_model_call().stop_reason == "budget_exhausted:model_calls"
    with pytest.raises(BudgetExhausted):
        t.charge_model_call()


def test_tool_calls_and_file_ops() -> None:
    t, _ = make(tool_calls=2, file_ops_per_run=3)
    t.charge_tool_call(file_ops=2)
    assert t.check_tool_call(file_ops=1).ok
    c = t.check_tool_call(file_ops=2)
    assert c.kind is BudgetKind.FILE_OPS and c.stop_reason == "budget_exhausted:file_ops"
    with pytest.raises(BudgetExhausted) as e:
        t.charge_tool_call(file_ops=2)
    assert e.value.kind is BudgetKind.FILE_OPS
    assert t.snapshot()["used"]["tool_calls"] == 1  # failed charge not applied
    t.charge_tool_call(file_ops=1)
    assert t.check_tool_call().stop_reason == "budget_exhausted:tool_calls"
    with pytest.raises(BudgetExhausted) as e2:
        t.charge_tool_call()
    assert e2.value.kind is BudgetKind.TOOL_CALLS


def test_negative_file_ops() -> None:
    t, _ = make()
    with pytest.raises(ValueError):
        t.check_tool_call(file_ops=-1)
    with pytest.raises(ValueError):
        t.charge_tool_call(file_ops=-1)


def test_retries_per_operation() -> None:
    t, _ = make(retries_per_operation=1)
    assert t.check_retry("a").ok
    t.charge_retry("a")
    assert t.check_retry("a").stop_reason == "budget_exhausted:retries"
    with pytest.raises(BudgetExhausted) as e:
        t.charge_retry("a")
    assert e.value.kind is BudgetKind.RETRIES
    assert t.check_retry("b").ok
    t.charge_retry("b")


def test_zero_retries() -> None:
    t, _ = make(retries_per_operation=0)
    assert not t.check_retry("a").ok
    with pytest.raises(BudgetExhausted):
        t.charge_retry("a")


def test_tabs() -> None:
    t, _ = make(browser_tabs=2)
    t.open_tab()
    assert t.check_open_tab().ok
    t.open_tab()
    assert t.check_open_tab().stop_reason == "budget_exhausted:browser_tabs"
    with pytest.raises(BudgetExhausted):
        t.open_tab()
    t.close_tab()
    assert t.check_open_tab().ok
    t.close_tab()
    t.close_tab()
    assert t.snapshot()["used"]["browser_tabs"] == 0


def test_run_time() -> None:
    t, clock = make(run_seconds=10)
    clock.now += 9.5
    assert t.check_step().ok
    clock.now += 0.5
    checks = (
        t.check_step(),
        t.check_model_call(),
        t.check_tool_call(),
        t.check_retry("x"),
        t.check_open_tab(),
    )
    for c in checks:
        assert c.kind is BudgetKind.RUN_TIME
        assert c.stop_reason == "budget_exhausted:run_time"
    with pytest.raises(BudgetExhausted) as e:
        t.charge_step()
    assert e.value.kind is BudgetKind.RUN_TIME
    assert t.remaining()["run_time"] == 0.0


def test_tokens_and_overshoot() -> None:
    t, _ = make(tokens=100)
    t.charge_model_call(TokenUsage(input_tokens=30, output_tokens=20, total_tokens=0))
    assert t.snapshot()["used"]["tokens"] == 50
    t.charge_model_call(TokenUsage(input_tokens=1, output_tokens=1, total_tokens=49))
    assert t.check_step().ok  # 99 < 100
    t.charge_model_call(TokenUsage(input_tokens=500, output_tokens=0, total_tokens=500))
    assert t.snapshot()["used"]["tokens"] == 599
    assert t.remaining()["tokens"] == 0
    for c in (t.check_step(), t.check_model_call(), t.check_tool_call()):
        assert c.kind is BudgetKind.TOKENS and c.stop_reason == "budget_exhausted:tokens"
    with pytest.raises(BudgetExhausted) as e:
        t.charge_tool_call()
    assert e.value.kind is BudgetKind.TOKENS


def test_defensive_clamp() -> None:
    b = Budgets.model_construct(
        steps=10**6,
        model_calls=10**6,
        tool_calls=10**6,
        retries_per_operation=10**6,
        file_ops_per_run=10**6,
        browser_tabs=10**6,
        command_timeout_s=10**6,
        run_seconds=10**6,
        tokens=10**9,
    )
    t = BudgetTracker(b)
    assert t.command_timeout_s == HARD_MAX["command_timeout_s"]
    lim = t.snapshot()["limits"]
    assert lim["steps"] == HARD_MAX["steps"]
    assert lim["tokens"] == HARD_MAX["tokens"]
    assert lim["retries"] == HARD_MAX["retries_per_operation"]
    assert lim["file_ops"] == HARD_MAX["file_ops_per_run"]
    assert lim["run_time"] == HARD_MAX["run_seconds"]


def test_snapshot_and_remaining() -> None:
    t, clock = make(steps=1)
    t.charge_step()
    t.charge_retry("op")
    clock.now += 1.5
    snap = t.snapshot()
    json.dumps(snap)
    assert snap["elapsed_s"] == 1.5 and snap["retries_by_operation"] == {"op": 1}
    rem = t.remaining()
    assert rem["steps"] == 0 and all(v >= 0 for v in rem.values())


def test_concurrent_tool_calls_respect_limit() -> None:
    t, _ = make(tool_calls=50)
    ok: list[int] = []
    lock = threading.Lock()

    def work() -> None:
        for _ in range(20):
            try:
                t.charge_tool_call()
            except BudgetExhausted:
                continue
            with lock:
                ok.append(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(ok) == 50
    assert t.snapshot()["used"]["tool_calls"] == 50


def test_strong_call_counts_as_model_call() -> None:
    t, _ = make(model_calls=3, strong_model_calls=1)
    t.charge_model_call(strong=True)
    used = t.snapshot()["used"]
    assert used["model_calls"] == 1 and used["strong_model_calls"] == 1


def test_strong_blocked_when_strong_budget_exhausted_but_model_calls_remain() -> None:
    t, _ = make(model_calls=5, strong_model_calls=1)
    t.charge_model_call(strong=True)
    check = t.check_model_call(strong=True)
    assert not check.ok and check.stop_reason == "budget_exhausted:strong_model_calls"
    assert t.check_model_call().ok
    with pytest.raises(BudgetExhausted) as exc:
        t.charge_model_call(TokenUsage(total_tokens=10), strong=True)
    assert exc.value.kind is BudgetKind.STRONG_MODEL_CALLS
    used = t.snapshot()["used"]
    assert used["model_calls"] == 1 and used["strong_model_calls"] == 1 and used["tokens"] == 0
    t.charge_model_call()


def test_strong_blocked_when_model_calls_exhausted_applies_nothing() -> None:
    t, _ = make(model_calls=1, strong_model_calls=5)
    t.charge_model_call()
    with pytest.raises(BudgetExhausted) as exc:
        t.charge_model_call(strong=True)
    assert exc.value.kind is BudgetKind.MODEL_CALLS
    assert t.snapshot()["used"]["strong_model_calls"] == 0


def test_strong_limit_clamped_to_hard_max() -> None:
    b = Budgets.model_construct(strong_model_calls=10**6)
    assert BudgetTracker(b).snapshot()["limits"]["strong_model_calls"] == 20
