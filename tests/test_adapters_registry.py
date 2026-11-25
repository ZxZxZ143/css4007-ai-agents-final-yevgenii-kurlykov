import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from deskpilot.adapters import ConformanceError, ToolContext, ToolError, ToolRegistry, ToolSpec
from deskpilot.adapters.base import VerifyResult
from deskpilot.policy import (
    ActionGate,
    ApprovalStore,
    BudgetTracker,
    PathPolicy,
    Permit,
    ProvenanceFlag,
    RiskClass,
)
from deskpilot.settings import Settings

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = ""
    path: str = ""


class LooseArgs(BaseModel):
    text: str = ""


class Res(BaseModel):
    out: str = ""
    items: list[int] = []


def _ok(a: Args, c: ToolContext) -> Res:
    return Res(out="real:" + a.text)


def _mock(a: Args, c: ToolContext) -> Res:
    return Res(out="mock:" + a.text)


def make_spec(name: str = "t.echo", **kw: object) -> ToolSpec:
    fields: dict[str, object] = {
        "name": name,
        "description": "echo",
        "args_model": Args,
        "result_model": Res,
        "risk": RiskClass.READ,
        "execute": _ok,
        "mock": _mock,
        "paths": lambda a: (a.path,) if a.path else (),
    }
    fields.update(kw)
    return ToolSpec(**fields)  # type: ignore[arg-type]


class Env:
    def __init__(self, tmp: Path, *specs: ToolSpec, mode: str = "mock") -> None:
        self.root = tmp / "docs"
        self.root.mkdir()
        self.settings = Settings(allowed_roots=(self.root,))
        self.policy = PathPolicy([self.root], settings_dir=tmp / "settings")
        self.registry = ToolRegistry()
        for spec in specs:
            self.registry.register(spec)
        self.gate = ActionGate(
            self.settings,
            tool_risks=self.registry.tool_risks(),
            paths=self.policy,
            budgets=BudgetTracker(self.settings.budgets),
            approvals=ApprovalStore(),
            file_ops=self.registry.file_ops,
        )
        self.ctx = ToolContext(
            run_id="run1",
            settings=self.settings,
            path_policy=self.policy,
            mode=mode,  # type: ignore[arg-type]
        )

    def run(self, tool: str = "t.echo", **args: object):
        action = self.registry.build_action("run1", tool, args)
        return self.registry.dispatch(action, self.gate.authorize(action), self.ctx)


# -- conformance -------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        make_spec(name="Bad Name"),
        make_spec(name="nodot_"[:0] + "1x"),
        make_spec(description=""),
        make_spec(description="x" * 401),
        make_spec(args_model=LooseArgs),
        make_spec(mock=None),
        make_spec(risk=RiskClass.FORBIDDEN),
        make_spec(risk=lambda a: RiskClass.READ),
        make_spec(risk=lambda a: RiskClass.READ, max_risk=RiskClass.FORBIDDEN),
        make_spec(untrusted_fields=frozenset({"nope"})),
    ],
    ids=[
        "bad_name",
        "digit_start",
        "empty_description",
        "long_description",
        "args_not_forbid",
        "missing_mock",
        "forbidden_risk",
        "callable_without_max",
        "forbidden_max",
        "unknown_untrusted_field",
    ],
)
def test_register_rejects_broken_spec(spec: ToolSpec) -> None:
    with pytest.raises(ConformanceError):
        ToolRegistry().register(spec)


def test_register_rejects_duplicate_name() -> None:
    registry = ToolRegistry()
    registry.register(make_spec())
    with pytest.raises(ConformanceError):
        registry.register(make_spec())


def test_tool_risks_uses_static_or_declared_max() -> None:
    registry = ToolRegistry()
    registry.register(make_spec("t.a"))
    registry.register(make_spec("t.b", risk=lambda a: RiskClass.READ, max_risk=RiskClass.MODIFY))
    assert registry.tool_risks() == {"t.a": RiskClass.READ, "t.b": RiskClass.MODIFY}


# -- model tools and version -------------------------------------------------


