"""Adapter SDK v0: tool specs, execution context, typed errors and bounded outcomes.

A tool never decides what it may do. The registry derives risk and paths from validated
arguments, the gate decides, and dispatch executes only with a matching Permit.
Everything a tool returns is bounded data; fields listed in `untrusted_fields` carry
external content (file text, page text, command output) and never grant permission.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.policy.paths import PathPolicy
from deskpilot.policy.types import RiskClass
from deskpilot.settings import Settings

TOOL_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
MAX_STR_CHARS: Final = 4000
MAX_LIST_ITEMS: Final = 100
MAX_ERROR_CHARS: Final = 300

ToolErrorKind = Literal[
    "invalid_args", "not_found", "permission", "unsupported", "transient", "timeout", "internal"
]
_RETRYABLE: Final = frozenset({"transient", "timeout"})


class ToolError(Exception):
    """Typed tool failure. The message must be short and safe to show to the model."""

    def __init__(self, kind: ToolErrorKind, message: str, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.kind: ToolErrorKind = kind
        self.message = message[:MAX_ERROR_CHARS]
        self.retryable = kind in _RETRYABLE if retryable is None else retryable


class VerifyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    detail: str = Field(default="", max_length=MAX_ERROR_CHARS)


class ToolErrorInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ToolErrorKind
    message: str
    retryable: bool


class ToolOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolErrorInfo | None = None
    untrusted: bool = False
    verify: VerifyResult | None = None
    truncated: bool = False


class ToolContext(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_id: str = Field(min_length=1)
    settings: Settings
    path_policy: PathPolicy
    mode: Literal["mock", "real"] = "mock"
    sandbox_dir: Path | None = None


def _no_paths(_args: Any) -> tuple[str, ...]:
    return ()


def _no_file_ops(_args: Any) -> int:
    return 0


@dataclass(frozen=True)
class ToolSpec:
    """Declaration of one tool. `risk` may be a function of the validated args."""

    name: str
    description: str
    args_model: type[BaseModel]
    result_model: type[BaseModel]
    risk: RiskClass | Callable[[Any], RiskClass]
    execute: Callable[[Any, ToolContext], BaseModel]
    mock: Callable[[Any, ToolContext], BaseModel] | None = None
    paths: Callable[[Any], tuple[str, ...]] = _no_paths
    file_ops: Callable[[Any], int] = _no_file_ops
    verify: Callable[[Any, Any, ToolContext], VerifyResult] | None = None
    untrusted_fields: frozenset[str] = frozenset()
    max_risk: RiskClass | None = None  # required when `risk` is a callable

    def risk_for(self, args: Any) -> RiskClass:
        return self.risk if isinstance(self.risk, RiskClass) else self.risk(args)

    def declared_risk(self) -> RiskClass | None:
        """Static risk for the gate: the fixed value, or the declared maximum."""
        return self.risk if isinstance(self.risk, RiskClass) else self.max_risk


def bound_data(value: Any) -> tuple[Any, bool]:
    """Cap string length and list size recursively; also report whether anything was cut."""
    if isinstance(value, str):
        return value[:MAX_STR_CHARS], len(value) > MAX_STR_CHARS
    if isinstance(value, list | tuple):
        parts = [bound_data(v) for v in value[:MAX_LIST_ITEMS]]
        return [v for v, _ in parts], len(value) > MAX_LIST_ITEMS or any(t for _, t in parts)
    if isinstance(value, dict):
        items = {str(k): bound_data(v) for k, v in value.items()}
        return {k: v for k, (v, _) in items.items()}, any(t for _, t in items.values())
    return value, False
