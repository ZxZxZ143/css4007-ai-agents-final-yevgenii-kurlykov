"""Action gate: the code-enforced decision for every tool call, plus the authorize step.

Prompts are never the boundary. evaluate() is a pure decision (no budget charge); authorize()
re-evaluates immediately before execution, consumes the single-use approval and charges the budget.

Permit and Decision are data, not capabilities: executors must call authorize() themselves.
The gate trusts action.paths, so adapters must derive paths from validated args.
A trace error raises out of evaluate()/authorize(); no Permit is issued and the orchestrator
must treat it as a hard stop.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from deskpilot.policy.approvals import ApprovalRequest, ApprovalStore
from deskpilot.policy.budgets import BudgetExhausted, BudgetTracker
from deskpilot.policy.paths import PathPolicy
from deskpilot.policy.types import (
    REASON_VERDICT,
    ActionRequest,
    ApprovalChannel,
    Decision,
    ProvenanceFlag,
    ReasonCode,
    RiskClass,
    Verdict,
)
from deskpilot.settings import Settings
from deskpilot.trace import EventKind, TraceRecorder

_ORDER = {RiskClass.READ: 0, RiskClass.MODIFY: 1, RiskClass.DANGEROUS: 2, RiskClass.FORBIDDEN: 3}
_DETAIL_MAX = 300
_APPROVAL_MISSING = "approval missing, expired, used or for another action"


class PolicyDenied(Exception):
    def __init__(self, decision: Decision, message: str | None = None) -> None:
        super().__init__(message or f"{decision.reason.value}: {decision.detail}")
        self.decision = decision


class Permit(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_fingerprint: str
    decision: Decision
    approval_id: str | None = None


def _higher(a: RiskClass, b: RiskClass) -> RiskClass:
    return a if _ORDER[a] >= _ORDER[b] else b


def _file_ops_default(action: ActionRequest) -> int:
    return len(action.paths)


def _make(
    action: ActionRequest,
    risk: RiskClass,
    reason: ReasonCode,
    channel: ApprovalChannel | None = None,
    detail: str = "",
) -> Decision:
    return Decision(
        verdict=REASON_VERDICT[reason],
        reason=reason,
        risk_class=risk,
        action_fingerprint=action.fingerprint(),
        approval_channel=channel,
        detail=detail[:_DETAIL_MAX],
    )


def _deny(action: ActionRequest, risk: RiskClass, reason: ReasonCode, detail: str) -> Decision:
    return _make(action, risk, reason, detail=detail)


class ActionGate:
    def __init__(
        self,
        settings: Settings,
        *,
        tool_risks: Mapping[str, RiskClass],
        paths: PathPolicy,
        budgets: BudgetTracker,
        approvals: ApprovalStore,
        trace: TraceRecorder | None = None,
        file_ops: Callable[[ActionRequest], int] | None = None,
    ) -> None:
        self._settings = settings
        self._tool_risks = MappingProxyType(dict(tool_risks))
        self._paths = paths
        self._budgets = budgets
        self._approvals = approvals
        self._trace = trace
        self._file_ops = file_ops or _file_ops_default
        self._risky = False
        self._lock = threading.RLock()

    @property
    def risky(self) -> bool:
        with self._lock:
            return self._risky

    def mark_risky(self, reason: str) -> None:
        with self._lock:
            self._risky = True
            self._emit(EventKind.POLICY_DECISION, {"event": "mark_risky", "reason": reason[:200]})

    def evaluate(self, action: ActionRequest, *, operation_id: str | None = None) -> Decision:
        with self._lock:
            if (
                ProvenanceFlag.UNTRUSTED_CONTENT in action.provenance
                or ProvenanceFlag.RISKY_REQUEST in action.provenance
            ):
                self._risky = True
            try:
                decision = self._decide(action)
            except Exception:  # fail closed
                decision = _deny(action, action.risk_class, ReasonCode.ARGS_INVALID, "gate error")
            self._trace_decision(action, decision, operation_id)
            return decision

    def request_approval(self, action: ActionRequest, decision: Decision) -> ApprovalRequest:
        with self._lock:
            fresh = self.evaluate(action)
            if (
                fresh.verdict is not Verdict.NEEDS_APPROVAL
                or fresh.action_fingerprint != decision.action_fingerprint
                or fresh.approval_channel is None
            ):
                raise ValueError("decision does not require approval for this action")
            request = self._approvals.request(action, fresh.approval_channel)
            self._emit(
                EventKind.APPROVAL_REQUESTED,
                {
                    "approval_id": request.approval_id,
                    "action_id": action.action_id,
                    "tool": action.tool,
                    "channel": request.channel.value,
                    "summary": action.summary[:200],
                },
            )
            return request

    def authorize(
        self,
        action: ActionRequest,
        *,
        approval_id: str | None = None,
        operation_id: str | None = None,
    ) -> Permit:
        with self._lock:
            decision = self.evaluate(action, operation_id=operation_id)
            if decision.verdict is Verdict.DENY:
                raise PolicyDenied(decision)
            used_approval: str | None = None
            if decision.verdict is Verdict.NEEDS_APPROVAL:
                info = {"approval_id": approval_id, "action_id": action.action_id}
                self._emit(
                    EventKind.APPROVAL_RESOLVED,
                    {**info, "stage": "consume_attempted"},
                    operation_id=operation_id,
                )
                ok = approval_id is not None and self._approvals.consume(approval_id, action)
                with contextlib.suppress(Exception):  # never burn an approval on a trace error
                    self._emit(
                        EventKind.APPROVAL_RESOLVED,
                        {**info, "stage": "consume_result", "consumed": ok},
                        operation_id=operation_id,
                    )
                if not ok:
                    raise PolicyDenied(decision, _APPROVAL_MISSING)
                used_approval = approval_id
            try:
                self._budgets.charge_tool_call(self._file_ops(action))
            except BudgetExhausted as exc:
                denied = _deny(
                    action, decision.risk_class, ReasonCode.BUDGET_EXHAUSTED, exc.stop_reason
                )
                self._trace_decision(action, denied, operation_id)
                raise PolicyDenied(denied) from exc
            except Exception as exc:  # fail closed
                denied = _deny(action, decision.risk_class, ReasonCode.ARGS_INVALID, "gate error")
                self._trace_decision(action, denied, operation_id)
                raise PolicyDenied(denied) from exc
            return Permit(
                action_fingerprint=action.fingerprint(),
                decision=decision,
                approval_id=used_approval,
            )

    # -- internals (lock held) ---------------------------------------------------

    def _decide(self, action: ActionRequest) -> Decision:
        declared = self._tool_risks.get(action.tool)
        if declared is None:
            return _deny(action, action.risk_class, ReasonCode.UNKNOWN_TOOL, "unknown tool")
        risk = _higher(declared, action.risk_class)
        if ProvenanceFlag.FAST_PATH in action.provenance and risk is not RiskClass.READ:
            return _deny(action, risk, ReasonCode.ARGS_INVALID, "fast path may only propose reads")
        if risk is RiskClass.FORBIDDEN:
            return _deny(action, risk, ReasonCode.FORBIDDEN_RISK_CLASS, "forbidden action")
        check = self._paths.check_all(action.paths)
        if not check.ok:
            return _deny(action, risk, check.reason or ReasonCode.PATH_INVALID, check.detail)
        budget = self._budgets.check_tool_call(self._file_ops(action))
        if not budget.ok:
            detail = budget.stop_reason or budget.detail
            return _deny(action, risk, ReasonCode.BUDGET_EXHAUSTED, detail)
        if risk is RiskClass.READ:
            return _make(action, risk, ReasonCode.READ_ALLOWED)
        if risk is RiskClass.MODIFY:
            channel = self._settings.approvals.modify
            if channel == "none":
                if not self._risky:
                    return _make(action, risk, ReasonCode.MODIFY_ALLOWED_BY_SETTINGS)
                reason = ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED
                return _make(action, risk, reason, ApprovalChannel.TEXT)
            reason = ReasonCode.MODIFY_APPROVAL_REQUIRED
            return _make(action, risk, reason, ApprovalChannel(channel))
        voice = self._settings.approvals.dangerous == "voice"
        channel = ApprovalChannel.VOICE if voice else ApprovalChannel.TEXT
        return _make(action, risk, ReasonCode.DANGEROUS_APPROVAL_REQUIRED, channel)

    def _trace_decision(
        self, action: ActionRequest, decision: Decision, operation_id: str | None
    ) -> None:
        self._emit(
            EventKind.POLICY_DECISION,
            {
                "action_id": action.action_id,
                "tool": action.tool,
                "risk": decision.risk_class.value,
                "verdict": decision.verdict.value,
                "reason": decision.reason.value,
                "detail": decision.detail,
                "fingerprint": decision.action_fingerprint,
                "provenance": sorted(p.value for p in action.provenance),
                "risky": self._risky,
            },
            operation_id=operation_id,
        )

    def _emit(
        self, kind: EventKind, payload: dict[str, object], *, operation_id: str | None = None
    ) -> None:
        if self._trace is not None:
            self._trace.emit(kind, payload, operation_id=operation_id)
