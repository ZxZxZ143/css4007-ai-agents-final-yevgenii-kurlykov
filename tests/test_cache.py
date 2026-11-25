import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from deskpilot import cache as cache_mod
from deskpilot.cache import (
    CandidateStep,
    RecipeCache,
    cacheability,
    context_fingerprint,
    normalize_goal,
)
from deskpilot.policy.types import RiskClass
from deskpilot.settings import Approvals, Settings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def step(**kw: Any) -> CandidateStep:
    base: dict[str, Any] = {
        "tool": "files.read",
        "args": {"path": "a.txt"},
        "risk_class": RiskClass.READ,
        "ok": True,
        "verified": True,
        "tainted": False,
    }
    return CandidateStep(**{**base, **kw})


GOOD = [step(), step(tool="files.write", risk_class=RiskClass.MODIFY)]


def make(tmp_path: Path, clock: Clock, **kw: Any) -> RecipeCache:
    params: dict[str, Any] = {"ttl_s": 100.0, "max_entries": 10, "clock": clock}
    return RecipeCache(tmp_path / "c" / "recipes.json", **{**params, **kw})


def test_normalization() -> None:
    assert normalize_goal("  Открой   ЁЛКУ!? ") == normalize_goal("открой елку")
    assert normalize_goal("Open  the Browser...") == "open the browser"
    assert normalize_goal("ｏｐｅｎ") == "open"
    assert normalize_goal("удали a.txt") != normalize_goal("удали b.txt")
    assert normalize_goal("delete 1.txt") != normalize_goal("delete 2.txt")


def test_fingerprint(tmp_path: Path) -> None:
    base = Settings(allowed_roots=(tmp_path,), sandbox_dir=tmp_path / "sb")
    fp = context_fingerprint(base, "v1")
    assert fp == context_fingerprint(
        Settings(allowed_roots=(tmp_path,), sandbox_dir=tmp_path / "sb"), "v1"
    )
    other = [
        context_fingerprint(base.model_copy(update={"allowed_roots": (tmp_path / "x",)}), "v1"),
        context_fingerprint(base.model_copy(update={"app_allowlist": ("notepad",)}), "v1"),
        context_fingerprint(base.model_copy(update={"approvals": Approvals(modify="none")}), "v1"),
        context_fingerprint(base, "v2"),
    ]
    assert fp not in other
    assert len(set(other)) == 4


def test_cacheable_ok() -> None:
    assert cacheability(GOOD) == (True, "ok")


@pytest.mark.parametrize(
    ("steps", "reason"),
    [
        ([], "empty"),
        ([step()] * 21, "too many"),
        ([step(ok=False)], "failed"),
        ([step(verified=False)], "unverified"),
        ([step(tainted=True)], "tainted"),
        ([step(risk_class=RiskClass.DANGEROUS)], "risk"),
        ([step(risk_class=RiskClass.FORBIDDEN)], "risk"),
        ([step(fast_path=True)], "fast-path"),
        ([step(args={"x": object()})], "JSON"),
        ([step(args={"x": float("nan")})], "JSON"),
        ([step(args={"x": "y" * 70_000})], "large"),
        ([step(args={"text": "key sk-abcdefghijklmnopqrstuvwxyz"})], "secret"),
        ([step(args={"password": "hunter22"})], "secret"),
    ],
)
def test_cacheability_rejections(steps: list[CandidateStep], reason: str) -> None:
    ok, why = cacheability(steps)
    assert not ok
    assert reason in why


def test_large_but_many_small_args_rejected() -> None:
    steps = [step(args={"k": "z" * 1900, "k2": "z" * 1900}) for _ in range(20)]
    ok, why = cacheability(steps)
    assert not ok
    assert "large" in why


def test_round_trip_and_replace(tmp_path: Path) -> None:
    clock = Clock()
    c = make(tmp_path, clock)
    assert c.lookup("g", "ctx") is None
    recipe = c.store("Open Notepad!", "ctx", GOOD)
    assert recipe is not None
    assert len(recipe.steps) == 2
    again = make(tmp_path, clock)
    found = again.lookup("open notepad", "ctx")
    assert found == recipe
    assert again.lookup("open notepad", "other") is None
    clock.now += 1
    second = again.store("open notepad", "ctx", GOOD[:1])
    assert second is not None
    assert len(again) == 1
    assert len(again.lookup("open notepad", "ctx").steps) == 1  # type: ignore[union-attr]


def test_store_rejected_writes_nothing(tmp_path: Path) -> None:
    c = make(tmp_path, Clock())
    assert c.store("g", "ctx", [step(tainted=True)]) is None
    assert len(c) == 0
    assert not (tmp_path / "c").exists()


def test_ttl_expiry(tmp_path: Path) -> None:
    clock = Clock()
    c = make(tmp_path, clock)
    c.store("g", "ctx", GOOD)
    clock.now += 100
    assert c.lookup("g", "ctx") is not None
    clock.now += 1
    assert c.lookup("g", "ctx") is None
    assert len(c) == 1
    c.store("h", "ctx", GOOD)
    assert len(c) == 1
    assert c.lookup("g", "ctx") is None