def test_model_tools_schemas_and_name_roundtrip() -> None:
    registry = ToolRegistry()
    registry.register(make_spec("app.open"))
    registry.register(make_spec("files.read_text"))
    tools = registry.model_tools()
    names = [t["function"]["name"] for t in tools]
    assert names == ["app__open", "files__read_text"]
    for tool in tools:
        assert tool["type"] == "function"
        schema = tool["function"]["parameters"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "." not in tool["function"]["name"]
    assert [registry.to_tool_name(n) for n in names] == ["app.open", "files.read_text"]
    with pytest.raises(ToolError):
        registry.to_tool_name("nope")


def test_version_is_stable_and_changes_with_schema() -> None:
    a, b = ToolRegistry(), ToolRegistry()
    for r in (a, b):
        r.register(make_spec("t.a"))
        r.register(make_spec("t.b"))
    assert a.version() == a.version() == b.version()
    assert len(a.version()) == 64

    class Other(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = ""
        extra_field: int = 0

    c = ToolRegistry()
    c.register(make_spec("t.a"))
    c.register(make_spec("t.b", args_model=Other))
    assert c.version() != a.version()


# -- build_action ------------------------------------------------------------


def test_build_action_validates_and_derives_paths(tmp_path: Path) -> None:
    env = Env(tmp_path, make_spec())
    action = env.registry.build_action(
        "run1", "t.echo", {"path": "C:\\x\\a.txt"}, frozenset({ProvenanceFlag.RISKY_REQUEST})
    )
    assert action.paths == ("C:\\x\\a.txt",)
    assert action.risk_class is RiskClass.READ
    assert action.provenance == {ProvenanceFlag.RISKY_REQUEST}
    assert action.args == {"text": "", "path": "C:\\x\\a.txt"}


@pytest.mark.parametrize("raw", [{"bogus": 1}, {"text": 5}])
def test_build_action_rejects_bad_args(tmp_path: Path, raw: dict) -> None:
    env = Env(tmp_path, make_spec())
    with pytest.raises(ToolError) as exc:
        env.registry.build_action("run1", "t.echo", raw)
    assert exc.value.kind == "invalid_args"


def test_build_action_unknown_tool_and_model_name(tmp_path: Path) -> None:
    env = Env(tmp_path, make_spec("t.echo"))
    with pytest.raises(ToolError) as exc:
        env.registry.build_action("run1", "t.nope", {})
    assert exc.value.kind == "not_found"
    assert env.registry.build_action("run1", "t__echo", {}).tool == "t.echo"


# -- dispatch ----------------------------------------------------------------


def test_end_to_end_mock_and_real_mode(tmp_path: Path) -> None:
    mock_env = Env(tmp_path / "m", make_spec()) if (tmp_path / "m").mkdir() is None else None
    assert mock_env is not None
    assert mock_env.run(text="hi").data["out"] == "mock:hi"
    (tmp_path / "r").mkdir()
    assert Env(tmp_path / "r", make_spec(), mode="real").run(text="hi").data["out"] == "real:hi"


def test_dispatch_refuses_wrong_fingerprint(tmp_path: Path) -> None:
    calls: list[int] = []
    env = Env(tmp_path, make_spec(mock=lambda a, c: calls.append(1) or Res()))
    action = env.registry.build_action("run1", "t.echo", {"text": "a"})
    other = env.registry.build_action("run1", "t.echo", {"text": "b"})
    outcome = env.registry.dispatch(action, env.gate.authorize(other), env.ctx)
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.kind == "permission"
    assert calls == []


def test_dispatch_refuses_forged_permit_with_foreign_decision(tmp_path: Path) -> None:
    env = Env(tmp_path, make_spec())
    action = env.registry.build_action("run1", "t.echo", {"text": "a"})
    other = env.registry.build_action("run1", "t.echo", {"text": "b"})
    real = env.gate.authorize(other)
    forged = Permit(action_fingerprint=action.fingerprint(), decision=real.decision)
    assert not env.registry.dispatch(action, forged, env.ctx).ok


def test_dispatch_revalidates_args_and_paths_against_the_action(tmp_path: Path) -> None:
    env = Env(tmp_path, make_spec())
    from deskpilot.policy import ActionRequest

    # paths do not match what the args derive: the tool refuses even with a matching permit
    bad = ActionRequest(
        run_id="run1",
        tool="t.echo",
        risk_class=RiskClass.READ,
        args={"path": "C:\\Windows"},
        paths=(),
    )
    outcome = env.registry.dispatch(
        bad, Permit(action_fingerprint=bad.fingerprint(), decision=env.gate.evaluate(bad)), env.ctx
    )
    assert not outcome.ok
    unknown_arg = ActionRequest(
        run_id="run1", tool="t.echo", risk_class=RiskClass.READ, args={"zzz": 1}
    )
    permit = Permit(
        action_fingerprint=unknown_arg.fingerprint(), decision=env.gate.evaluate(unknown_arg)
    )
    out = env.registry.dispatch(unknown_arg, permit, env.ctx)
    assert out.error is not None
    assert out.error.kind == "invalid_args"


def test_dispatch_rechecks_paths_before_running(tmp_path: Path) -> None:
    env = Env(tmp_path, make_spec())
    inside = str(env.root / "a.txt")
    action = env.registry.build_action("run1", "t.echo", {"path": inside})
    permit = env.gate.authorize(action)
    # a stricter policy at execution time (as after a swapped junction) blocks the call
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    strict = ToolContext(
        run_id="run1",
        settings=env.settings,
        path_policy=PathPolicy([other_root], settings_dir=tmp_path / "settings"),
    )
    outcome = env.registry.dispatch(action, permit, strict)
    assert outcome.error is not None
    assert outcome.error.kind == "permission"


def test_exception_text_is_not_leaked(tmp_path: Path) -> None:
    def boom(a: Args, c: ToolContext) -> Res:
        raise RuntimeError("key sk-SECRET123 leaked")

    env = Env(tmp_path, make_spec(mock=boom))
    outcome = env.run()
    assert outcome.error is not None
    assert outcome.error.kind == "internal"
    assert "sk-" not in outcome.model_dump_json()
    assert "RuntimeError" in outcome.error.message


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (FileNotFoundError("C:\\secret"), "not_found", False),
        (PermissionError("denied"), "permission", False),
        (TimeoutError("slow"), "timeout", True),
        (ToolError("transient", "later"), "transient", True),
    ],
)
def test_exception_mapping(tmp_path: Path, exc: Exception, kind: str, retryable: bool) -> None:
    def boom(a: Args, c: ToolContext) -> Res:
        raise exc

    outcome = Env(tmp_path, make_spec(mock=boom)).run()
    assert outcome.error is not None
    assert (outcome.error.kind, outcome.error.retryable) == (kind, retryable)
    assert "secret" not in outcome.model_dump_json()


