"""Run one goal: start the trace, drive the graph, answer approval interrupts, build the result."""

from __future__ import annotations

import logging
from typing import Any, Literal

from langgraph.types import Command

from deskpilot.agent.deps import RunDeps
from deskpilot.agent.graph import build_graph, initial_state
from deskpilot.agent.state import AgentState, RunResult, TierInfo, steps_of
from deskpilot.agent.verify import build_report, default_summary
from deskpilot.prefilter import PrefilterDecision, evaluate
from deskpilot.trace import EventKind, Outcome

log = logging.getLogger(__name__)

_RECURSION_PER_STEP = 10
_RECURSION_BASE = 60
PREFILTER_REFUSED = "prefilter_refused"


def report_for(result: RunResult, language: Literal["ru", "en"]) -> str:
    """User-facing report for a result (RunResult has no report field; the CLI calls this)."""
    try:
        return build_report(result, language)
    except Exception:  # a report must never break a finished run
        log.warning("could not build the report")
        return result.summary[:2500]


def _approve(deps: RunDeps, payload: dict[str, Any]) -> bool:
    """Ask the approver callback. Only an explicit True approves; any failure denies."""
    try:
        request = deps.approvals.get(str(payload["approval_id"]))
        return deps.approver(request) is True
    except Exception:  # the approver is user code; fail closed
        log.warning("approver failed; treating as denied")
        return False


def _result(deps: RunDeps, state: AgentState) -> RunResult:
    outcome = Outcome(state.get("outcome", Outcome.FAILED.value))
    steps = steps_of(state)
    used = deps.budgets.snapshot()["used"]
    summary = state.get("final_text") or default_summary(
        outcome, state.get("stop_reason", "internal_error"), steps, deps.settings.language
    )
    return RunResult(
        run_id=deps.run_id,
        outcome=outcome,
        stop_reason=state.get("stop_reason", "internal_error"),
        summary=summary,
        steps=tuple(steps),
        model_calls=used["model_calls"],
        tokens=used["tokens"],
        tier=TierInfo(
            start=deps.start_tier, final=deps.tiers.tier.value, escalated=deps.tiers.escalated
        ),
        cache=state.get("cache_status", "disabled"),
        failures=dict(state.get("failures", {})),
        trace_path=str(deps.trace_path),
    )


def _failed(deps: RunDeps, state: AgentState, reason: str) -> RunResult:
    try:
        deps.trace.finish(Outcome.FAILED, reason)
    except Exception:  # already finished or the trace itself is broken
        log.warning("could not finish the trace after an internal error")
    return _result(
        deps,
        {**state, "outcome": Outcome.FAILED.value, "stop_reason": reason},  # type: ignore[typeddict-item]
    )


def _record_decision(deps: RunDeps, decision: PrefilterDecision) -> None:
    """Trace the decision without the goal text (run_started already holds it)."""

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    deps.trace.emit(
        EventKind.PREFILTER_DECISION,
        {
            "verdict": decision.verdict,
            "reasons": list(decision.reasons),
            "lexicon_label": decision.lexicon_label,
            "lexicon_intents": list(decision.lexicon_intents),
            "classifier_label": decision.classifier_label,
            "malicious_score": rounded(decision.malicious_score),
            "risk_score": rounded(decision.risk_score),
        },
    )


_REFUSAL = {
    "ru": "Запрос отклонён фильтром безопасности. Ни одно действие не выполнено.",
    "en": "The request was refused by the safety pre-filter. No action was taken.",
}


def _refused(deps: RunDeps) -> RunResult:
    deps.trace.finish(Outcome.FAILED, PREFILTER_REFUSED)
    result = _result(
        deps,
        {"outcome": Outcome.FAILED.value, "stop_reason": PREFILTER_REFUSED},  # type: ignore[typeddict-item]
    )
    return result.model_copy(update={"summary": _REFUSAL[deps.settings.language]})


def _last_state(graph: Any, config: Any, fallback: AgentState) -> AgentState:
    """The last checkpointed state, so steps and counters survive an unexpected exception."""
    try:
        return dict(graph.get_state(config).values)  # type: ignore[return-value]
    except Exception:
        return fallback


def run_goal(goal: str, deps: RunDeps) -> RunResult:
    """Run `goal` to an explicit outcome. Only KeyboardInterrupt escapes."""
    state: AgentState = {}
    graph: Any = None
    config: Any = None
    try:
        deps.trace.start_run(goal, mode=deps.mode, model_id=deps.settings.model_id)
        decision = evaluate(goal)
        _record_decision(deps, decision)
        if decision.verdict == "refuse":
            return _refused(deps)
        if decision.verdict == "flag":
            deps.gate.mark_risky("prefilter:" + ",".join(decision.reasons))
        graph = build_graph(deps)
        limit = deps.budgets.snapshot()["limits"]["steps"] * _RECURSION_PER_STEP + _RECURSION_BASE
        config = {"configurable": {"thread_id": deps.run_id}, "recursion_limit": limit}
        state = graph.invoke(initial_state(deps, goal), config)
        while state.get("__interrupt__"):  # type: ignore[attr-defined]
            payload = state["__interrupt__"][0].value  # type: ignore[typeddict-item]
            state = graph.invoke(Command(resume=_approve(deps, payload)), config)
        return _result(deps, state)
    except KeyboardInterrupt:
        try:
            deps.trace.finish(Outcome.FAILED, "interrupted")
        except Exception:
            log.warning("could not finish the trace after an interrupt")
        raise
    except Exception as exc:
        log.warning("run failed: %s", type(exc).__name__)
        return _failed(
            deps, _last_state(graph, config, state), f"internal_error:{type(exc).__name__}"
        )
    finally:
        deps.trace.close()
