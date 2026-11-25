"""Recovery decisions: bounded retries and repeated-failure detection (G2 guardrail).

- `retry_model(exc, budgets, operation_id)`: may a failed model call be repeated?
- `retry_tool(outcome, budgets, operation_id)`: may a failed tool call be repeated once?
  Both charge the retry budget when they say yes, so the budget bounds the total.
- Failure ledger: plain-data counts per action key (tool + canonical args, hashed), kept in the
  agent state. `note_failure`/`note_success` update it, `is_blocked` and `block_note` decide
  over it, `too_many_in_a_row` caps consecutive failed steps. Pure functions, no I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from deskpilot.adapters import ToolOutcome
from deskpilot.model import ModelError
from deskpilot.policy import BudgetExhausted, BudgetTracker

REPEATED_FAILURE: Final = "repeated_failure"
MAX_CONSECUTIVE_FAILED_STEPS: Final = 5

Ledger = dict[str, dict[str, Any]]


class RetryDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    retry: bool
    stop_reason: str | None = None


_NO = RetryDecision(retry=False)


def _charge(budgets: BudgetTracker, operation_id: str) -> RetryDecision:
    check = budgets.check_retry(operation_id)
    if not check.ok:
        return RetryDecision(retry=False, stop_reason=check.stop_reason)
    try:
        budgets.charge_retry(operation_id)
    except BudgetExhausted as exc:
        return RetryDecision(retry=False, stop_reason=exc.stop_reason)
    return RetryDecision(retry=True)


def retry_model(exc: ModelError, budgets: BudgetTracker, operation_id: str) -> RetryDecision:
    if not exc.retryable:
        return _NO
    return _charge(budgets, operation_id)


def retry_tool(outcome: ToolOutcome, budgets: BudgetTracker, operation_id: str) -> RetryDecision:
    if outcome.ok or outcome.error is None or not outcome.error.retryable:
        return _NO
    return _charge(budgets, operation_id)


def action_key(tool: str, canonical_args: str) -> str:
    """Stable short hash of one exact action (no raw arguments leave this function)."""
    return hashlib.sha256(f"{tool}:{canonical_args}".encode()).hexdigest()[:16]


def note_failure(ledger: Mapping[str, Mapping[str, Any]], key: str, kind: str) -> Ledger:
    count = int(ledger.get(key, {}).get("count", 0)) + 1
    return {**{k: dict(v) for k, v in ledger.items()}, key: {"count": count, "last_kind": kind}}


def note_success(ledger: Mapping[str, Mapping[str, Any]], key: str) -> Ledger:
    return {k: dict(v) for k, v in ledger.items() if k != key}


def failure_count(ledger: Mapping[str, Mapping[str, Any]], key: str) -> int:
    return int(ledger.get(key, {}).get("count", 0))


def is_blocked(
    ledger: Mapping[str, Mapping[str, Any]], key: str, retries_per_operation: int
) -> bool:
    """An action that failed `retries_per_operation + 1` times must not be proposed again."""
    return failure_count(ledger, key) >= retries_per_operation + 1


def block_note(ledger: Mapping[str, Mapping[str, Any]], key: str) -> str:
    entry = ledger.get(key, {})
    return (
        f"This exact action failed {entry.get('count', 0)} times "
        f"(last error kind {entry.get('last_kind', 'unknown')}); do not repeat it. "
        "Choose a different approach or finish."
    )


def too_many_in_a_row(consecutive_failures: int) -> bool:
    return consecutive_failures >= MAX_CONSECUTIVE_FAILED_STEPS
