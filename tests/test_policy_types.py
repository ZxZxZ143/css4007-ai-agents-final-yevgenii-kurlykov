from typing import Any

import pytest
from pydantic import ValidationError

from deskpilot import limits
from deskpilot.policy import (
    REASON_VERDICT,
    ActionRequest,
    ApprovalChannel,
    Decision,
    ProvenanceFlag,
    ReasonCode,
    RiskClass,
    Verdict,
)

FP = "a" * 64


def req(**kw: Any) -> ActionRequest:
    base: dict[str, Any] = {
        "run_id": "r1",
        "tool": "files.move",
        "risk_class": RiskClass.MODIFY,
        "args": {"a": 1},
    }
    return ActionRequest(**{**base, **kw})


def test_reason_verdict_exhaustive_and_immutable() -> None:
    assert set(REASON_VERDICT) == set(ReasonCode)
    for r in ReasonCode:
        assert r.value == r.name.lower()
        assert isinstance(REASON_VERDICT[r], Verdict)
    with pytest.raises(TypeError):
        REASON_VERDICT[ReasonCode.READ_ALLOWED] = Verdict.DENY  # type: ignore[index]


def test_reason_verdict_values() -> None:
    allow = {ReasonCode.READ_ALLOWED, ReasonCode.MODIFY_ALLOWED_BY_SETTINGS}
    approval = {
        ReasonCode.MODIFY_APPROVAL_REQUIRED,
        ReasonCode.DANGEROUS_APPROVAL_REQUIRED,
        ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED,
    }
    for r in ReasonCode:
        expected = (
            Verdict.ALLOW
            if r in allow
            else Verdict.NEEDS_APPROVAL
            if r in approval
            else Verdict.DENY
        )
        assert REASON_VERDICT[r] is expected


def test_risk_classes_cover_limits() -> None:
    values = {c.value for c in RiskClass}
    assert values >= limits.ALWAYS_DENIED_RISK_CLASSES | limits.ALWAYS_APPROVED_RISK_CLASSES


@pytest.mark.parametrize("tool", ["", "Files.move", "files..move", "1files", "files.", "a b"])
def test_bad_tool(tool: str) -> None:
    with pytest.raises(ValidationError):
        req(tool=tool)


def test_bad_run_id_and_extra() -> None:
    with pytest.raises(ValidationError):
        req(run_id="")
    with pytest.raises(ValidationError):
        req(bogus=1)


@pytest.mark.parametrize(
    "args",
    [{"a": {1, 2}}, {"a": b"x"}, {"a": float("nan")}, {1: "x"}, {"a": float("inf")}],
)
def test_non_json_args(args: dict[Any, Any]) -> None:
    with pytest.raises(ValidationError):
        req(args=args)


def test_immutable_and_deep_copy() -> None:
    src: dict[str, Any] = {"a": {"b": [1]}}
    r = req(args=src)
    src["a"]["b"].append(2)
    assert r.args == {"a": {"b": (1,)}}
    with pytest.raises(ValidationError):
        r.tool = "x.y"  # type: ignore[misc]


def test_defaults() -> None:
    a, b = req(), req()
    assert a.action_id != b.action_id
    assert len(a.action_id) == 32
    assert a.paths == ()
    assert a.provenance == frozenset()


def test_fingerprint_stable() -> None:
    a = req(args={"x": 1, "y": {"p": 1, "q": 2}}, summary="one")
    b = req(
        args={"y": {"q": 2, "p": 1}, "x": 1},
        summary="two",
        provenance={ProvenanceFlag.FAST_PATH, ProvenanceFlag.RISKY_REQUEST},
    )
    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 64
    assert a.canonical_args() == '{"x":1,"y":{"p":1,"q":2}}'


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "r2"},
        {"tool": "files.copy"},
        {"risk_class": RiskClass.DANGEROUS},
        {"args": {"a": 2}},
        {"args": {"a": "1"}},
        {"args": {"a": 1, "n": {"k": [1, 2]}}},
    ],
)
def test_fingerprint_changes(change: dict[str, Any]) -> None:
    assert req().fingerprint() != req(**change).fingerprint()


def test_fingerprint_nested_change() -> None:
    a = req(args={"n": {"k": [1, 2]}})
    b = req(args={"n": {"k": [1, 3]}})
    assert a.fingerprint() != b.fingerprint()


def dec(**kw: Any) -> Decision:
    base: dict[str, Any] = {
        "verdict": Verdict.ALLOW,
        "reason": ReasonCode.READ_ALLOWED,
        "risk_class": RiskClass.READ,
        "action_fingerprint": FP,
    }
    return Decision(**{**base, **kw})


def test_valid_decisions() -> None:
    dec()
    dec(
        verdict=Verdict.NEEDS_APPROVAL,
        reason=ReasonCode.DANGEROUS_APPROVAL_REQUIRED,
        risk_class=RiskClass.DANGEROUS,
        approval_channel=ApprovalChannel.VOICE,
    )
    dec(
        verdict=Verdict.DENY,
        reason=ReasonCode.FORBIDDEN_RISK_CLASS,
        risk_class=RiskClass.FORBIDDEN,
    )
    dec(
        verdict=Verdict.ALLOW,
        reason=ReasonCode.MODIFY_ALLOWED_BY_SETTINGS,
        risk_class=RiskClass.MODIFY,
    )


NEEDS = {
    "verdict": Verdict.NEEDS_APPROVAL,
    "reason": ReasonCode.MODIFY_APPROVAL_REQUIRED,
    "risk_class": RiskClass.MODIFY,
}


