"""Core policy types shared by the action gate, path policy, approvals and budgets."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from deskpilot import limits


class RiskClass(StrEnum):
    READ = "read"
    MODIFY = "modify"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


class ApprovalChannel(StrEnum):
    VOICE = "voice"
    TEXT = "text"
    NONE = "none"


class ProvenanceFlag(StrEnum):
    UNTRUSTED_CONTENT = "untrusted_content"
    RISKY_REQUEST = "risky_request"
    FAST_PATH = "fast_path"
    CACHE_REPLAY = "cache_replay"


class ReasonCode(StrEnum):
    READ_ALLOWED = "read_allowed"
    MODIFY_APPROVAL_REQUIRED = "modify_approval_required"
    MODIFY_ALLOWED_BY_SETTINGS = "modify_allowed_by_settings"
    DANGEROUS_APPROVAL_REQUIRED = "dangerous_approval_required"
    RISKY_SESSION_APPROVAL_REQUIRED = "risky_session_approval_required"
    FORBIDDEN_RISK_CLASS = "forbidden_risk_class"
    PATH_INVALID = "path_invalid"
    PATH_HARD_DENY = "path_hard_deny"
    PATH_DENIED_FILE_PATTERN = "path_denied_file_pattern"
    PATH_OUTSIDE_ALLOWED_ROOTS = "path_outside_allowed_roots"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNKNOWN_TOOL = "unknown_tool"
    ARGS_INVALID = "args_invalid"


_ALLOW = {ReasonCode.READ_ALLOWED, ReasonCode.MODIFY_ALLOWED_BY_SETTINGS}
_APPROVAL = {
    ReasonCode.MODIFY_APPROVAL_REQUIRED,
    ReasonCode.DANGEROUS_APPROVAL_REQUIRED,
    ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED,
}

REASON_VERDICT: Final = MappingProxyType(
    {
        r: Verdict.ALLOW
        if r in _ALLOW
        else Verdict.NEEDS_APPROVAL
        if r in _APPROVAL
        else Verdict.DENY
        for r in ReasonCode
    }
)

if not {c.value for c in RiskClass} >= (
    limits.ALWAYS_DENIED_RISK_CLASSES | limits.ALWAYS_APPROVED_RISK_CLASSES
):
    raise RuntimeError("RiskClass does not cover the hard-ceiling risk classes")

_R = RiskClass
_REASON_RISKS: Final = MappingProxyType(
    {
        ReasonCode.READ_ALLOWED: {_R.READ},
        ReasonCode.MODIFY_APPROVAL_REQUIRED: {_R.MODIFY},
        ReasonCode.MODIFY_ALLOWED_BY_SETTINGS: {_R.MODIFY},
        ReasonCode.DANGEROUS_APPROVAL_REQUIRED: {_R.DANGEROUS},
        ReasonCode.RISKY_SESSION_APPROVAL_REQUIRED: {_R.READ, _R.MODIFY},
        ReasonCode.FORBIDDEN_RISK_CLASS: {_R.FORBIDDEN},
    }
)


def _dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, **kwargs)


class FrozenDict(dict[str, Any]):
    """Immutable dict subclass (still isinstance(dict) and JSON-serializable)."""

    def _blocked(self, *_: Any, **__: Any) -> Any:
        raise TypeError("FrozenDict is immutable")

    __setitem__ = __delitem__ = __ior__ = _blocked
    update = pop = popitem = clear = setdefault = _blocked

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: Any) -> FrozenDict:
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("args contain an unencodable string") from exc
        return value
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("args contain a non-finite float")
        return value
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("args keys must be str")
        return FrozenDict({_freeze(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    raise ValueError(f"args are not JSON-serializable: {type(value).__name__}")


class _Validated(BaseModel):
    """Base: no validation bypass through model_copy/model_construct."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def model_construct(cls, *_: Any, **__: Any) -> Any:
        raise TypeError(f"{cls.__name__}.model_construct bypasses validation")

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        data = {name: getattr(self, name) for name in type(self).model_fields}
        data.update(update or {})
        return type(self).model_validate(data)


class ActionRequest(_Validated):
    """One tool call proposed by the model or the fast-path router."""

    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = Field(min_length=1)
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    risk_class: RiskClass
    args: dict[str, Any] = Field(default_factory=dict)
    paths: tuple[str, ...] = ()
    provenance: frozenset[ProvenanceFlag] = frozenset()
    summary: str = Field(default="", max_length=500)

    _canonical: str = PrivateAttr(default="")
    _fingerprint: str = PrivateAttr(default="")

    @field_validator("args", mode="after")
    @classmethod
    def _freeze_args(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _freeze(value)

    @model_validator(mode="after")
    def _cache(self) -> Self:
        self._canonical = _dumps(self.args, sort_keys=True, separators=(",", ":"))
        payload = _dumps(
            [self.run_id, self.tool, self.risk_class.value, self._canonical, list(self.paths)]
        )
        self._fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self

    def canonical_args(self) -> str:
        return self._canonical

    def fingerprint(self) -> str:
        return self._fingerprint


class Decision(_Validated):
    """Gate outcome; inconsistent combinations are unrepresentable."""

    verdict: Verdict
    reason: ReasonCode
    risk_class: RiskClass
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_channel: ApprovalChannel | None = None
    detail: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.verdict is not REASON_VERDICT[self.reason]:
            raise ValueError("verdict does not match reason")
        allowed = _REASON_RISKS.get(self.reason)
        if allowed is not None and self.risk_class not in allowed:
            raise ValueError("risk_class does not match reason")
        if self.verdict is Verdict.NEEDS_APPROVAL:
            if self.approval_channel not in (ApprovalChannel.VOICE, ApprovalChannel.TEXT):
                raise ValueError("needs_approval requires a voice or text channel")
        elif self.approval_channel is not None:
            raise ValueError("approval_channel only allowed with needs_approval")
        if self.risk_class is RiskClass.FORBIDDEN and self.verdict is not Verdict.DENY:
            raise ValueError("forbidden risk class must be denied")
        if self.risk_class is RiskClass.DANGEROUS and self.verdict is Verdict.ALLOW:
            raise ValueError("dangerous risk class cannot be allowed")
        return self