def test_lru_eviction(tmp_path: Path) -> None:
    clock = Clock()
    c = make(tmp_path, clock, max_entries=2)
    a = c.store("a", "x", GOOD)
    clock.now += 1
    b = c.store("b", "x", GOOD)
    clock.now += 1
    assert a is not None
    assert b is not None
    c.mark_used(a.key)
    clock.now += 1
    c.store("c", "x", GOOD)
    assert len(c) == 2
    assert c.lookup("a", "x") is not None
    assert c.lookup("b", "x") is None
    assert c.lookup("c", "x") is not None


def test_mark_used_and_invalidate(tmp_path: Path) -> None:
    clock = Clock()
    c = make(tmp_path, clock)
    r = c.store("g", "x", GOOD)
    assert r is not None
    clock.now += 5
    c.mark_used(r.key)
    used = make(tmp_path, clock).lookup("g", "x")
    assert used is not None
    assert used.hits == 1
    assert used.last_used_at == 1005.0
    assert c.invalidate(r.key, "replay failed")
    assert not c.invalidate(r.key, "again")
    assert c.lookup("g", "x") is None
    assert len(make(tmp_path, clock)) == 0
    c.store("g", "x", GOOD)
    c.clear()
    assert len(make(tmp_path, clock)) == 0


@pytest.mark.parametrize(
    "content",
    [
        "not json {",
        json.dumps({"version": 2, "entries": []}),
        json.dumps([1, 2]),
        json.dumps({"version": 1, "entries": "x"}),
        "x" * (5 * 1024 * 1024 + 1),
    ],
)
def test_bad_files_yield_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "c" / "recipes.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    c = RecipeCache(path, ttl_s=100, max_entries=5)
    assert len(c) == 0
    assert c.lookup("g", "x") is None


def test_partially_invalid_entries_dropped(tmp_path: Path) -> None:
    clock = Clock()
    c = make(tmp_path, clock)
    r = c.store("g", "x", GOOD)
    assert r is not None
    path = tmp_path / "c" / "recipes.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    forged = {**data["entries"][0], "goal": "other"}
    data["entries"] += [{"junk": 1}, forged, "str"]
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = make(tmp_path, clock)
    assert len(loaded) == 1
    assert loaded.lookup("g", "x") == r


def test_atomic_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    c = make(tmp_path, clock)
    c.store("g", "x", GOOD)
    path = tmp_path / "c" / "recipes.json"
    before = path.read_text(encoding="utf-8")

    def boom(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with caplog.at_level(logging.WARNING, logger=cache_mod.log.name):
        assert c.store("h", "x", GOOD) is None
    assert "write failed" in caplog.text
    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob("*.tmp")) == []
    assert len(c) == 1
    monkeypatch.undo()
    assert len(make(tmp_path, clock)) == 1


def _entry(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    c = make(tmp_path, Clock())
    assert c.store("g", "x", GOOD) is not None
    path = tmp_path / "c" / "recipes.json"
    return path, json.loads(path.read_text(encoding="utf-8"))["entries"][0]


def _bad_steps() -> dict[str, dict[str, Any]]:
    ok = {"tool": "files.read", "args": {}, "risk_class": "read"}
    return {
        "dangerous": {"steps": [{**ok, "risk_class": "dangerous"}]},
        "forbidden": {"steps": [{**ok, "risk_class": "forbidden"}]},
        "secret": {"steps": [{**ok, "args": {"t": "sk-abcdefghijklmnopqrstuvwxyz"}}]},
        "21 steps": {"steps": [ok] * 21},
        "empty": {"steps": []},
        "huge": {"steps": [{**ok, "args": {"k": "y" * 70_000}}]},
        "negative hits": {"hits": -1},
        "nan time": {"created_at": float("nan")},
        "negative time": {"last_used_at": -5},
    }


@pytest.mark.parametrize("name", list(_bad_steps()))
def test_tampered_entries_rejected_on_load(tmp_path: Path, name: str) -> None:
    path, entry = _entry(tmp_path)
    forged = {**entry, **_bad_steps()[name]}
    path.write_text(json.dumps({"version": 1, "entries": [forged]}), encoding="utf-8")
    c = make(tmp_path, Clock())
    assert len(c) == 0
    assert c.lookup("g", "x") is None


def test_deeply_nested_entry_dropped(tmp_path: Path) -> None:
    path, entry = _entry(tmp_path)
    deep: Any = "x"
    for _ in range(500):
        deep = [deep]
    entry["steps"][0]["args"] = {"d": deep}
    path.write_text(json.dumps({"version": 1, "entries": [entry]}), encoding="utf-8")
    assert len(make(tmp_path, Clock())) == 0


def test_trailing_punctuation_rules() -> None:
    assert normalize_goal("открой блокнот!") == normalize_goal("открой блокнот")
    assert normalize_goal("открой report.") == normalize_goal("открой report")
    assert normalize_goal("удали a.txt.") != normalize_goal("удали a.txt")
    assert normalize_goal("открой C:/dir/.") != normalize_goal("открой C:/dir/")
    assert normalize_goal("открой C:\\dir\\.") != normalize_goal("открой C:\\dir\\")
