"""Recipe cache: verified tool-call sequences keyed by normalized goal and context.

Pure store. Replayed actions are gated elsewhere; nothing here grants permission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deskpilot.policy.types import RiskClass
from deskpilot.redaction import redact
from deskpilot.settings import Settings

log = logging.getLogger(__name__)

MAX_STEPS = 20
MAX_RECIPE_BYTES = 64 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_VERSION = 1
_CACHEABLE_RISKS = frozenset({RiskClass.READ, RiskClass.MODIFY})
_SPACES = re.compile(r"\s+")
_TRAILING = ".!?…"


def normalize_goal(goal: str) -> str:
    text = unicodedata.normalize("NFKC", goal).casefold().replace("ё", "е")
    text = _SPACES.sub(" ", text).strip()
    # Sentence punctuation is dropped, but not after a file-name or path-like token
    # ("удали a.txt." keeps its dot; "открой report." equals "открой report").
    stripped = text.rstrip(_TRAILING + " ")
    last = stripped.rsplit(" ", 1)[-1]
    if any(ch in last for ch in "./\\"):
        return text
    return stripped


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def context_fingerprint(settings: Settings, registry_version: str) -> str:
    payload = {
        "allowed_roots": sorted(str(p).casefold() for p in settings.allowed_roots),
        "sandbox_dir": None if settings.sandbox_dir is None else str(settings.sandbox_dir),
        "app_allowlist": sorted(a.casefold() for a in settings.app_allowlist),
        "approvals": settings.approvals.model_dump(),
        "registry_version": registry_version,
    }
    return _sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _key(goal: str, context: str) -> str:
    return _sha256(f"{normalize_goal(goal)}\0{context}")


class RecipeStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    args: dict[str, Any]
    risk_class: RiskClass

    @field_validator("risk_class")
    @classmethod
    def _cacheable_risk(cls, value: RiskClass) -> RiskClass:
        if value not in _CACHEABLE_RISKS:
            raise ValueError("risk class not cacheable")
        return value


class CandidateStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    args: dict[str, Any]
    risk_class: RiskClass
    ok: bool
    verified: bool
    tainted: bool
    fast_path: bool = False


class Recipe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    goal: str
    context: str
    steps: tuple[RecipeStep, ...] = Field(min_length=1, max_length=MAX_STEPS)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    last_used_at: float = Field(ge=0, allow_inf_nan=False)
    hits: int = Field(default=0, ge=0)


def _normalized_args(args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        loaded = json.loads(json.dumps(args, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _recipe_problem(items: Sequence[tuple[RiskClass, dict[str, Any]]]) -> str | None:
    """Store-time rules shared by cacheability() and loading from disk."""
    if not items:
        return "empty recipe"
    if len(items) > MAX_STEPS:
        return "too many steps"
    if any(risk not in _CACHEABLE_RISKS for risk, _ in items):
        return "risk class not cacheable"
    normalized = [_normalized_args(args) for _, args in items]
    if any(a is None for a in normalized):
        return "args not JSON-serializable"
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_RECIPE_BYTES:
        return "recipe too large"
    try:
        if any(redact(a) != a for a in normalized):
            return "secret-looking content"
    except RecursionError:
        return "args not JSON-serializable"
    return None


def cacheability(steps: Sequence[CandidateStep]) -> tuple[bool, str]:
    problem = _recipe_problem([(s.risk_class, s.args) for s in steps])
    if problem is None:
        if any(not s.ok for s in steps):
            problem = "failed step"
        elif any(not s.verified for s in steps):
            problem = "unverified step"
        elif any(s.tainted for s in steps):
            problem = "tainted step"
        elif any(s.fast_path for s in steps):
            problem = "fast-path step"
    return (False, problem) if problem else (True, "ok")


class RecipeCache:
    def __init__(
        self,
        path: Path,
        *,
        ttl_s: float,
        max_entries: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, Recipe] = self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def lookup(self, goal: str, context: str) -> Recipe | None:
        with self._lock:
            recipe = self._entries.get(_key(goal, context))
            if recipe is None or self._expired(recipe):
                return None
            return recipe

    def mark_used(self, key: str) -> None:
        with self._lock:
            recipe = self._entries.get(key)
            if recipe is None or self._expired(recipe):
                return
            updated = recipe.model_copy(
                update={"hits": recipe.hits + 1, "last_used_at": self._clock()}
            )
            self._commit({**self._entries, key: updated})

    def store(self, goal: str, context: str, steps: Sequence[CandidateStep]) -> Recipe | None:
        ok, reason = cacheability(steps)
        if not ok:
            log.info("recipe not cached: %s", reason)
            return None
        key = _key(goal, context)
        now = self._clock()
        recipe = Recipe(
            key=key,
            goal=normalize_goal(goal),
            context=context,
            steps=tuple(
                RecipeStep(
                    tool=s.tool, args=_normalized_args(s.args) or {}, risk_class=s.risk_class
                )
                for s in steps
            ),
            created_at=now,
            last_used_at=now,
        )
        with self._lock:
            entries = {k: r for k, r in self._entries.items() if k != key}
            entries[key] = recipe
            return recipe if self._commit(entries) else None

    def invalidate(self, key: str, reason: str) -> bool:
        with self._lock:
            if key not in self._entries:
                return False
            log.info("recipe invalidated: %s", reason)
            return self._commit({k: r for k, r in self._entries.items() if k != key})

    def clear(self) -> None:
        with self._lock:
            self._commit({})

    def _expired(self, recipe: Recipe) -> bool:
        return self._clock() - recipe.created_at > self._ttl_s

    def _commit(self, entries: dict[str, Recipe]) -> bool:
        live = {k: r for k, r in entries.items() if not self._expired(r)}
        while len(live) > self._max_entries:
            del live[min(live, key=lambda k: live[k].last_used_at)]
        try:
            self._write(live)
        except OSError as exc:
            log.warning("recipe cache write failed: %s", exc)
            return False
        self._entries = live
        return True

    def _write(self, entries: dict[str, Recipe]) -> None:
        payload = {
            "version": FILE_VERSION,
            "entries": [r.model_dump(mode="json") for r in entries.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".recipes-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _load(self) -> dict[str, Recipe]:
        try:
            if self._path.stat().st_size > MAX_FILE_BYTES:
                log.warning("recipe cache file too large; ignored")
                return {}
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as exc:
            log.warning("recipe cache unreadable: %s", exc)
            return {}
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
            return {}
        raw = data.get("entries")
        if not isinstance(raw, list):
            return {}
        entries: dict[str, Recipe] = {}
        for item in raw:
            try:
                recipe = Recipe.model_validate(item)
                valid = recipe.key == _key(recipe.goal, recipe.context) and not _recipe_problem(
                    [(st.risk_class, st.args) for st in recipe.steps]
                )
            except Exception:  # noqa: BLE001 - any bad entry is dropped, never raised
                continue
            if valid:
                entries[recipe.key] = recipe
        return entries
