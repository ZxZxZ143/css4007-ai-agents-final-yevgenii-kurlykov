"""Adapter conformance kit: every adapter must pass `check_adapter` in its own tests.

Plain functions, no test-framework dependency. Registration problems are reported as
findings instead of exceptions, so one run lists everything that is wrong.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from deskpilot.adapters.base import (
    MAX_LIST_ITEMS,
    MAX_STR_CHARS,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from deskpilot.adapters.registry import ConformanceError, ToolRegistry
from deskpilot.policy import ActionGate, ApprovalStore, BudgetTracker, PathPolicy, Permit
from deskpilot.policy.types import RiskClass

MAX_OUTPUT_BYTES: Final = MAX_STR_CHARS * MAX_LIST_ITEMS * 4
_ORDER: Final = {RiskClass.READ: 0, RiskClass.MODIFY: 1, RiskClass.DANGEROUS: 2}
_MAX_DESCRIPTION_LINES: Final = 3


@dataclasses.dataclass(frozen=True)
class Finding:
    tool: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.tool} [{self.check}] {self.message}"


@dataclasses.dataclass
class ConformanceReport:
    findings: list[Finding] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, tool: str, check: str, message: str) -> None:
        self.findings.append(Finding(tool, check, message))

    def checks(self, tool: str | None = None) -> set[str]:
        return {f.check for f in self.findings if tool is None or f.tool == tool}


def _walk_schema(schema: Mapping[str, Any], where: str, out: list[str]) -> None:
    kind = schema.get("type")
    if kind == "string" and "maxLength" not in schema and "enum" not in schema:
        out.append(f"string field {where} has no maxLength")
    if kind == "array":
        if "maxItems" not in schema:
            out.append(f"array field {where} has no maxItems")
        if isinstance(schema.get("items"), dict):
            _walk_schema(schema["items"], f"{where}[]", out)
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(key, [])):
            _walk_schema(sub, f"{where}|{i}", out)
    for name, sub in schema.get("properties", {}).items():
        _walk_schema(sub, f"{where}.{name}" if where else name, out)


def _check_schema(
    spec: ToolSpec, samples: Sequence[Mapping[str, Any]], rep: ConformanceReport
) -> None:
    schema = spec.args_model.model_json_schema()
    if schema.get("additionalProperties") is not False:
        rep.add(spec.name, "schema", "args schema must set additionalProperties false")
    problems: list[str] = []
    _walk_schema(schema, "", problems)
    for problem in problems:
        rep.add(spec.name, "schema", problem)
    for raw in samples:
        try:
            spec.args_model.model_validate(dict(raw), strict=True)
        except ValidationError:
            rep.add(spec.name, "sample", f"sample does not validate strictly: {dict(raw)}")
            continue
        try:
            spec.args_model.model_validate({**raw, "zz_extra": 1}, strict=True)
            rep.add(spec.name, "schema", "an unknown extra key was accepted")
        except ValidationError:
            pass
        if raw:
            key = next(iter(raw))
            try:
                spec.args_model.model_validate({**raw, key: object()}, strict=True)
                rep.add(spec.name, "schema", f"wrong type accepted for field {key}")
            except ValidationError:
                pass


def _check_risk(spec: ToolSpec, args: BaseModel, rep: ConformanceReport) -> None:
    declared = spec.declared_risk()
    if declared is None or declared is RiskClass.FORBIDDEN:
        rep.add(spec.name, "risk", "declared risk is missing or forbidden")
        return
    actual = spec.risk_for(args)
    if actual is RiskClass.FORBIDDEN or _ORDER[actual] > _ORDER[declared]:
        rep.add(spec.name, "risk", f"risk_for gave {actual.value}, above declared {declared.value}")


def _check_mock(
    spec: ToolSpec,
    args: BaseModel,
    ctx: ToolContext,
    reset: Callable[[], None] | None,
    rep: ConformanceReport,
) -> None:
    if spec.mock is None:
        rep.add(spec.name, "mock", "no mock")
        return
    results: list[BaseModel] = []
    try:
        for _ in range(2):
            if reset is not None:
                reset()
            results.append(spec.mock(args, ctx))
    except Exception as exc:
        rep.add(spec.name, "mock", f"mock raised {type(exc).__name__}")
        return
    if not all(isinstance(r, spec.result_model) for r in results):
        rep.add(spec.name, "mock", "mock result is not an instance of result_model")
    elif results[0].model_dump(mode="json") != results[1].model_dump(mode="json"):
        rep.add(spec.name, "mock", "mock is not deterministic")


def _make_gate(registry: ToolRegistry, ctx: ToolContext) -> ActionGate:
    return ActionGate(
        ctx.settings,
        tool_risks=registry.tool_risks(),
        paths=ctx.path_policy,
        budgets=BudgetTracker(ctx.settings.budgets),
        approvals=ApprovalStore(),
        file_ops=registry.file_ops,
    )


def _check_description(spec: ToolSpec, rep: ConformanceReport) -> None:
    if not 1 <= len(spec.description) <= 400:
        rep.add(spec.name, "description", "description must be 1..400 chars")
    if len(spec.description.splitlines()) > _MAX_DESCRIPTION_LINES:
        rep.add(spec.name, "description", f"more than {_MAX_DESCRIPTION_LINES} lines")


def _undeclared_paths(args: BaseModel, declared: tuple[str, ...]) -> list[str]:
    values = [v for v in args.model_dump(mode="json").values() if isinstance(v, str)]
    return [v for v in values if os.path.isabs(v) and v not in declared]


def _check_runtime(
    spec: ToolSpec,
    raw: Mapping[str, Any],
    args: BaseModel,
    registry: ToolRegistry,
    spy_calls: list[str],
    ctx: ToolContext,
    strict_ctx: ToolContext,
    rep: ConformanceReport,
) -> None:
    action = registry.build_action(ctx.run_id, spec.name, raw)
    decision = _make_gate(registry, ctx).evaluate(action)
    # gate: a permit for another fingerprint must be refused and must not run the tool
    spy_calls.clear()
    forged = Permit(action_fingerprint="0" * 64, decision=decision, approval_id="conformance")
    refused = registry.dispatch(action, forged, ctx)
    if refused.ok or refused.error is None or refused.error.kind != "permission":
        rep.add(spec.name, "gate", "dispatch accepted a permit with a wrong fingerprint")
    if spy_calls:
        rep.add(spec.name, "gate", "tool ran despite a mismatching permit")
    declared = spec.paths(args)
    if not isinstance(declared, tuple) or not all(isinstance(p, str) for p in declared):
        rep.add(spec.name, "paths", "paths() must return a tuple of str")
        return
    for stray in _undeclared_paths(args, declared):
        rep.add(spec.name, "paths", f"path-like argument not declared by paths(): {stray}")
    permit = Permit(
        action_fingerprint=action.fingerprint(), decision=decision, approval_id="conformance"
    )
    if declared and all(ctx.path_policy.check(p).ok for p in declared):
        spy_calls.clear()
        denied = registry.dispatch(action, permit, strict_ctx)
        if denied.ok or denied.error is None or denied.error.kind != "permission" or spy_calls:
            rep.add(spec.name, "paths", "dispatch did not enforce the path policy")
    outcome = registry.dispatch(action, permit, ctx)
    if not outcome.ok:
        detail = outcome.error.message if outcome.error else "unknown"
        rep.add(spec.name, "sample", f"sample did not dispatch successfully: {detail}")
        return
    _check_outcome(spec, outcome, rep)


def _check_outcome(spec: ToolSpec, outcome: ToolOutcome, rep: ConformanceReport) -> None:
    if outcome.truncated:
        rep.add(spec.name, "output", "output was truncated by the bounds")
    if len(json.dumps(outcome.data).encode("utf-8")) > MAX_OUTPUT_BYTES:
        rep.add(spec.name, "output", "output exceeds the byte cap")
    has_untrusted = any(outcome.data.get(f) for f in spec.untrusted_fields)
    if has_untrusted and not outcome.untrusted:
        rep.add(spec.name, "output", "untrusted data not flagged as untrusted")


def _spy(spec: ToolSpec, calls: list[str]) -> ToolSpec:
    inner_mock, inner_exec = spec.mock, spec.execute

    def mock(args: Any, ctx: ToolContext) -> BaseModel:
        calls.append(spec.name)
        assert inner_mock is not None
        return inner_mock(args, ctx)

    def execute(args: Any, ctx: ToolContext) -> BaseModel:
        calls.append(spec.name)
        return inner_exec(args, ctx)

    return dataclasses.replace(spec, mock=mock, execute=execute)


def check_adapter(
    register: Callable[[ToolRegistry], None],
    *,
    ctx: ToolContext,
    samples: Mapping[str, Sequence[Mapping[str, Any]]],
    reset: Callable[[], None] | None = None,
) -> ConformanceReport:
    """Run every conformance check against the tools that `register` adds."""
    rep = ConformanceReport()
    probe = ToolRegistry()
    try:
        register(probe)
    except ConformanceError as exc:
        rep.add("*", "register", str(exc))
        return rep
    spy_calls: list[str] = []
    specs = [s for s in (probe.get(n) for n in probe.names()) if s is not None]
    spied = ToolRegistry()
    for spec in specs:
        spied.register(_spy(spec, spy_calls))
    anchor = ctx.settings.allowed_roots[0].parent if ctx.settings.allowed_roots else None
    # an unrelated empty root next to the allowed one (the OS temp dir is a protected zone)
    with tempfile.TemporaryDirectory(dir=anchor, prefix="conformance-") as empty:
        root = Path(empty).resolve()
        strict = ToolContext(
            run_id=ctx.run_id,
            settings=ctx.settings,
            path_policy=PathPolicy([root], settings_dir=root / "settings"),
            mode=ctx.mode,
            sandbox_dir=ctx.sandbox_dir,
        )
        for spec in specs:
            _check_description(spec, rep)
            tool_samples = samples.get(spec.name, ())
            if not tool_samples:
                rep.add(spec.name, "samples", "no samples given")
                continue
            _check_schema(spec, tool_samples, rep)
            for raw in tool_samples:
                try:
                    args = spec.args_model.model_validate(dict(raw), strict=True)
                except ValidationError:
                    continue
                _check_risk(spec, args, rep)
                _check_mock(spec, args, ctx, reset, rep)
                if reset is not None:
                    reset()
                _check_runtime(spec, raw, args, spied, spy_calls, ctx, strict, rep)
    return rep


def assert_conformant(
    register: Callable[[ToolRegistry], None],
    *,
    ctx: ToolContext,
    samples: Mapping[str, Sequence[Mapping[str, Any]]],
    reset: Callable[[], None] | None = None,
) -> None:
    report = check_adapter(register, ctx=ctx, samples=samples, reset=reset)
    if not report.ok:
        raise AssertionError("adapter is not conformant:\n" + "\n".join(map(str, report.findings)))
