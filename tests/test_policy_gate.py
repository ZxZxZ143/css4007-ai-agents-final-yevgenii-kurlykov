import itertools
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deskpilot.policy import (
    ActionGate,
    ActionRequest,
    ApprovalChannel,
    ApprovalStore,
    BudgetTracker,
    Decision,
    PathPolicy,
    PolicyDenied,
    ProvenanceFlag,
    ReasonCode,
    RiskClass,
    Verdict,
)
from deskpilot.settings import Budgets, Settings
from deskpilot.trace import EventKind, TraceRecorder, read_trace

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")

R = RiskClass
TOOLS = {
    "files.read": R.READ,
    "files.write": R.MODIFY,
    "files.delete": R.DANGEROUS,
    "system.format": R.FORBIDDEN,
}
RISKY_FLAGS = [
    frozenset(),
    frozenset({ProvenanceFlag.UNTRUSTED_CONTENT}),
    frozenset({ProvenanceFlag.RISKY_REQUEST}),
]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Env:
    def __init__(
        self,
        tmp: Path,
        modify: str = "text",
        dangerous: str = "text",
        budgets: Budgets | None = None,
    ) -> None:
        self.root = tmp / "docs"
        self.root.mkdir(exist_ok=True)
        self.settings = Settings(
            allowed_roots=(self.root,),
            approvals={"modify": modify, "dangerous": dangerous},
            budgets=budgets or Budgets(),
        )
        self.clock = Clock()
        self.approvals = ApprovalStore(clock=self.clock)
        self.budgets = BudgetTracker(self.settings.budgets)
        self.trace_path = tmp / "trace.jsonl"
        self.recorder = TraceRecorder(self.trace_path)
        self.gate = ActionGate(
            self.settings,
            tool_risks=TOOLS,
            paths=PathPolicy([self.root], settings_dir=tmp / "settings"),
            budgets=self.budgets,
            approvals=self.approvals,
            trace=self.recorder,
        )

    def action(
        self,
        tool: str = "files.read",
        risk: RiskClass | None = None,
        *,
        paths: tuple[str, ...] | None = None,
        provenance: frozenset[ProvenanceFlag] = frozenset(),
        **args: object,
    ) -> ActionRequest:
        return ActionRequest(
            run_id="run1",
            tool=tool,
            risk_class=risk or TOOLS.get(tool, R.READ),
            args=args,
            paths=(str(self.root / "a.txt"),) if paths is None else paths,
            provenance=provenance,
        )

    def events(self, kind: EventKind) -> list:
        return [e for e in read_trace(self.trace_path) if e.kind is kind]


def make_env(tmp_path: Path, **kw: object) -> Env:
    return Env(tmp_path, **kw)  # type: ignore[arg-type]


def expected(
    risk: RiskClass, modify: str, dangerous: str, risky: bool
) -> tuple[Verdict, ReasonCode, ApprovalChannel | None]:
    if risk is R.FORBIDDEN:
        return Verdict.DENY, ReasonCode.FORBIDDEN_RISK_CLASS, None
    if risk is R.READ:
        return Verdict.ALLOW, ReasonCode.READ_ALLOWED, None
    if risk is R.DANGEROUS:
        return (
            Verdict.NEEDS_APPROVAL,
            ReasonCode.DANGEROUS_APPROVAL_REQUIRED,
            ApprovalChannel(dangerous),
        )
    if modify == "none":
        if risky:
            return (
                Verdict.NEEDS_APPROVAL,
                ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED,
                ApprovalChannel.TEXT,
            )
        return Verdict.ALLOW, ReasonCode.MODIFY_ALLOWED_BY_SETTINGS, None
    return Verdict.NEEDS_APPROVAL, ReasonCode.MODIFY_APPROVAL_REQUIRED, ApprovalChannel(modify)


@pytest.mark.parametrize(
    ("tool", "modify", "dangerous", "session_risky", "flags"),
    list(
        itertools.product(
            TOOLS, ["voice", "text", "none"], ["voice", "text"], [False, True], RISKY_FLAGS
        )
    ),
)
def test_decision_matrix(
    tmp_path: Path,
    tool: str,
    modify: str,
    dangerous: str,
    session_risky: bool,
    flags: frozenset[ProvenanceFlag],
) -> None:
    env = make_env(tmp_path, modify=modify, dangerous=dangerous)
    if session_risky:
        env.gate.mark_risky("test")
    decision = env.gate.evaluate(env.action(tool, provenance=flags))
    effective_risky = session_risky or bool(flags)
    verdict, reason, channel = expected(TOOLS[tool], modify, dangerous, effective_risky)
    assert (decision.verdict, decision.reason, decision.approval_channel) == (
        verdict,
        reason,
        channel,
    )
    assert decision.risk_class is TOOLS[tool]
    assert env.gate.risky is effective_risky