def test_wrong_result_type_is_internal_error(tmp_path: Path) -> None:
    class Other(BaseModel):
        pass

    outcome = Env(tmp_path, make_spec(mock=lambda a, c: Other())).run()
    assert outcome.error is not None
    assert outcome.error.kind == "internal"


def test_output_is_bounded_and_untrusted_flag(tmp_path: Path) -> None:
    spec = make_spec(
        mock=lambda a, c: Res(out="z" * 9000, items=list(range(300))),
        untrusted_fields=frozenset({"out"}),
    )
    outcome = Env(tmp_path, spec).run()
    assert len(outcome.data["out"]) == 4000
    assert len(outcome.data["items"]) == 100
    assert outcome.truncated is True
    assert outcome.untrusted is True
    empty = (
        Env(tmp_path / "e", make_spec(mock=lambda a, c: Res(), untrusted_fields=frozenset({"out"})))
        if (tmp_path / "e").mkdir() is None
        else None
    )
    assert empty is not None
    assert empty.run().untrusted is False


def test_verify_runs_and_failures_are_contained(tmp_path: Path) -> None:
    ok = Env(tmp_path, make_spec(verify=lambda a, r, c: VerifyResult(ok=True, detail="seen")))
    assert ok.run().verify == VerifyResult(ok=True, detail="seen")

    def bad(a: Args, r: Res, c: ToolContext) -> VerifyResult:
        raise ValueError("sk-LEAK")

    (tmp_path / "v").mkdir()
    out = Env(tmp_path / "v", make_spec(verify=bad)).run()
    assert out.ok is True
    assert out.verify is not None
    assert out.verify.ok is False
    assert "sk-" not in out.model_dump_json()


def test_dispatch_requires_approval_id_for_needs_approval(tmp_path: Path) -> None:
    calls: list[int] = []
    spec = make_spec(risk=RiskClass.MODIFY, mock=lambda a, c: calls.append(1) or Res())
    env = Env(tmp_path, spec)
    action = env.registry.build_action("run1", "t.echo", {})
    decision = env.gate.evaluate(action)
    assert decision.verdict.value == "needs_approval"
    permit = Permit(action_fingerprint=action.fingerprint(), decision=decision, approval_id=None)
    outcome = env.registry.dispatch(action, permit, env.ctx)
    assert outcome.error is not None
    assert outcome.error.kind == "permission"
    assert calls == []
    with_id = Permit(action_fingerprint=action.fingerprint(), decision=decision, approval_id="a1")
    assert env.registry.dispatch(action, with_id, env.ctx).ok
