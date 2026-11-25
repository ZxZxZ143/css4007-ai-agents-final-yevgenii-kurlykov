"""Typed agent state and result models.

LangGraph checkpoints the state, so it holds only plain data (strings, numbers, dicts, LangChain
messages). Steps and plan items are Pydantic models in memory and dicts inside the state.
Non-serializable per-action objects (ActionRequest, ToolOutcome) live in the graph's scratch.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field

from deskpilot.policy.types import RiskClass
from deskpilot.trace import Outcome

Source = Literal["fast_path", "cache", "model"]
CacheStatus = Literal["hit", "miss", "stored", "invalidated", "disabled", "skipped"]
PlanStatus = Literal["pending", "done"]

MAX_SUBGOALS = 7


class RunStop(Exception):
    """Ends the run through verify/report. Not an error for the caller of run_goal."""

    def __init__(self, stop_reason: str, *, hard_error: bool = False) -> None:
        super().__init__(stop_reason)
        self.stop_reason = stop_reason
        self.hard_error = hard_error


class PlanItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=200)
    status: PlanStatus = "pending"


class StepRecord(BaseModel):
    """One attempted action: executed, denied or failed."""

    model_config = ConfigDict(frozen=True)

    action_id: str
    tool: str
    args: dict[str, Any]
    risk: RiskClass
    decision_reason: str
    ok: bool
    verified: bool
    tainted: bool
    error_kind: str | None = None
    observation_id: str | None = None
    source: Source


class AgentState(TypedDict, total=False):
    run_id: str
    goal: str
    route: Literal["fast_path", "model"]
    plan: list[dict[str, Any]]
    messages: list[BaseMessage]
    steps: list[dict[str, Any]]
    untrusted_seen: bool
    consecutive_failures: int
    failures: dict[str, dict[str, Any]]
    consecutive_denials: int
    consecutive_user_denials: int
    reminded: bool
    cache_key: str | None
    cache_status: CacheStatus
    finish_success: bool | None
    hard_error: bool
    outcome: str
    stop_reason: str
    final_text: str
    next_node: str


class TierInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: str
    final: str
    escalated: bool


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    outcome: Outcome
    stop_reason: str
    summary: str
    steps: tuple[StepRecord, ...] = ()
    model_calls: int = 0
    tokens: int = 0
    tier: TierInfo
    cache: CacheStatus
    trace_path: str
    failures: dict[str, dict[str, Any]] = Field(default_factory=dict)


def steps_of(state: AgentState) -> list[StepRecord]:
    return [StepRecord.model_validate(s) for s in state.get("steps", [])]


def plan_of(state: AgentState) -> list[PlanItem]:
    return [PlanItem.model_validate(p) for p in state.get("plan", [])]
