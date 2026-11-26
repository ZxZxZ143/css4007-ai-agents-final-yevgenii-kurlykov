"""Offline tests for the pre-filter classifier (pure-Python inference over the exported JSON)."""

from __future__ import annotations

import copy
import importlib.util
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deskpilot.prefilter.classifier import (
    CLASSES,
    ClassifierModelError,
    PrefilterClassifier,
    load_default,
    preprocess,
)

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "src" / "deskpilot" / "prefilter" / "model" / "classifier.json"
METRICS_PATH = ROOT / "ml" / "prefilter" / "metrics.json"
TRAIN_PATH = ROOT / "data" / "prefilter" / "train.jsonl"
SCRIPT_PATH = ROOT / "ml" / "prefilter" / "train_classifier.py"

HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None


def _model() -> dict[str, Any]:
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_default_model_loads_and_validates() -> None:
    clf = load_default()
    assert load_default() is clf
    assert 0.0 < clf.refuse_threshold <= 1.0
    assert 0.0 < clf.flag_threshold <= 1.0
    assert _model()["classes"] == list(CLASSES)


def test_preprocess_normalizes() -> None:
    assert preprocess("  Ёлка,  ОТКРОЙ!!  Блокнот.exe ") == "елка открой блокнот exe"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format_version=99),
        lambda d: d.pop("coef"),
        lambda d: d["coef"][0].pop(),
        lambda d: d["coef"].pop(),
        lambda d: d["vectorizers"][0]["idf"].pop(),
        lambda d: d["vectorizers"][0].update(analyzer="nope"),
        lambda d: d.update(classes=["a", "b", "c"]),
        lambda d: d["thresholds"].update(refuse_threshold=7),
    ],
)
def test_malformed_model_fails_closed(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object]
) -> None:
    data = copy.deepcopy(_model())
    mutate(data)
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(_write(tmp_path, data))


def test_unreadable_and_non_json_files_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(bad)
    with pytest.raises(ClassifierModelError):
        PrefilterClassifier.load(_write(tmp_path, [1, 2]))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("открой блокнот", "safe"), ("отформатируй диск C", "malicious")],
)
def test_obvious_cases(text: str, expected: str) -> None:
    result = load_default().classify(text)
    assert result.label == expected
    assert abs(sum(result.probs.values()) - 1.0) < 1e-9
    assert result.malicious_score == result.probs["malicious"]


def test_risky_request_is_flagged_or_risky() -> None:
    clf = load_default()
    result = clf.classify("удали папку загрузки")
    flagged = (1.0 - result.probs["safe"]) >= clf.flag_threshold
    assert result.label in {"risky", "malicious"} or flagged


def test_long_input_is_bounded_and_fast() -> None:
    clf = load_default()
    text = "открой блокнот " * 700
    start = time.perf_counter()
    result = clf.classify(text)
    assert time.perf_counter() - start < 0.5
    assert result == clf.classify(text[:2000])


def test_empty_and_unknown_text_do_not_crash() -> None:
    clf = load_default()
    for text in ("", "   ", "🙂🙂", "zzzzqqqq"):
        assert abs(sum(clf.classify(text).probs.values()) - 1.0) < 1e-9


def test_load_and_classify_speed() -> None:
    start = time.perf_counter()
    clf = PrefilterClassifier.load()
    clf.classify("открой блокнот")
    assert time.perf_counter() - start < 1.0
    start = time.perf_counter()
    for _ in range(50):
        clf.classify("хочу, чтобы ты удалил папку загрузки и очистил корзину")
    assert (time.perf_counter() - start) / 50 < 0.02


def test_metrics_record_holdout_and_overlap() -> None:
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    assert metrics["data"]["rows_dropped_overlap"] >= 0
    assert set(metrics["holdout"]["per_class"]) == set(CLASSES)
    assert metrics["model_file_sha256"]


@pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")
def test_pure_python_matches_sklearn_probabilities() -> None:
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    fixture = metrics["parity_sklearn_probs"]
    assert len(fixture) >= 150
    texts = {
        row["id"]: row["text"]
        for row in map(json.loads, TRAIN_PATH.read_text(encoding="utf-8").splitlines())
    }
    clf = load_default()
    for item in fixture:
        probs = clf.classify(texts[item["id"]]).probs
        for i, name in enumerate(CLASSES):
            assert probs[name] == pytest.approx(item["probs"][i], abs=1e-6)


@pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")
def test_training_is_reproducible_byte_for_byte() -> None:
    import numpy as np

    spec = importlib.util.spec_from_file_location("train_classifier", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    rows, info = script.prepare_training()
    committed = _model()
    labels = np.array([r["label"] for r in rows], dtype=object)
    pipe = script.fit_final([r["text"] for r in rows], labels, committed["training"]["c"])
    rebuilt = script.export_model(
        pipe, committed["thresholds"], {**info, "c": committed["training"]["c"]}
    )
    assert script.dump_model(rebuilt) == MODEL_PATH.read_text(encoding="utf-8")