@pytest.mark.parametrize(
    "kw",
    [
        {"verdict": Verdict.DENY},
        {"action_fingerprint": "xyz"},
        {"detail": "x" * 301},
        {"approval_channel": ApprovalChannel.NONE},
        {"approval_channel": ApprovalChannel.TEXT},
        NEEDS,
        {**NEEDS, "approval_channel": ApprovalChannel.NONE},
        {
            "verdict": Verdict.DENY,
            "reason": ReasonCode.PATH_INVALID,
            "approval_channel": ApprovalChannel.TEXT,
        },
        {"risk_class": RiskClass.FORBIDDEN},
        {**NEEDS, "risk_class": RiskClass.FORBIDDEN, "approval_channel": ApprovalChannel.TEXT},
        {"risk_class": RiskClass.DANGEROUS},
        {"extra": 1},
    ],
)
def test_invalid_decisions(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        dec(**kw)


def test_decision_frozen() -> None:
    with pytest.raises(ValidationError):
        dec().detail = "x"  # type: ignore[misc]


def test_args_deep_frozen() -> None:
    r = req(args={"x": [1, {"k": [2]}], "d": {"a": 1}})
    fp = r.fingerprint()
    assert isinstance(r.args, dict)
    for mutate in (
        lambda: r.args.__setitem__("z", 1),
        lambda: r.args["d"].__setitem__("a", 2),
        lambda: r.args["d"].update({"a": 2}),
        lambda: r.args["d"].pop("a"),
        lambda: r.args["d"].clear(),
        lambda: r.args["d"].setdefault("q", 1),
        lambda: r.args["d"].popitem(),
        lambda: r.args["x"].append(2),  # tuple
        lambda: r.args["x"][1]["k"].append(3),
    ):
        with pytest.raises((TypeError, AttributeError)):
            mutate()
    assert r.fingerprint() == fp
    assert r.canonical_args() == '{"d":{"a":1},"x":[1,{"k":[2]}]}'


def test_paths_in_fingerprint() -> None:
    assert req(paths=("a", "b")).fingerprint() != req(paths=("b", "a")).fingerprint()
    assert req(paths=("a",)).fingerprint() != req().fingerprint()


def test_model_copy_revalidates() -> None:
    r = req()
    with pytest.raises(ValidationError):
        r.model_copy(update={"args": {"a": {1, 2}}})
    c = r.model_copy(update={"args": {"a": 2}})
    assert c.fingerprint() != r.fingerprint()
    d = dec(verdict=Verdict.DENY, reason=ReasonCode.PATH_INVALID)
    with pytest.raises(ValidationError):
        d.model_copy(update={"verdict": Verdict.ALLOW})


def test_model_construct_blocked() -> None:
    with pytest.raises(TypeError):
        ActionRequest.model_construct(run_id="r")
    with pytest.raises(TypeError):
        Decision.model_construct(verdict=Verdict.ALLOW)


@pytest.mark.parametrize("args", [{"a": "\ud800"}, {"\ud800": 1}, {"a": ["x", "\udfff"]}])
def test_unencodable_args(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        req(args=args)


@pytest.mark.parametrize(
    ("reason", "risk", "ok"),
    [
        (ReasonCode.READ_ALLOWED, RiskClass.READ, True),
        (ReasonCode.READ_ALLOWED, RiskClass.MODIFY, False),
        (ReasonCode.MODIFY_ALLOWED_BY_SETTINGS, RiskClass.MODIFY, True),
        (ReasonCode.MODIFY_ALLOWED_BY_SETTINGS, RiskClass.READ, False),
        (ReasonCode.MODIFY_APPROVAL_REQUIRED, RiskClass.MODIFY, True),
        (ReasonCode.MODIFY_APPROVAL_REQUIRED, RiskClass.DANGEROUS, False),
        (ReasonCode.DANGEROUS_APPROVAL_REQUIRED, RiskClass.DANGEROUS, True),
        (ReasonCode.DANGEROUS_APPROVAL_REQUIRED, RiskClass.MODIFY, False),
        (ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED, RiskClass.READ, True),
        (ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED, RiskClass.MODIFY, True),
        (ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED, RiskClass.DANGEROUS, False),
        (ReasonCode.FORBIDDEN_RISK_CLASS, RiskClass.FORBIDDEN, True),
        (ReasonCode.FORBIDDEN_RISK_CLASS, RiskClass.READ, False),
        (ReasonCode.PATH_INVALID, RiskClass.READ, True),
        (ReasonCode.BUDGET_EXHAUSTED, RiskClass.DANGEROUS, True),
        (ReasonCode.UNKNOWN_TOOL, RiskClass.FORBIDDEN, True),
    ],
)
def test_reason_risk_pairs(reason: ReasonCode, risk: RiskClass, ok: bool) -> None:
    verdict = REASON_VERDICT[reason]
    kw: dict[str, Any] = {"verdict": verdict, "reason": reason, "risk_class": risk}
    if verdict is Verdict.NEEDS_APPROVAL:
        kw["approval_channel"] = ApprovalChannel.TEXT
    if ok:
        dec(**kw)
    else:
        with pytest.raises(ValidationError):
            dec(**kw)


def test_cache_replay_flag_is_plain_and_ignored_by_fingerprint() -> None:
    assert ProvenanceFlag.CACHE_REPLAY == "cache_replay"
    plain = req()
    replay = req(provenance=frozenset({ProvenanceFlag.CACHE_REPLAY}))
    assert ProvenanceFlag.CACHE_REPLAY in replay.provenance
    assert replay.fingerprint() == plain.fingerprint()
