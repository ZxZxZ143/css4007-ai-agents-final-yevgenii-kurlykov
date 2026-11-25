"""The LangGraph agent loop.

route -> (fast_path | cache_lookup -> replay | plan) -> act -> gate -> (approval) -> execute ->
observe -> assess -> act ... -> verify -> report. Edges are driven by `state["next_node"]`, which
each node sets from what it observed. Every tool execution, whether it comes from the fast path,
the recipe cache or the model, goes through gate.evaluate, an optional approval interrupt,
gate.authorize and registry.dispatch with the returned Permit.

Per-action objects that must not be checkpointed (ActionRequest, ToolOutcome) live in `_Scratch`.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from deskpilot.adapters import ToolError, ToolOutcome
from deskpilot.adapters.base import ToolErrorInfo
from deskpilot.agent import recovery
from deskpilot.agent.deps import RunDeps
from deskpilot.agent.observe import observation_id, render_denial, render_observation
from deskpilot.agent.prompts import (
    REMINDER,
    TOOL_FINISH,
    TOOL_SUBMIT_PLAN,
    TOOL_UPDATE_PLAN,
    FinishArgs,
    PlanArgs,
    act_control_tools,
    plan_tool,
    system_prompt,
)
from deskpilot.agent.state import (
    AgentState,
    PlanItem,
    RunStop,
    Source,
    StepRecord,
    plan_of,
    steps_of,
)
from deskpilot.agent.verify import decide_outcome, default_summary, step_verified
from deskpilot.cache import CandidateStep, Recipe, context_fingerprint
from deskpilot.model import ModelError, ModelResponse, ToolCallRequest
from deskpilot.policy import (
    ActionRequest,
    BudgetExhausted,
    Decision,
    PolicyDenied,
    ProvenanceFlag,
    ReasonCode,
    RiskClass,
    Verdict,
)
from deskpilot.router import RouteKind
from deskpilot.router import route as route_goal
from deskpilot.tiers import ESCALATION_TOOL_NAME, Tier
from deskpilot.trace import EventKind, Outcome

MAX_USER_DENIALS: Final = 2
MAX_CONSECUTIVE_DENIALS: Final = 3
_NODES: Final = (
    "route",
    "fast_path",
    "cache_lookup",
    "replay",
    "plan",
    "act",
    "gate",
    "approval_request",
    "approval_wait",
    "execute",
    "observe",
    "assess",
)

Update = dict[str, Any]


@dataclass
class _Scratch:
    action: ActionRequest | None = None
    call_id: str | None = None
    source: Source = "model"
    op_id: str = ""
    decision: Decision | None = None
    approval_id: str | None = None
    outcome: ToolOutcome | None = None
    obs_id: str | None = None
    obs_text: str = ""
    obs_n: int = 0
    reason: str = ""
    recipe: Recipe | None = None
    replay_i: int = 0
    attempt: int = 0


def _guarded(node: Callable[..., Update]) -> Callable[..., Update]:
    """Turn RunStop and BudgetExhausted into a jump to verify/report."""

    @functools.wraps(node)
    def wrapper(self: AgentNodes, state: AgentState) -> Update:
        try:
            return node(self, state)
        except RunStop as stop:
            return {
                "next_node": "verify",
                "stop_reason": stop.stop_reason,
                "hard_error": stop.hard_error,
            }
        except BudgetExhausted as exc:
            return {"next_node": "verify", "stop_reason": exc.stop_reason}

    return wrapper


def _call_id(call: ToolCallRequest, index: int) -> str:
    return call.id or f"call_{index}"


def _ai_message(resp: ModelResponse) -> AIMessage:
    calls = [
        {"name": c.name, "args": c.args, "id": _call_id(c, i), "type": "tool_call"}
        for i, c in enumerate(resp.tool_calls)
    ]
    return AIMessage(content=resp.text, tool_calls=calls)


def _action_summary(tool: str, args: dict[str, Any]) -> str:
    return f"{tool} {json.dumps(args, ensure_ascii=False, default=str)}"[:200]


def _parse_plan(args: dict[str, Any]) -> list[PlanItem]:
    try:
        parsed = PlanArgs.model_validate(args)
        return [PlanItem(text=t.strip()) for t in parsed.subgoals]
    except ValidationError:
        return []


class AgentNodes:
    def __init__(self, deps: RunDeps) -> None:
        self.d = deps
        self.s = _Scratch()

    # -- helpers ---------------------------------------------------------------

    def _emit(self, kind: EventKind, payload: dict[str, Any], op: str | None = None) -> None:
        self.d.trace.emit(kind, payload, operation_id=op)

    @property
    def _retries(self) -> int:
        return self.d.settings.budgets.retries_per_operation

    def _emit_recovery(self, kind: str, tool: str, count: int) -> None:
        self._emit(
            EventKind.ERROR, {"source": "recovery", "type": kind, "tool": tool, "count": count}
        )

    def _stop_if_looping(self, upd: Update) -> Update:
        """Stop after too many failed steps in a row, whatever they were."""
        count = upd.get("consecutive_failures", 0)
        if recovery.too_many_in_a_row(count):
            self._emit_recovery("consecutive_failures", "", count)
            upd.update(next_node="verify", stop_reason=recovery.REPEATED_FAILURE)
        return upd

    def _begin(self, action: ActionRequest, source: Source, call_id: str | None) -> None:
        self.s.action = action
        self.s.call_id = call_id
        self.s.source = source
        self.s.op_id = self.d.trace.new_operation()
        self.s.decision = None
        self.s.approval_id = None
        self.s.outcome = None
        self.s.attempt = 0

    def _call_model(self, messages: list[BaseMessage], base_tools: list[Any]) -> ModelResponse:
        d = self.d
        strong = d.tiers.select_for_call() is Tier.STRONG
        check = d.budgets.check_model_call(strong)
        if not check.ok:
            raise RunStop(check.stop_reason or "budget_exhausted")
        tools = d.tiers.model_tools(base_tools)
        client = d.pool.get(d.tiers.model_id)
        op = d.trace.new_operation()
        names = [t["function"]["name"] for t in tools]
        while True:
            self._emit(
                EventKind.MODEL_CALL,
                {"model_id": d.tiers.model_id, "tier": d.tiers.tier.value, "tools": names},
                op,
            )
            try:
                resp = client.invoke(messages, tools)
                break
            except ModelError as exc:
                info = {
                    "source": "model",
                    "type": type(exc).__name__,
                    "retryable": exc.retryable,
                    "tier": d.tiers.tier.value,
                }
                self._emit(EventKind.ERROR, {**info, "status_code": exc.status_code}, op)
                decision = recovery.retry_model(exc, d.budgets, op)
                if not decision.retry:
                    reason = decision.stop_reason or f"model_error:{type(exc).__name__}"
                    raise RunStop(reason, hard_error=True) from None
        d.tiers.charge(resp.usage)
        self._emit(
            EventKind.MODEL_RESULT,
            {
                "model_id": resp.model_id,
                "tool_calls": [c.name for c in resp.tool_calls],
                "has_text": bool(resp.text.strip()),
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
            op,
        )
        return resp

    def _ask(
        self, messages: list[BaseMessage], base_tools: list[Any]
    ) -> tuple[ModelResponse, list[BaseMessage]]:
        """One model turn. A stronger-model request is answered and the question repeated."""
        msgs = list(messages)
        while True:
            resp = self._call_model(msgs, base_tools)
            msgs.append(_ai_message(resp))
            esc = next((c for c in resp.tool_calls if c.name == ESCALATION_TOOL_NAME), None)
            if esc is None:
                return resp, msgs
            granted = self.d.tiers.request_escalation(str(esc.args.get("reason", "")), "model")
            answer = "switched to a stronger model" if granted else "not available"
            for i, call in enumerate(resp.tool_calls):
                text = answer if call is esc else "skipped: re-asking after escalation"
                msgs.append(ToolMessage(content=text, tool_call_id=_call_id(call, i)))

    def _record(self, *, ok: bool, verified: bool, error_kind: str | None) -> StepRecord:
        action = self.s.action
        assert action is not None
        return StepRecord(
            action_id=action.action_id,
            tool=action.tool,
            args=dict(action.args),
            risk=action.risk_class,
            decision_reason=self.s.reason,
            ok=ok,
            verified=verified,
            tainted=ProvenanceFlag.UNTRUSTED_CONTENT in action.provenance,
            error_kind=error_kind,
            observation_id=self.s.obs_id,
            source=self.s.source,
        )

    def _assess(self, state: AgentState, ok: bool) -> Update:
        self.d.tiers.record_assessment(ok)
        return {"consecutive_failures": 0 if ok else state.get("consecutive_failures", 0) + 1}

    def _after(self, state: AgentState, upd: Update, good: bool, why: str) -> Update:
        """Choose the next node after a finished step, by where the action came from."""
        source = self.s.source
        if source == "model":
            upd["next_node"] = "act"
            self._stop_if_looping(upd)
        elif source == "fast_path":
            upd.update(
                next_node="verify",
                finish_success=good,
                stop_reason="fast_path" if good else "fast_path_failed",
            )
        elif good:
            upd["next_node"] = "replay"
        else:
            messages = upd.get("messages", state["messages"])
            steps = [StepRecord.model_validate(s) for s in upd.get("steps", state["steps"])]
            upd.update(self._abandon_replay(messages, steps, why))
        return upd

    def _abandon_replay(
        self, messages: list[BaseMessage], steps: Sequence[StepRecord], why: str
    ) -> Update:
        recipe = self.s.recipe
        key = recipe.key if recipe else ""
        if self.d.cache is not None and recipe is not None:
            self.d.cache.invalidate(key, why)
        self._emit(EventKind.CACHE_INVALIDATED, {"key": key[:12], "reason": why[:200]})
        self.s.recipe = None
        done = [f"{s.tool}:{'ok' if s.ok else 'failed'}" for s in steps]
        note = (
            "Note: a saved procedure for this goal was tried and did not work "
            f"({why[:100]}). Steps already tried: {', '.join(done) or 'none'}. "
            "Check the current state and continue from there."
        )
        return {
            "messages": [*messages, HumanMessage(content=note)],
            "cache_status": "invalidated",
            "next_node": "plan",
        }

    def _denied(
        self,
        state: AgentState,
        reason: str,
        detail: str,
        *,
        error_kind: str = "denied",
        user: bool = False,
    ) -> Update:
        self.s.reason = reason
        self.s.obs_id = None
        rec = self._record(ok=False, verified=False, error_kind=error_kind)
        messages = list(state["messages"])
        text = (
            "DENIED by the user. Nothing was executed." if user else render_denial(reason, detail)
        )
        if self.s.call_id:
            messages.append(ToolMessage(content=text, tool_call_id=self.s.call_id))
        denials = state.get("consecutive_denials", 0) + 1
        user_denials = state.get("consecutive_user_denials", 0) + (1 if user else 0)
        upd: Update = {
            "messages": messages,
            "steps": [*state.get("steps", []), rec.model_dump(mode="json")],
            "consecutive_denials": denials,
            "consecutive_user_denials": user_denials,
            **self._assess(state, False),
        }
        stop = None
        if user and (self.s.source != "model" or user_denials >= MAX_USER_DENIALS):
            stop = "approval_denied"
        elif denials >= MAX_CONSECUTIVE_DENIALS:
            stop = "repeated_denials"
        if stop:
            upd.update(next_node="verify", stop_reason=stop)
            return upd
        return self._after(state, upd, False, f"denied: {reason}")

    # -- nodes: start ----------------------------------------------------------

    @_guarded
    def route(self, state: AgentState) -> Update:
        decision = route_goal(state["goal"], self.d.settings, state["run_id"])
        if decision.kind is RouteKind.FAST_PATH and decision.action is not None:
            self._begin(decision.action, "fast_path", None)
            return {"route": "fast_path", "cache_status": "skipped", "next_node": "fast_path"}
        return {"route": "model", "next_node": "cache_lookup"}

    @_guarded
    def fast_path(self, state: AgentState) -> Update:
        self.d.budgets.charge_step()
        return {"next_node": "gate"}

    @_guarded
    def cache_lookup(self, state: AgentState) -> Update:
        cache = self.d.cache
        if cache is None:
            return {"cache_status": "disabled", "next_node": "plan"}
        context = context_fingerprint(self.d.settings, self.d.registry.version())
        recipe = cache.lookup(state["goal"], context)
        if recipe is None:
            self._emit(EventKind.CACHE_MISS, {})
            return {"cache_status": "miss", "next_node": "plan"}
        self._emit(EventKind.CACHE_HIT, {"key": recipe.key[:12], "steps": len(recipe.steps)})
        self.s.recipe = recipe
        self.s.replay_i = 0
        return {"cache_key": recipe.key, "next_node": "replay"}

    @_guarded
    def replay(self, state: AgentState) -> Update:
        recipe = self.s.recipe
        assert recipe is not None
        if self.s.replay_i >= len(recipe.steps):
            if self.d.cache is not None:
                self.d.cache.mark_used(recipe.key)
            return {
                "cache_status": "hit",
                "finish_success": True,
                "stop_reason": "cache_replay",
                "next_node": "verify",
            }
        step = recipe.steps[self.s.replay_i]
        self.d.budgets.charge_step()
        try:
            action = self.d.registry.build_action(
                state["run_id"],
                step.tool,
                step.args,
                frozenset({ProvenanceFlag.CACHE_REPLAY}),
                _action_summary(step.tool, step.args),
            )
        except (ToolError, ValueError):
            why = f"step {self.s.replay_i + 1} no longer valid"
            return self._abandon_replay(state["messages"], steps_of(state), why)
        self._begin(action, "cache", None)
        self.s.replay_i += 1
        return {"next_node": "gate"}

    @_guarded
    def plan(self, state: AgentState) -> Update:
        resp, msgs = self._ask(state["messages"], [plan_tool()])
        items = [PlanItem(text=state["goal"][:200])]
        for i, call in enumerate(resp.tool_calls):
            text = "not available during planning; use submit_plan"
            if call.name == TOOL_SUBMIT_PLAN:
                parsed = _parse_plan(call.args)
                text = "plan accepted" if parsed else "invalid plan; the goal is the plan"
                items = parsed or items
            msgs.append(ToolMessage(content=text, tool_call_id=_call_id(call, i)))
        return {"plan": [p.model_dump() for p in items], "messages": msgs, "next_node": "act"}

    # -- nodes: act ------------------------------------------------------------

    @_guarded
    def act(self, state: AgentState) -> Update:
        check = self.d.budgets.check_step()
        if not check.ok:
            raise RunStop(check.stop_reason or "budget_exhausted:steps")
        base = [*self.d.registry.model_tools(), *act_control_tools()]
        resp, msgs = self._ask(state["messages"], base)
        if not resp.tool_calls:
            return self._text_only(state, resp, msgs)
        first = resp.tool_calls[0]
        for i, call in enumerate(resp.tool_calls[1:], start=1):
            skipped = "skipped: one action per step"
            msgs.append(ToolMessage(content=skipped, tool_call_id=_call_id(call, i)))
        call_id = _call_id(first, 0)
        if first.name == TOOL_FINISH:
            return self._finish(first, call_id, msgs)
        if first.name in (TOOL_UPDATE_PLAN, TOOL_SUBMIT_PLAN):
            parsed = _parse_plan(first.args)
            text = "plan updated" if parsed else "invalid plan"
            msgs.append(ToolMessage(content=text, tool_call_id=call_id))
            upd: Update = {"messages": msgs, "reminded": False, "next_node": "act"}
            if parsed:
                upd["plan"] = [p.model_dump() for p in parsed]
            return upd
        return self._propose(state, first, call_id, msgs)

    @staticmethod
    def _text_only(state: AgentState, resp: ModelResponse, msgs: list[BaseMessage]) -> Update:
        if not state.get("reminded", False):
            msgs.append(HumanMessage(content=REMINDER))
            return {"messages": msgs, "reminded": True, "next_node": "act"}
        return {
            "messages": msgs,
            "finish_success": False,
            "final_text": resp.text.strip()[:2000],
            "stop_reason": "no_tool_call",
            "next_node": "verify",
        }

    @staticmethod
    def _finish(call: ToolCallRequest, call_id: str, msgs: list[BaseMessage]) -> Update:
        try:
            args = FinishArgs.model_validate(call.args)
        except ValidationError:
            msgs.append(ToolMessage(content="invalid finish arguments", tool_call_id=call_id))
            return {"messages": msgs, "next_node": "act"}
        msgs.append(ToolMessage(content="finished", tool_call_id=call_id))
        return {
            "messages": msgs,
            "finish_success": args.success,
            "final_text": args.summary,
            "stop_reason": "finished",
            "next_node": "verify",
        }

    def _propose(
        self, state: AgentState, call: ToolCallRequest, call_id: str, msgs: list[BaseMessage]
    ) -> Update:
        provenance = (
            frozenset({ProvenanceFlag.UNTRUSTED_CONTENT})
            if state.get("untrusted_seen", False)
            else frozenset()
        )
        try:
            tool = self.d.registry.to_tool_name(call.name)
            action = self.d.registry.build_action(
                state["run_id"], tool, call.args, provenance, _action_summary(tool, call.args)
            )
        except (ToolError, ValueError) as exc:
            text = f"Invalid call: {exc}"[:300]
            msgs.append(ToolMessage(content=text, tool_call_id=call_id))
            upd: Update = {"messages": msgs, "next_node": "act", **self._assess(state, False)}
            return self._stop_if_looping(upd)
        key = recovery.action_key(action.tool, action.canonical_args())
        ledger = state.get("failures", {})
        if recovery.is_blocked(ledger, key, self._retries):
            self._emit_recovery(
                "repeated_failure", action.tool, recovery.failure_count(ledger, key)
            )
            raise RunStop(recovery.REPEATED_FAILURE)
        self.d.budgets.charge_step()
        self._begin(action, "model", call_id)
        return {"messages": msgs, "reminded": False, "next_node": "gate"}

    # -- nodes: gate, approval, execute ----------------------------------------

    @_guarded
    def gate(self, state: AgentState) -> Update:
        action = self.s.action
        assert action is not None
        self._emit(
            EventKind.TOOL_CALL,
            {
                "action_id": action.action_id,
                "tool": action.tool,
                "args": dict(action.args),
                "risk": action.risk_class.value,
                "source": self.s.source,
                "attempt": self.s.attempt,
            },
            self.s.op_id,
        )
        decision = self.d.gate.evaluate(action, operation_id=self.s.op_id)
        self.s.decision = decision
        self.s.reason = decision.reason.value
        if decision.verdict is Verdict.DENY:
            if decision.reason is ReasonCode.BUDGET_EXHAUSTED:
                raise RunStop(decision.detail or "budget_exhausted")
            return self._denied(state, decision.reason.value, decision.detail)
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            return {"next_node": "approval_request"}
        return {"next_node": "execute"}

    @_guarded
    def approval_request(self, state: AgentState) -> Update:
        action, decision = self.s.action, self.s.decision
        assert action is not None and decision is not None
        try:
            request = self.d.gate.request_approval(action, decision)
        except ValueError:
            return self._denied(state, "approval_unavailable", "approval could not be requested")
        self.s.approval_id = request.approval_id
        return {"next_node": "approval_wait"}

    @_guarded
    def approval_wait(self, state: AgentState) -> Update:
        approval_id = self.s.approval_id
        assert approval_id is not None
        approved = interrupt({"approval_id": approval_id}) is True
        self.d.approvals.resolve_text(approval_id, approved)
        self._emit(
            EventKind.APPROVAL_RESOLVED,
            {"approval_id": approval_id, "stage": "user_decision", "approved": approved},
            self.s.op_id,
        )
        if approved:
            return {"next_node": "execute"}
        return self._denied(state, "approval_denied", "", error_kind="approval_denied", user=True)

    @_guarded
    def execute(self, state: AgentState) -> Update:
        action = self.s.action
        assert action is not None
        approval_id, self.s.approval_id = self.s.approval_id, None
        try:
            permit = self.d.gate.authorize(
                action, approval_id=approval_id, operation_id=self.s.op_id
            )
        except PolicyDenied as exc:
            reason = exc.decision.reason
            if reason is ReasonCode.BUDGET_EXHAUSTED:
                raise RunStop(exc.decision.detail or "budget_exhausted") from None
            return self._denied(state, reason.value, str(exc)[:300])
        self.s.reason = permit.decision.reason.value
        try:
            self.s.outcome = self.d.registry.dispatch(action, permit, self.d.ctx)
        except Exception:  # dispatch maps tool errors itself; this is the last line of defence
            info = ToolErrorInfo(kind="internal", message="tool execution failed", retryable=False)
            self.s.outcome = ToolOutcome(tool=action.tool, ok=False, error=info)
        return {"consecutive_denials": 0, "consecutive_user_denials": 0, "next_node": "observe"}

    # -- nodes: observe, assess, verify, report --------------------------------

    @_guarded
    def observe(self, state: AgentState) -> Update:
        action, outcome = self.s.action, self.s.outcome
        assert action is not None and outcome is not None
        self.s.obs_n += 1
        self.s.obs_id = observation_id(self.s.obs_n)
        self.s.obs_text = render_observation(outcome, self.s.obs_id)
        rec = self._record(
            ok=outcome.ok,
            verified=step_verified(outcome, action.risk_class),
            error_kind=outcome.error.kind if outcome.error else None,
        )
        self._emit(
            EventKind.TOOL_RESULT,
            {
                "tool": action.tool,
                "ok": outcome.ok,
                "verified": rec.verified,
                "error_kind": rec.error_kind,
                "truncated": outcome.truncated,
            },
            self.s.op_id,
        )
        self._emit(
            EventKind.OBSERVATION,
            {
                "observation_id": self.s.obs_id,
                "untrusted": outcome.untrusted,
                "chars": len(self.s.obs_text),
            },
            self.s.op_id,
        )
        key = recovery.action_key(action.tool, action.canonical_args())
        ledger = state.get("failures", {})
        if rec.ok:
            ledger = recovery.note_success(ledger, key)
        else:
            ledger = recovery.note_failure(ledger, key, rec.error_kind or "unknown")
        return {
            "steps": [*state.get("steps", []), rec.model_dump(mode="json")],
            "failures": ledger,
            "untrusted_seen": state.get("untrusted_seen", False) or outcome.untrusted,
            "next_node": "assess",
        }

    @_guarded
    def assess(self, state: AgentState) -> Update:
        action, outcome = self.s.action, self.s.outcome
        assert action is not None and outcome is not None
        retry_key = f"{action.tool}:{action.fingerprint()[:16]}"
        idempotent = action.risk_class is RiskClass.READ  # modify/dangerous go back to the model
        if (
            idempotent
            and self.s.attempt == 0
            and recovery.retry_tool(outcome, self.d.budgets, retry_key).retry
        ):
            self.s.attempt += 1
            return {"next_node": "gate"}
        rec = steps_of(state)[-1]
        messages = list(state["messages"])
        if self.s.call_id:
            messages.append(ToolMessage(content=self.s.obs_text, tool_call_id=self.s.call_id))
        key = recovery.action_key(action.tool, action.canonical_args())
        if self.s.source == "model" and recovery.is_blocked(
            ledger := state.get("failures", {}), key, self._retries
        ):
            note = recovery.block_note(ledger, key)
            messages.append(HumanMessage(content=note))
            self._emit_recovery("action_blocked", action.tool, recovery.failure_count(ledger, key))
        upd: Update = {"messages": messages, **self._assess(state, rec.ok)}
        why = f"step {action.tool} " + ("failed" if not rec.ok else "not verified")
        return self._after(state, upd, rec.ok and rec.verified, why)

    @staticmethod
    def verify(state: AgentState) -> Update:
        outcome = decide_outcome(
            steps_of(state), state.get("finish_success"), state.get("hard_error", False)
        )
        return {
            "outcome": outcome.value,
            "stop_reason": state.get("stop_reason") or "finished",
            "next_node": "report",
        }

    def report(self, state: AgentState) -> Update:
        d = self.d
        steps = steps_of(state)
        outcome = Outcome(state["outcome"])
        stop_reason = state["stop_reason"]
        cache_status = self._maybe_store(state, steps, outcome)
        text = state.get("final_text") or default_summary(
            outcome, stop_reason, steps, d.settings.language
        )
        used = d.budgets.snapshot()["used"]
        d.trace.finish(
            outcome,
            stop_reason,
            steps=len(steps),
            model_calls=used["model_calls"],
            tokens=used["tokens"],
            cache=cache_status,
            tier=d.tiers.tier.value,
            failures=state.get("failures", {}),
        )
        upd: Update = {"final_text": text, "cache_status": cache_status}
        if outcome is Outcome.COMPLETED:
            done = [PlanItem(text=p.text, status="done") for p in plan_of(state)]
            upd["plan"] = [p.model_dump() for p in done]
        return upd

    def _maybe_store(self, state: AgentState, steps: list[StepRecord], outcome: Outcome) -> str:
        """Store a recipe only for a completed, model-driven run; cache.store re-checks safety."""
        status = state.get("cache_status", "disabled")
        cache = self.d.cache
        if (
            cache is None
            or state.get("route") != "model"
            or outcome is not Outcome.COMPLETED
            or not steps
            or any(s.source != "model" for s in steps)
            # Content was read: the answer depends on data a replay would not show again.
            or state.get("untrusted_seen", False)
        ):
            return status
        candidates = [
            CandidateStep(
                tool=s.tool,
                args=s.args,
                risk_class=s.risk,
                ok=s.ok,
                verified=s.verified,
                tainted=s.tainted,
            )
            for s in steps
        ]
        context = context_fingerprint(self.d.settings, self.d.registry.version())
        recipe = cache.store(state["goal"], context, candidates)
        if recipe is None:
            return status
        self._emit(EventKind.CACHE_STORED, {"key": recipe.key[:12], "steps": len(recipe.steps)})
        return "stored"


def initial_state(deps: RunDeps, goal: str) -> AgentState:
    return {
        "run_id": deps.run_id,
        "goal": goal,
        "plan": [],
        "messages": [
            SystemMessage(content=system_prompt(deps.settings.language)),
            HumanMessage(content=goal),
        ],
        "steps": [],
        "untrusted_seen": False,
        "consecutive_failures": 0,
        "failures": {},
        "consecutive_denials": 0,
        "consecutive_user_denials": 0,
        "reminded": False,
        "cache_key": None,
        "cache_status": "disabled" if deps.cache is None else "miss",
        "finish_success": None,
        "hard_error": False,
        "next_node": "route",
    }


def _next_node(state: AgentState) -> str:
    return state["next_node"]


def build_graph(deps: RunDeps) -> CompiledStateGraph:
    nodes = AgentNodes(deps)
    graph = StateGraph(AgentState)
    for name in (*_NODES, "verify", "report"):
        graph.add_node(name, getattr(nodes, name))
    graph.set_entry_point("route")
    targets = [*_NODES, "verify"]
    for name in _NODES:
        graph.add_conditional_edges(name, _next_node, targets)
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=InMemorySaver())
