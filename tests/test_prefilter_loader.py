"""Classifier model loader fails closed on malformed or hostile files."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from deskpilot.prefilter.classifier import (
    MAX_MODEL_BYTES,
    ClassifierModelError,
    PrefilterClassifier,
)


def tiny() -> dict[str, Any]:
    return {
        "format_version": 1,
        "classes": ["malicious", "risky", "safe"],
        "vectorizers": [
            {"analyzer": "word", "ngram_range": [1, 1], "terms": ["a", "b"], "idf": [1.0, 2.0]}
        ],
        "coef": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
        "intercept": [0.0, 0.0, 0.0],
        "thresholds": {"refuse_threshold": 0.5, "flag_threshold": 0.5},
    }


def write(tmp_path: Path, data: dict[str, Any] | str) -> Path:
    path = tmp_path / "m.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def mutated(change: Any) -> dict[str, Any]:
    data = copy.deepcopy(tiny())
    change(data)
    return data


def test_tiny_model_loads(tmp_path: Path) -> None:
    assert PrefilterClassifier.load(write(tmp_path, tiny())).classify("a b").label


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["coef"][0].__setitem__(0, 1e999),
        lambda d: d["intercept"].__setitem__(1, 1e999),
        lambda d: d["vectorizers"][0]["idf"].__setitem__(0, 1e999),
        lambda d: d["thresholds"].__setitem__("flag_threshold", 1e999),
        lambda d: d["vectorizers"][0]["idf"].__setitem__(0, 0.0),
        lambda d: d["vectorizers"][0]["idf"].__setitem__(1, -1.0),
        lambda d: d["vectorizers"][0]["terms"].__setitem__(0, 5),
        lambda d: d["vectorizers"][0].__setitem__("ngram_range", [1.0, 2]),
        lambda d: d["vectorizers"][0].__setitem__("ngram_range", [True, 2]),
        lambda d: d.__setitem__("format_version", True),
        lambda d: d.__setitem__("format_version", 1.0),
        lambda d: d["coef"][0].__setitem__(0, "0.1"),
        lambda d: d["coef"][0].__setitem__(0, None),
        lambda d: d["thresholds"].__setitem__("refuse_threshold", True),
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, change: Any) -> None:
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(write(tmp_path, mutated(change)))


@pytest.mark.parametrize("raw", ["NaN", "[NaN]", '{"format_version": Infinity}', "[" * 100000, ""])
def test_hostile_json_is_wrapped(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(write(tmp_path, raw))


def test_nan_inside_the_model_is_rejected(tmp_path: Path) -> None:
    raw = json.dumps(tiny()).replace("0.1", "NaN", 1)
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(write(tmp_path, raw))


def test_oversized_file_is_refused_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * (MAX_MODEL_BYTES + 1))
    with pytest.raises(ClassifierModelError, match="too large"):
        PrefilterClassifier.load(path)


def test_missing_file_is_wrapped(tmp_path: Path) -> None:
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(tmp_path / "nope.json")
