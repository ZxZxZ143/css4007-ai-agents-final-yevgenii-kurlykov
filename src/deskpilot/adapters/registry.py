"""Tool registry: conformance checks, model-facing schemas, action building and dispatch."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from deskpilot.adapters.base import (
    MAX_ERROR_CHARS,
    TOOL_NAME_PATTERN,
    ToolContext,
    ToolError,
    ToolErrorInfo,
    ToolOutcome,
    ToolSpec,
    VerifyResult,
    bound_data,
)
from deskpilot.policy.gate import Permit
from deskpilot.policy.types import ActionRequest, ProvenanceFlag, RiskClass, Verdict

_NAME_RE = re.compile(TOOL_NAME_PATTERN)
_MODEL_SEP = "__"
_MAX_SUMMARY = 500


class ConformanceError(Exception):
    """A tool spec violates the adapter contract."""


def _model_name(name: str) -> str:
    return name.replace(".", _MODEL_SEP)


def _fail(tool: str, error: ToolError) -> ToolOutcome:
    info = ToolErrorInfo(kind=error.kind, message=error.message, retryable=error.retryable)
    return ToolOutcome(tool=tool, ok=False, error=info)


def _permit_usable(permit: Permit) -> bool:
    verdict = permit.decision.verdict
    return verdict is Verdict.ALLOW or (
        verdict is Verdict.NEEDS_APPROVAL and permit.approval_id is not None
    )


def _to_tool_error(exc: Exception) -> ToolError:
    """Map any exception to a typed error without leaking raw exception text."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ValidationError):
        return ToolError("invalid_args", "arguments failed validation")
    if isinstance(exc, TimeoutError):
        return ToolError("timeout", "operation timed out")
    if isinstance(exc, FileNotFoundError | NotADirectoryError):
        return ToolError("not_found", "path not found")
    if isinstance(exc, PermissionError):
        return ToolError("permission", "permission denied by the operating system")
    return ToolError("internal", f"unexpected error: {type(exc).__name__}")


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._by_model_name: dict[str, str] = {}

    def register(self, spec: ToolSpec) -> None:
        self._check(spec)
        self._specs[spec.name] = spec
        self._by_model_name[_model_name(spec.name)] = spec.name

    def _check(self, spec: ToolSpec) -> None:
        if not _NAME_RE.match(spec.name):
            raise ConformanceError(f"bad tool name: {spec.name!r}")
        if spec.name in self._specs or _model_name(spec.name) in self._by_model_name:
            raise ConformanceError(f"duplicate tool name: {spec.name}")
        if not 1 <= len(spec.description) <= 400:
            raise ConformanceError(f"{spec.name}: description must be 1..400 chars")
        if not (isinstance(spec.args_model, type) and issubclass(spec.args_model, BaseModel)):
            raise ConformanceError(f"{spec.name}: args_model must be a Pydantic model")
        if spec.args_model.model_config.get("extra") != "forbid":
            raise ConformanceError(f"{spec.name}: args model must set extra='forbid'")
        if not (isinstance(spec.result_model, type) and issubclass(spec.result_model, BaseModel)):
            raise ConformanceError(f"{spec.name}: result_model must be a Pydantic model")
        if spec.mock is None:
            raise ConformanceError(f"{spec.name}: a deterministic mock is required")
        declared = spec.declared_risk()
        if declared is None:
            raise ConformanceError(f"{spec.name}: a callable risk needs max_risk")
        if declared is RiskClass.FORBIDDEN:
            raise ConformanceError(f"{spec.name}: forbidden risk cannot be a tool")
        unknown = spec.untrusted_fields - set(spec.result_model.model_fields)
        if unknown:
            raise ConformanceError(f"{spec.name}: unknown untrusted fields {sorted(unknown)}")

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(self._by_model_name.get(name, name))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def tool_risks(self) -> Mapping[str, RiskClass]:
        return {
            name: spec.declared_risk() or RiskClass.DANGEROUS for name, spec in self._specs.items()
        }

    def model_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": _model_name(spec.name),
                    "description": spec.description,
                    "parameters": spec.args_model.model_json_schema(),
                },
            }
            for spec in (self._specs[n] for n in self.names())
        ]

    def to_tool_name(self, model_name: str) -> str:
        """Reverse the model-safe name mapping ("app__open" -> "app.open")."""
        if model_name not in self._by_model_name:
            raise ToolError("not_found", "unknown tool")
        return self._by_model_name[model_name]

    def version(self) -> str:
        payload = [[n, self._specs[n].args_model.model_json_schema()] for n in self.names()]
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def file_ops(self, action: ActionRequest) -> int:
        """File-operation weight for the gate's budget check, from validated args."""
        spec = self._specs.get(action.tool)
        if spec is None:
            return len(action.paths)
        try:
            return spec.file_ops(spec.args_model.model_validate(dict(action.args), strict=True))
        except ValidationError:
            return len(action.paths)

    def build_action(
        self,
        run_id: str,
        tool: str,
        raw_args: Mapping[str, Any],
        provenance: frozenset[ProvenanceFlag] = frozenset(),
        summary: str = "",
    ) -> ActionRequest:
        spec = self.get(tool)
        if spec is None:
            raise ToolError("not_found", "unknown tool")
        try:
            args = spec.args_model.model_validate(dict(raw_args), strict=True)
        except ValidationError as exc:
            raise ToolError("invalid_args", "arguments failed validation") from exc
        return ActionRequest(
            run_id=run_id,
            tool=spec.name,
            risk_class=spec.risk_for(args),
            args=args.model_dump(mode="json"),
            paths=spec.paths(args),
            provenance=provenance,
            summary=summary[:_MAX_SUMMARY],
        )

    def dispatch(self, action: ActionRequest, permit: Permit, ctx: ToolContext) -> ToolOutcome:
        """Execute only with a Permit for exactly this action; re-check args and paths first."""
        spec = self._specs.get(action.tool)
        if spec is None:
            return _fail(action.tool, ToolError("not_found", "unknown tool"))
        fingerprint = action.fingerprint()
        if (
            permit.action_fingerprint != fingerprint
            or permit.decision.action_fingerprint != fingerprint
            or not _permit_usable(permit)
        ):
            return _fail(action.tool, ToolError("permission", "permit does not match the action"))
        try:
            args = spec.args_model.model_validate(dict(action.args), strict=True)
            if spec.risk_for(args) is not action.risk_class or spec.paths(args) != action.paths:
                raise ToolError("permission", "action does not match its validated arguments")
            for raw in action.paths:
                check = ctx.path_policy.check(raw)
                if not check.ok:
                    raise ToolError("permission", f"path denied: {check.detail}"[:MAX_ERROR_CHARS])
            run = spec.mock if ctx.mode == "mock" else spec.execute
            assert run is not None
            result = run(args, ctx)
            if not isinstance(result, spec.result_model):
                raise ToolError("internal", "tool returned a wrong result type")
            data, truncated = bound_data(result.model_dump(mode="json"))
            untrusted = any(getattr(result, f) for f in spec.untrusted_fields)
        except Exception as exc:  # map everything; never leak raw exception text
            return _fail(action.tool, _to_tool_error(exc))
        return ToolOutcome(
            tool=action.tool,
            ok=True,
            data=data,
            untrusted=untrusted,
            truncated=truncated,
            verify=self._verify(spec, args, result, ctx),
        )

    @staticmethod
    def _verify(
        spec: ToolSpec, args: Any, result: BaseModel, ctx: ToolContext
    ) -> VerifyResult | None:
        if spec.verify is None:
            return None
        try:
            return spec.verify(args, result, ctx)
        except Exception as exc:
            return VerifyResult(ok=False, detail=f"verify failed: {type(exc).__name__}")
