"""User settings: TOML file, Pydantic validation, clamping to hard maxima."""

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from deskpilot.defaults import DEFAULT_MODEL_ID, DEFAULT_STRONG_MODEL_ID
from deskpilot.limits import HARD_MAX

logger = logging.getLogger(__name__)

# Keys that try to configure code-level ceilings; rejected at any nesting level.
_CEILING_KEYS = frozenset(
    {"hard_deny", "denylist", "deny", "forbidden", "forbidden_commands", "hard_limits"}
)

_DEFAULT_ROOT_NAMES = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")


class SettingsError(Exception):
    """Invalid settings file or values. The message lists one problem per line."""


class SettingsWarning(BaseModel):
    """A budget value that was clamped to its hard maximum."""

    model_config = ConfigDict(frozen=True)

    field: str
    given: int
    clamped_to: int


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Approvals(_Model):
    modify: Literal["voice", "text", "none"] = "text"
    dangerous: Literal["voice", "text"] = "text"

    @field_validator("dangerous", mode="before")
    @classmethod
    def _dangerous_never_none(cls, value: object) -> object:
        if value == "none":
            raise ValueError("dangerous actions always require approval; 'none' is not allowed")
        return value


class Budgets(_Model):
    steps: StrictInt = Field(30, gt=0)
    model_calls: StrictInt = Field(40, gt=0)
    tool_calls: StrictInt = Field(60, gt=0)
    retries_per_operation: StrictInt = Field(2, ge=0)
    file_ops_per_run: StrictInt = Field(100, gt=0)
    browser_tabs: StrictInt = Field(5, gt=0)
    command_timeout_s: StrictInt = Field(30, gt=0)
    run_seconds: StrictInt = Field(600, gt=0)
    tokens: StrictInt = Field(100_000, gt=0)
    strong_model_calls: StrictInt = Field(5, gt=0)


class CacheSettings(_Model):
    enabled: bool = True
    ttl_days: StrictInt = Field(14, ge=1, le=90)
    max_entries: StrictInt = Field(200, ge=1, le=1000)


def _default_allowed_roots() -> tuple[Path, ...]:
    home = Path.home()
    return tuple(home / name for name in _DEFAULT_ROOT_NAMES)


def _check_path(path: Path) -> None:
    text = str(path)
    if text.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError(f"device paths are not allowed: {text}")
    if text.startswith(("\\\\", "//")):
        raise ValueError(f"UNC paths are not allowed: {text}")
    if not path.is_absolute():
        raise ValueError(f"path must be absolute: {text}")
    if ".." in path.parts:
        raise ValueError(f"'..' components are not allowed: {text}")


class Settings(_Model):
    model_id: str = Field(DEFAULT_MODEL_ID, min_length=1)
    strong_model_id: str = Field(DEFAULT_STRONG_MODEL_ID, min_length=1)
    language: Literal["ru", "en"] = "ru"
    allowed_roots: tuple[Path, ...] = Field(default_factory=_default_allowed_roots)
    sandbox_dir: Path | None = None
    app_allowlist: tuple[str, ...] = ("notepad", "calc", "explorer")
    approvals: Approvals = Field(default_factory=Approvals)
    budgets: Budgets = Field(default_factory=Budgets)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    stt_engine: str | None = None
    wake_phrase: str = Field("deskpilot", min_length=1)
    browser: Literal["managed"] = "managed"

    @field_validator("allowed_roots")
    @classmethod
    def _roots_are_safe(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        for root in roots:
            _check_path(root)
            if root == Path(root.anchor):
                raise ValueError(f"drive roots are not allowed: {root}")
        return roots

    @field_validator("sandbox_dir")
    @classmethod
    def _sandbox_is_safe(cls, path: Path | None) -> Path | None:
        if path is not None:
            _check_path(path)
            if path == Path(path.anchor):
                raise ValueError(f"drive roots are not allowed: {path}")
        return path

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Settings":
        return _build(data)[0]


class LoadedSettings(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    settings: Settings
    warnings: tuple[SettingsWarning, ...] = ()
    source: Path | None = None


def default_settings_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "DeskPilot" / "settings.toml"


def _find_ceiling_keys(data: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            path = f"{prefix}{key}"
            if str(key).lower().replace("-", "_") in _CEILING_KEYS:
                found.append(
                    f"{path}: hard-deny and forbidden lists are code constants "
                    "and cannot be configured"
                )
            found.extend(_find_ceiling_keys(value, f"{path}."))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(_find_ceiling_keys(item, f"{prefix}[{index}]."))
    return found


def _clamp_budgets(data: dict[str, Any]) -> tuple[dict[str, Any], list[SettingsWarning]]:
    budgets = data.get("budgets")
    if not isinstance(budgets, Mapping):
        return data, []
    clamped = dict(budgets)
    warnings: list[SettingsWarning] = []
    for name, value in budgets.items():
        limit = HARD_MAX.get(name)
        if limit is not None and type(value) is int and value > limit:
            clamped[name] = limit
            warnings.append(SettingsWarning(field=f"budgets.{name}", given=value, clamped_to=limit))
    return {**data, "budgets": clamped}, warnings


def _build(data: Mapping[str, Any]) -> tuple[Settings, tuple[SettingsWarning, ...]]:
    errors = _find_ceiling_keys(data)
    if errors:
        raise SettingsError("\n".join(errors))
    clamped, warnings = _clamp_budgets(dict(data))
    try:
        settings = Settings.model_validate(clamped)
    except ValidationError as exc:
        lines = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise SettingsError("\n".join(lines)) from exc
    for warning in warnings:
        logger.warning(
            "setting %s=%d exceeds hard maximum, clamped to %d",
            warning.field,
            warning.given,
            warning.clamped_to,
        )
    return settings, tuple(warnings)


def load_settings(path: Path | None = None) -> LoadedSettings:
    """Load settings from `path` (or the default location). Missing file gives defaults."""
    target = path if path is not None else default_settings_path()
    if not target.is_file():
        return LoadedSettings(settings=Settings(), source=None)
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{target}: invalid TOML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(f"{target}: cannot read settings file: {exc}") from exc
    try:
        settings, warnings = _build(data)
    except SettingsError as exc:
        raise SettingsError(f"{target}:\n{exc}") from exc
    return LoadedSettings(settings=settings, warnings=warnings, source=target)
