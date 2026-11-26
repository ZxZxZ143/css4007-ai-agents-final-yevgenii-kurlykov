import itertools
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from deskpilot.adapters import ToolContext, ToolRegistry, ToolSpec, register_system_tools
from deskpilot.adapters.conformance import assert_conformant, check_adapter
from deskpilot.policy import PathPolicy, RiskClass
from deskpilot.settings import Settings

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=50)
    path: str = Field(default="", max_length=300)


class LooseArgs(BaseModel):
    text: str = Field(default="", max_length=50)


class NoMaxArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = ""


class Res(BaseModel):
    out: str = ""


def _mock(a: Any, c: ToolContext) -> Res:
    return Res(out="m:" + a.text)


def make_spec(**kw: Any) -> ToolSpec:
    fields: dict[str, Any] = {
        "name": "t.echo",
        "description": "echo",
        "args_model": Args,
        "result_model": Res,
        "risk": RiskClass.READ,
        "execute": _mock,
        "mock": _mock,
        "paths": lambda a: (a.path,) if a.path else (),
    }
    fields.update(kw)
    return ToolSpec(**fields)


def reg(*specs: ToolSpec):
    def register(registry: ToolRegistry) -> None:
        for spec in specs:
            registry.register(spec)

    return register


def make_ctx(tmp: Path, **settings: Any) -> ToolContext:
    root = tmp / "sandbox"
    root.mkdir(exist_ok=True)
    s = Settings(allowed_roots=(root,), **settings)
    return ToolContext(
        run_id="run1",
        settings=s,
        path_policy=PathPolicy([root], settings_dir=tmp / "settings"),
        sandbox_dir=root,
    )


SAMPLES = {"t.echo": [{"text": "hi"}]}


def checks(tmp: Path, spec: ToolSpec, samples: dict | None = None, **kw: Any) -> set[str]:
    report = check_adapter(
        reg(spec), ctx=make_ctx(tmp), samples=SAMPLES if samples is None else samples, **kw
    )
    return report.checks()


def test_system_adapter_is_conformant(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, app_allowlist=("notepad", "calc"))
    box = ctx.sandbox_dir
    assert box is not None
    (box / "a.txt").write_text("hello", encoding="utf-8")
    samples = {
        "files.list": [{"path": str(box)}],
        "files.read_text": [{"path": str(box / "a.txt")}],
        "app.open": [{"app": "notepad"}],
        "files.open": [{"path": str(box)}],
        "browser.open_url": [{"url": "https://example.com"}],
    }
    assert_conformant(register_system_tools, ctx=ctx, samples=samples)


def test_good_toy_adapter_passes(tmp_path: Path) -> None:
    assert checks(tmp_path, make_spec()) == set()


def test_broken_adapter_raises_listing_findings(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match=r"t\.echo \[mock\]"):
        assert_conformant(
            reg(make_spec(mock=lambda a, c: Res(out=str(next(counter))))),
            ctx=make_ctx(tmp_path),
            samples=SAMPLES,
        )


counter = itertools.count()


def test_args_without_forbid_is_reported(tmp_path: Path) -> None:
    report = check_adapter(
        reg(make_spec(args_model=LooseArgs)), ctx=make_ctx(tmp_path), samples=SAMPLES
    )
    assert report.checks() == {"register"}


def test_string_without_max_length(tmp_path: Path) -> None:
    assert checks(tmp_path, make_spec(args_model=NoMaxArgs, paths=lambda a: ())) == {"schema"}


def test_missing_mock(tmp_path: Path) -> None:
    assert checks(tmp_path, make_spec(mock=None)) == {"register"}


def test_paths_omitting_the_used_path(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert ctx.sandbox_dir is not None
    spec = make_spec(paths=lambda a: ())
    samples = {"t.echo": [{"path": str(ctx.sandbox_dir / "x.txt")}]}
    report = check_adapter(reg(spec), ctx=ctx, samples=samples)
    assert "paths" in report.checks("t.echo")


def test_non_deterministic_mock(tmp_path: Path) -> None:
    spec = make_spec(mock=lambda a, c: Res(out=str(next(counter))))
    assert checks(tmp_path, spec) == {"mock"}


def test_reset_hook_makes_stateful_mock_deterministic(tmp_path: Path) -> None:
    state = itertools.count()

    def reset() -> None:
        nonlocal state
        state = itertools.count()

    spec = make_spec(mock=lambda a, c: Res(out=str(next(state))))
    assert checks(tmp_path, spec, reset=reset) == set()


def test_truncated_output_is_reported(tmp_path: Path) -> None:
    spec = make_spec(mock=lambda a, c: Res(out="z" * 5000))
    assert checks(tmp_path, spec) == {"output"}


def test_risk_above_declared_max(tmp_path: Path) -> None:
    spec = make_spec(risk=lambda a: RiskClass.DANGEROUS, max_risk=RiskClass.READ)
    assert "risk" in checks(tmp_path, spec)


def test_missing_samples(tmp_path: Path) -> None:
    assert checks(tmp_path, make_spec(), samples={}) == {"samples"}


def test_invalid_sample_and_description(tmp_path: Path) -> None:
    assert "sample" in checks(tmp_path, make_spec(), samples={"t.echo": [{"bogus": 1}]})
    assert "description" in checks(tmp_path, make_spec(description="a\nb\nc\nd"))
