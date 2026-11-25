import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from deskpilot.adapters.base import (
    MAX_LIST_ITEMS,
    MAX_STR_CHARS,
    ToolError,
    ToolOutcome,
    ToolSpec,
    VerifyResult,
    bound_data,
)
from deskpilot.policy.types import RiskClass


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 0


class Res(BaseModel):
    pass


def _spec(risk: RiskClass | object, **kw: object) -> ToolSpec:
    return ToolSpec(
        name="x.y",
        description="d",
        args_model=Args,
        result_model=Res,
        risk=risk,  # type: ignore[arg-type]
        execute=lambda a, c: Res(),
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        ("transient", True),
        ("timeout", True),
        ("invalid_args", False),
        ("not_found", False),
        ("permission", False),
        ("unsupported", False),
        ("internal", False),
    ],
)
def test_tool_error_retryable_by_kind(kind: str, retryable: bool) -> None:
    err = ToolError(kind, "m")  # type: ignore[arg-type]
    assert err.retryable is retryable
    assert err.message == "m"


def test_tool_error_message_is_capped() -> None:
    assert len(ToolError("internal", "x" * 1000).message) == 300


def test_bound_data_caps_strings_and_lists_recursively() -> None:
    data = {"s": "a" * 10_000, "l": list(range(500)), "n": {"deep": ["b" * 5000]}, "i": 3}
    out, cut = bound_data(data)
    assert cut is True
    assert len(out["s"]) == MAX_STR_CHARS
    assert len(out["l"]) == MAX_LIST_ITEMS
    assert len(out["n"]["deep"][0]) == MAX_STR_CHARS
    assert out["i"] == 3
    assert bound_data({"a": [1, 2], "s": "x"}) == ({"a": [1, 2], "s": "x"}, False)


def test_spec_static_and_callable_risk() -> None:
    static = _spec(RiskClass.READ)
    assert static.risk_for(Args()) is RiskClass.READ
    assert static.declared_risk() is RiskClass.READ
    dyn = _spec(lambda a: RiskClass.MODIFY if a.n else RiskClass.READ, max_risk=RiskClass.MODIFY)
    assert dyn.risk_for(Args(n=1)) is RiskClass.MODIFY
    assert dyn.risk_for(Args()) is RiskClass.READ
    assert dyn.declared_risk() is RiskClass.MODIFY
    assert _spec(lambda a: RiskClass.READ).declared_risk() is None


def test_spec_defaults_have_no_paths_and_no_file_ops() -> None:
    spec = _spec(RiskClass.READ)
    assert spec.paths(Args()) == ()
    assert spec.file_ops(Args()) == 0
    assert spec.untrusted_fields == frozenset()


def test_outcome_and_verify_are_frozen() -> None:
    out = ToolOutcome(tool="x.y", ok=True, verify=VerifyResult(ok=True))
    with pytest.raises(ValidationError):
        out.ok = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        out.verify.ok = False  # type: ignore[union-attr, misc]