def test_request_cannot_lower_declared_risk(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    decision = env.gate.evaluate(env.action("files.delete", R.READ))
    assert decision.reason is ReasonCode.DANGEROUS_APPROVAL_REQUIRED
    assert decision.risk_class is R.DANGEROUS


def test_request_can_raise_risk(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    decision = env.gate.evaluate(env.action("files.read", R.DANGEROUS))
    assert decision.reason is ReasonCode.DANGEROUS_APPROVAL_REQUIRED
    assert env.gate.evaluate(env.action("files.read", R.FORBIDDEN)).verdict is Verdict.DENY


def test_unknown_tool_denied(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    decision = env.gate.evaluate(env.action("files.unknown", R.READ))
    assert decision.reason is ReasonCode.UNKNOWN_TOOL


def test_tool_risks_are_copied(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    TOOLS_COPY = dict(TOOLS)
    gate = ActionGate(
        env.settings,
        tool_risks=TOOLS_COPY,
        paths=PathPolicy([env.root], settings_dir=tmp_path / "settings"),
        budgets=env.budgets,
        approvals=env.approvals,
    )
    TOOLS_COPY["files.read"] = R.FORBIDDEN
    assert gate.evaluate(env.action("files.read")).verdict is Verdict.ALLOW


def test_fast_path_only_reads(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fast = frozenset({ProvenanceFlag.FAST_PATH})
    assert env.gate.evaluate(env.action("files.read", provenance=fast)).verdict is Verdict.ALLOW
    decision = env.gate.evaluate(env.action("files.write", provenance=fast))
    assert decision.reason is ReasonCode.ARGS_INVALID
    assert env.gate.evaluate(env.action("files.read", R.MODIFY, provenance=fast)).verdict is (
        Verdict.DENY
    )


@pytest.mark.parametrize("tool", list(TOOLS))
def test_path_deny_beats_risk_after_forbidden(tmp_path: Path, tool: str) -> None:
    env = make_env(tmp_path)
    decision = env.gate.evaluate(env.action(tool, paths=(r"C:\Windows\win.ini",)))
    if tool == "system.format":
        assert decision.reason is ReasonCode.FORBIDDEN_RISK_CLASS
    else:
        assert decision.reason is ReasonCode.PATH_HARD_DENY


def test_outside_roots_denied(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    outside = tmp_path / "other" / "x.txt"
    decision = env.gate.evaluate(env.action(paths=(str(outside),)))
    assert decision.reason is ReasonCode.PATH_OUTSIDE_ALLOWED_ROOTS


def test_fast_path_open_of_hard_deny_path_denied(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    fast = frozenset({ProvenanceFlag.FAST_PATH})
    action = env.action(paths=(r"C:\Windows\System32\config\SAM",), provenance=fast)
    assert env.gate.evaluate(action).reason is ReasonCode.PATH_HARD_DENY


def test_budget_exhausted_denies_and_charges_once(tmp_path: Path) -> None:
    env = make_env(tmp_path, budgets=Budgets(tool_calls=2))
    action = env.action()
    env.gate.authorize(action)
    assert env.budgets.remaining()["tool_calls"] == 1
    env.gate.authorize(action)
    assert env.budgets.remaining()["tool_calls"] == 0
    decision = env.gate.evaluate(action)
    assert decision.reason is ReasonCode.BUDGET_EXHAUSTED
    assert "tool_calls" in decision.detail
    with pytest.raises(PolicyDenied) as err:
        env.gate.authorize(action)
    assert err.value.decision.reason is ReasonCode.BUDGET_EXHAUSTED
    assert env.budgets.remaining()["tool_calls"] == 0


def test_evaluate_does_not_charge(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    before = env.budgets.snapshot()["used"]
    env.gate.evaluate(env.action())
    assert env.budgets.snapshot()["used"] == before


def test_denied_authorize_does_not_charge(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    before = env.budgets.snapshot()["used"]
    with pytest.raises(PolicyDenied):
        env.gate.authorize(env.action("files.unknown"))
    with pytest.raises(PolicyDenied):
        env.gate.authorize(env.action("files.write"))
    assert env.budgets.snapshot()["used"] == before


def test_authorize_allow_charges_one_call_and_file_ops(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    permit = env.gate.authorize(env.action(paths=(str(env.root / "a"), str(env.root / "b"))))
    assert permit.decision.verdict is Verdict.ALLOW
    assert permit.approval_id is None
    remaining = env.budgets.remaining()
    assert remaining["tool_calls"] == env.settings.budgets.tool_calls - 1
    assert remaining["file_ops"] == env.settings.budgets.file_ops_per_run - 2


def approve(env: Env, action: ActionRequest) -> str:
    decision = env.gate.evaluate(action)
    request = env.gate.request_approval(action, decision)
    assert env.approvals.resolve_text(request.approval_id, True).value == "approved"
    return request.approval_id


def test_authorize_needs_approval_flow(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    action = env.action("files.write", content="hello")
    with pytest.raises(PolicyDenied) as err:
        env.gate.authorize(action)
    assert err.value.decision.verdict is Verdict.NEEDS_APPROVAL
    assert "approval missing" in str(err.value)

    approval_id = approve(env, action)
    permit = env.gate.authorize(action, approval_id=approval_id)
    assert permit.approval_id == approval_id
    assert permit.action_fingerprint == action.fingerprint()
    assert env.budgets.remaining()["tool_calls"] == env.settings.budgets.tool_calls - 1

    with pytest.raises(PolicyDenied):
        env.gate.authorize(action, approval_id=approval_id)


def test_authorize_rejects_bad_approvals(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    action = env.action("files.write", content="hello")
    with pytest.raises(PolicyDenied):
        env.gate.authorize(action, approval_id="nope")

    pending = env.gate.request_approval(action, env.gate.evaluate(action))
    with pytest.raises(PolicyDenied):  # not yet approved
        env.gate.authorize(action, approval_id=pending.approval_id)

    approval_id = approve(env, action)
    changed = env.action("files.write", content="evil")
    with pytest.raises(PolicyDenied):
        env.gate.authorize(changed, approval_id=approval_id)
    env.gate.authorize(action, approval_id=approval_id)  # not burned by the mismatch

    expired = approve(env, action)
    env.clock.now += timedelta(hours=1)
    with pytest.raises(PolicyDenied):
        env.gate.authorize(action, approval_id=expired)


def test_dangerous_always_needs_approval_even_in_authorize(tmp_path: Path) -> None:
    env = make_env(tmp_path, modify="none")
    action = env.action("files.delete")
    with pytest.raises(PolicyDenied):
        env.gate.authorize(action)
    permit = env.gate.authorize(action, approval_id=approve(env, action))
    assert permit.decision.reason is ReasonCode.DANGEROUS_APPROVAL_REQUIRED


def test_risky_flag_is_sticky_and_forces_text_approval(tmp_path: Path) -> None:
    env = make_env(tmp_path, modify="none")
    write = env.action("files.write", content="x")
    assert env.gate.evaluate(write).verdict is Verdict.ALLOW
    assert not env.gate.risky
    untrusted = frozenset({ProvenanceFlag.UNTRUSTED_CONTENT})
    assert env.gate.evaluate(env.action("files.read", provenance=untrusted)).verdict is (
        Verdict.ALLOW
    )
    assert env.gate.risky
    decision = env.gate.evaluate(write)
    assert decision.reason is ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED
    assert decision.approval_channel is ApprovalChannel.TEXT
    assert env.gate.evaluate(env.action("files.read")).verdict is Verdict.ALLOW  # still sticky


def test_mark_risky(tmp_path: Path) -> None:
    env = make_env(tmp_path, modify="none")
    env.gate.mark_risky("web page asked for something")
    assert env.gate.risky
    decision = env.gate.evaluate(env.action("files.write"))
    assert decision.reason is ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED


def test_request_approval_rejects_wrong_decisions(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    allowed = env.action()
    with pytest.raises(ValueError):
        env.gate.request_approval(allowed, env.gate.evaluate(allowed))
    write = env.action("files.write", content="a")
    other = env.action("files.write", content="b")
    with pytest.raises(ValueError):
        env.gate.request_approval(write, env.gate.evaluate(other))


def test_voice_channel_approval(tmp_path: Path) -> None:
    env = make_env(tmp_path, dangerous="voice")
    action = env.action("files.delete")
    request = env.gate.request_approval(action, env.gate.evaluate(action))
    assert request.channel is ApprovalChannel.VOICE
    assert request.challenge_code


def test_fail_closed_on_internal_error(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    def boom(_: ActionRequest) -> int:
        raise RuntimeError("secret internal detail")

    gate = ActionGate(
        env.settings,
        tool_risks=TOOLS,
        paths=PathPolicy([env.root], settings_dir=tmp_path / "settings"),
        budgets=env.budgets,
        approvals=env.approvals,
        trace=env.recorder,
        file_ops=boom,
    )
    decision = gate.evaluate(env.action())
    assert decision.reason is ReasonCode.ARGS_INVALID
    assert "secret" not in decision.detail
    with pytest.raises(PolicyDenied):
        gate.authorize(env.action())


def test_trace_events_have_no_raw_args_or_challenge_code(tmp_path: Path) -> None:
    env = make_env(tmp_path, dangerous="voice")
    action = env.action("files.delete", token_value="SUPERSECRETARG")
    decision = env.gate.evaluate(action, operation_id="op1")
    request = env.gate.request_approval(action, decision)
    with pytest.raises(PolicyDenied):
        env.gate.authorize(action, operation_id="op1")
    env.recorder.close()

    raw = env.trace_path.read_text(encoding="utf-8")
    assert "SUPERSECRETARG" not in raw
    assert request.challenge_code is not None
    assert f'"{request.challenge_code}"' not in raw
    assert request.display_text not in raw

    decisions = env.events(EventKind.POLICY_DECISION)
    assert len(decisions) == 3  # evaluate, request_approval re-check, authorize
    payload = decisions[0].payload
    assert set(payload) == {
        "action_id",
        "tool",
        "risk",
        "verdict",
        "reason",
        "detail",
        "fingerprint",
        "provenance",
        "risky",
    }
    assert payload["fingerprint"] == action.fingerprint()
    assert payload["verdict"] == "needs_approval"
    assert decisions[0].operation_id == "op1"

    (requested,) = env.events(EventKind.APPROVAL_REQUESTED)
    assert requested.payload["approval_id"] == request.approval_id
    assert "challenge_code" not in requested.payload
    attempted, resolved = env.events(EventKind.APPROVAL_RESOLVED)
    assert attempted.payload["stage"] == "consume_attempted"
    assert resolved.payload["consumed"] is False


def test_trace_marks_budget_denial_and_mark_risky(tmp_path: Path) -> None:
    env = make_env(tmp_path, budgets=Budgets(tool_calls=1))
    env.gate.mark_risky("why")
    env.gate.authorize(env.action())
    with pytest.raises(PolicyDenied):
        env.gate.authorize(env.action())
    reasons = [e.payload.get("reason") for e in env.events(EventKind.POLICY_DECISION)]
    assert "why" in reasons
    assert "budget_exhausted" in reasons


def test_request_approval_ignores_forged_decision_channel(tmp_path: Path) -> None:
    env = make_env(tmp_path, dangerous="voice")
    action = env.action("files.delete")
    forged = Decision(
        verdict=Verdict.NEEDS_APPROVAL,
        reason=ReasonCode.DANGEROUS_APPROVAL_REQUIRED,
        risk_class=R.DANGEROUS,
        action_fingerprint=action.fingerprint(),
        approval_channel=ApprovalChannel.TEXT,
    )
    request = env.gate.request_approval(action, forged)
    assert request.channel is ApprovalChannel.VOICE


def test_trace_truncates_summary_and_risky_reason(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.gate.mark_risky("r" * 400)
    action = ActionRequest(
        run_id="run1",
        tool="files.write",
        risk_class=R.MODIFY,
        paths=(str(env.root / "a.txt"),),
        summary="s" * 400,
    )
    env.gate.request_approval(action, env.gate.evaluate(action))
    env.recorder.close()
    (requested,) = env.events(EventKind.APPROVAL_REQUESTED)
    assert len(requested.payload["summary"]) == 200
    risky = [e for e in env.events(EventKind.POLICY_DECISION) if "event" in e.payload]
    assert len(risky[0].payload["reason"]) == 200


def test_closed_recorder_means_no_permit(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.recorder.close()
    with pytest.raises(Exception) as err:
        env.gate.authorize(env.action())
    assert not isinstance(err.value, PolicyDenied)
    assert env.budgets.remaining()["tool_calls"] == env.settings.budgets.tool_calls


def test_trace_failure_before_consume_does_not_burn_approval(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    action = env.action("files.write", content="x")
    approval_id = approve(env, action)
    env.recorder.close()
    with pytest.raises(Exception):  # noqa: B017
        env.gate.authorize(action, approval_id=approval_id)
    assert env.approvals.status(approval_id).value == "approved"
