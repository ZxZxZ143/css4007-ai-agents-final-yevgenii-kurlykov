"""Run budgets: bounded steps, calls, retries, file ops, tabs, time and tokens.

Limits come from ``Settings.budgets`` but are clamped again to ``HARD_MAX`` here.
Check methods never mutate; charge methods raise ``BudgetExhausted`` without applying
the charge when it would exceed a limit.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from deskpilot.limits import HARD_MAX
from deskpilot.model import TokenUsage
from deskpilot.settings import Budgets


class BudgetKind(StrEnum):
    STEPS = "steps"
    MODEL_CALLS = "model_calls"
    STRONG_MODEL_CALLS = "strong_model_calls"
    TOOL_CALLS = "tool_calls"
    RETRIES = "retries"
    FILE_OPS = "file_ops"
    BROWSER_TABS = "browser_tabs"
    RUN_TIME = "run_time"
    TOKENS = "tokens"


class BudgetExhausted(Exception):
    def __init__(self, kind: BudgetKind, limit: int, used: int) -> None:
        super().__init__(f"budget exhausted: {kind} (used {used}, limit {limit})")
        self.kind = kind
        self.limit = limit
        self.used = used

    @property
    def stop_reason(self) -> str:
        return f"budget_exhausted:{self.kind}"


class BudgetCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    kind: BudgetKind | None = None
    stop_reason: str | None = None
    detail: str = ""


_OK = BudgetCheck(ok=True)


def _clamped(budgets: Budgets, key: str) -> int:
    return min(int(getattr(budgets, key)), HARD_MAX[key])


class BudgetTracker:
    """Thread-safe usage counters for one run."""

    def __init__(self, budgets: Budgets, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self._lock = threading.RLock()
        self._limits: dict[BudgetKind, int] = {
            BudgetKind.STEPS: _clamped(budgets, "steps"),
            BudgetKind.MODEL_CALLS: _clamped(budgets, "model_calls"),
            BudgetKind.STRONG_MODEL_CALLS: _clamped(budgets, "strong_model_calls"),
            BudgetKind.TOOL_CALLS: _clamped(budgets, "tool_calls"),
            BudgetKind.RETRIES: _clamped(budgets, "retries_per_operation"),
            BudgetKind.FILE_OPS: _clamped(budgets, "file_ops_per_run"),
            BudgetKind.BROWSER_TABS: _clamped(budgets, "browser_tabs"),
            BudgetKind.RUN_TIME: _clamped(budgets, "run_seconds"),
            BudgetKind.TOKENS: _clamped(budgets, "tokens"),
        }
        self._command_timeout_s = _clamped(budgets, "command_timeout_s")
        self._used: dict[BudgetKind, int] = {
            k: 0 for k in BudgetKind if k is not BudgetKind.RUN_TIME
        }
        self._retries: defaultdict[str, int] = defaultdict(int)

    @property
    def command_timeout_s(self) -> int:
        return self._command_timeout_s

    def elapsed_s(self) -> float:
        return max(0.0, self._clock() - self._start)

    # -- internal (call with lock held) ------------------------------------

    def _exhausted(self, kind: BudgetKind, need: int = 1) -> BudgetExhausted | None:
        used = self._used[kind]
        limit = self._limits[kind]
        return BudgetExhausted(kind, limit, used) if used + need > limit else None

    def _global_failure(self) -> BudgetExhausted | None:
        limit = self._limits[BudgetKind.RUN_TIME]
        if self.elapsed_s() >= limit:
            return BudgetExhausted(BudgetKind.RUN_TIME, limit, int(self.elapsed_s()))
        return self._exhausted(BudgetKind.TOKENS)

    def _failure(self, kind: BudgetKind, need: int = 1) -> BudgetExhausted | None:
        return self._global_failure() or self._exhausted(kind, need)

    @staticmethod
    def _as_check(exc: BudgetExhausted | None) -> BudgetCheck:
        if exc is None:
            return _OK
        return BudgetCheck(ok=False, kind=exc.kind, stop_reason=exc.stop_reason, detail=str(exc))

    def _retry_failure(self, operation_id: str) -> BudgetExhausted | None:
        if (exc := self._global_failure()) is not None:
            return exc
        used, limit = self._retries[operation_id], self._limits[BudgetKind.RETRIES]
        return BudgetExhausted(BudgetKind.RETRIES, limit, used) if used >= limit else None

    def _model_failure(self, strong: bool) -> BudgetExhausted | None:
        exc = self._failure(BudgetKind.MODEL_CALLS)
        if exc is None and strong:
            exc = self._exhausted(BudgetKind.STRONG_MODEL_CALLS)
        return exc

    def _tool_failure(self, file_ops: int) -> BudgetExhausted | None:
        if file_ops < 0:
            raise ValueError("file_ops must be >= 0")
        return self._failure(BudgetKind.TOOL_CALLS) or self._exhausted(
            BudgetKind.FILE_OPS, file_ops
        )

    # -- pre-dispatch checks (no mutation) -----------------------------------

    def check_step(self) -> BudgetCheck:
        with self._lock:
            return self._as_check(self._failure(BudgetKind.STEPS))

    def check_model_call(self, strong: bool = False) -> BudgetCheck:
        with self._lock:
            return self._as_check(self._model_failure(strong))

    def check_tool_call(self, file_ops: int = 0) -> BudgetCheck:
        with self._lock:
            return self._as_check(self._tool_failure(file_ops))

    def check_retry(self, operation_id: str) -> BudgetCheck:
        with self._lock:
            return self._as_check(self._retry_failure(operation_id))

    def check_open_tab(self) -> BudgetCheck:
        with self._lock:
            return self._as_check(self._failure(BudgetKind.BROWSER_TABS))

    # -- charges (mutate, raise without applying) ----------------------------

    def charge_step(self) -> None:
        with self._lock:
            if (exc := self._failure(BudgetKind.STEPS)) is not None:
                raise exc
            self._used[BudgetKind.STEPS] += 1

    def charge_model_call(self, usage: TokenUsage | None = None, *, strong: bool = False) -> None:
        """Count one model call and add its tokens.

        Tokens of a completed call are always recorded, even if they overshoot the
        limit: usage may exceed the limit after the fact and the next checks fail.
        """
        with self._lock:
            if (exc := self._model_failure(strong)) is not None:
                raise exc
            self._used[BudgetKind.MODEL_CALLS] += 1
            if strong:
                self._used[BudgetKind.STRONG_MODEL_CALLS] += 1
            if usage is not None:
                # Charge the larger figure so an understated total never lowers usage.
                total = max(usage.total_tokens, usage.input_tokens + usage.output_tokens, 0)
                self._used[BudgetKind.TOKENS] += total

    def charge_tool_call(self, file_ops: int = 0) -> None:
        with self._lock:
            if (exc := self._tool_failure(file_ops)) is not None:
                raise exc
            self._used[BudgetKind.TOOL_CALLS] += 1
            self._used[BudgetKind.FILE_OPS] += file_ops

    def charge_retry(self, operation_id: str) -> None:
        with self._lock:
            if (exc := self._retry_failure(operation_id)) is not None:
                raise exc
            self._retries[operation_id] += 1
            self._used[BudgetKind.RETRIES] = max(self._retries.values())

    def open_tab(self) -> None:
        with self._lock:
            if (exc := self._failure(BudgetKind.BROWSER_TABS)) is not None:
                raise exc
            self._used[BudgetKind.BROWSER_TABS] += 1

    def close_tab(self) -> None:
        with self._lock:
            self._used[BudgetKind.BROWSER_TABS] = max(0, self._used[BudgetKind.BROWSER_TABS] - 1)

    # -- reporting -------------------------------------------------------------

    def remaining(self) -> dict[str, int | float]:
        with self._lock:
            out: dict[str, int | float] = {
                str(k): max(0, self._limits[k] - self._used[k]) for k in self._used
            }
            out[str(BudgetKind.RUN_TIME)] = max(
                0.0, self._limits[BudgetKind.RUN_TIME] - self.elapsed_s()
            )
            return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "used": {str(k): v for k, v in self._used.items()},
                "limits": {str(k): v for k, v in self._limits.items()},
                "retries_by_operation": dict(self._retries),
                "elapsed_s": self.elapsed_s(),
            }
