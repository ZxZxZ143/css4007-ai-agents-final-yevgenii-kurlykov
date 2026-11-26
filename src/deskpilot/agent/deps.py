"""Run dependencies: one object wiring settings, registry, gate, budgets, tiers, trace and cache."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

from deskpilot.adapters import ToolContext, ToolRegistry, register_file_tools, register_system_tools
from deskpilot.cache import RecipeCache
from deskpilot.model import ModelClient, ModelConfig
from deskpilot.policy import (
    ActionGate,
    ApprovalRequest,
    ApprovalStore,
    BudgetTracker,
    PathPolicy,
)
from deskpilot.settings import Settings
from deskpilot.tiers import ModelPool, TierController, initial_tier
from deskpilot.trace import TraceRecorder

Approver = Callable[[ApprovalRequest], bool]


def deny_all(_request: ApprovalRequest) -> bool:
    return False


@dataclass
class RunDeps:
    settings: Settings
    mode: Literal["mock", "real"]
    registry: ToolRegistry
    path_policy: PathPolicy
    budgets: BudgetTracker
    approvals: ApprovalStore
    gate: ActionGate
    tiers: TierController
    pool: ModelPool
    trace: TraceRecorder
    ctx: ToolContext
    cache: RecipeCache | None
    approver: Approver
    start_tier: str
    trace_path: Path

    @property
    def run_id(self) -> str:
        return self.trace.run_id


def build_deps(
    settings: Settings,
    goal: str,
    *,
    chat_factory: Callable[[str], BaseChatModel],
    mode: Literal["mock", "real"] = "mock",
    trace_path: Path,
    cache: RecipeCache | None = None,
    approver: Approver = deny_all,
    extra_tools: Callable[[ToolRegistry], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunDeps:
    """Build everything one run needs. `extra_tools` lets tests or later adapters add tools."""
    registry = ToolRegistry()
    register_system_tools(registry)
    register_file_tools(registry)
    if extra_tools is not None:
        extra_tools(registry)
    run_id = uuid.uuid4().hex
    trace = TraceRecorder(trace_path, run_id)
    path_policy = PathPolicy.from_settings(settings)
    budgets = BudgetTracker(settings.budgets, clock=clock)
    approvals = ApprovalStore()
    gate = ActionGate(
        settings,
        tool_risks=registry.tool_risks(),
        paths=path_policy,
        budgets=budgets,
        approvals=approvals,
        trace=trace,
        file_ops=registry.file_ops,
    )
    tiers = TierController(settings, budgets, trace=trace, start=initial_tier(goal))
    start_tier = tiers.tier.value
    pool = ModelPool(
        lambda model_id: ModelClient(chat_factory(model_id), ModelConfig(model_id=model_id))
    )
    ctx = ToolContext(
        run_id=run_id,
        settings=settings,
        path_policy=path_policy,
        mode=mode,
        sandbox_dir=settings.sandbox_dir,
    )
    return RunDeps(
        settings=settings,
        mode=mode,
        registry=registry,
        path_policy=path_policy,
        budgets=budgets,
        approvals=approvals,
        gate=gate,
        tiers=tiers,
        pool=pool,
        trace=trace,
        ctx=ctx,
        cache=cache if settings.cache.enabled else None,
        approver=approver,
        start_tier=start_tier,
        trace_path=trace_path,
    )
