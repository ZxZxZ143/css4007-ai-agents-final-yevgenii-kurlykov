"""Tests for the generated pre-filter training set (R-02a)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ml" / "prefilter" / "generate_dataset.py"
TRAIN = ROOT / "data" / "prefilter" / "train.jsonl"
CARD = ROOT / "data" / "prefilter" / "DATASET_CARD.md"
LABELS = {"safe", "risky", "malicious"}
ALLOWED_HOSTS = {
    "example.com",
    "github.com",
    "python.org",
    "wikipedia.org",
    "docs.python.org",
    "stackoverflow.com",
    "microsoft.com",
}


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    lines = TRAIN.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _generate(out: Path) -> bytes:
    subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)], check=True, capture_output=True
    )
    return out.read_bytes()


def test_generator_is_deterministic_and_matches_committed(tmp_path: Path) -> None:
    first = _generate(tmp_path / "a.jsonl")
    second = _generate(tmp_path / "b.jsonl")
    assert first == second
    assert first == TRAIN.read_bytes().replace(b"\r\n", b"\n")  # tolerate autocrlf checkouts
    assert b"\r" not in first


def test_schema(rows: list[dict]) -> None:
    keys = {"id", "text", "label", "lang", "family", "noise"}
    for row in rows:
        assert set(row) == keys
        assert row["label"] in LABELS
        assert row["lang"] in {"ru", "en"}
        assert row["text"].strip()
        assert isinstance(row["noise"], list)
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"tr-\d{6}", i) for i in ids)


def test_no_duplicate_normalized_texts(rows: list[dict]) -> None:
    texts = [_normalize(r["text"]) for r in rows]
    assert len(set(texts)) == len(texts)


def test_size_balance_and_language_share(rows: list[dict]) -> None:
    assert 2400 <= len(rows) <= 4000
    for label, count in Counter(r["label"] for r in rows).items():
        assert 0.25 <= count / len(rows) <= 0.45, label
    assert sum(r["lang"] == "ru" for r in rows) / len(rows) >= 0.5


def test_family_coverage(rows: list[dict]) -> None:
    families: dict[str, set[str]] = {label: set() for label in LABELS}
    for row in rows:
        families[row["label"]].add(row["family"])
    assert len(families["malicious"]) >= 12
    assert len(families["risky"]) >= 8
    assert len(families["safe"]) >= 10
    assert "safe_hardneg_words" in families["safe"]
    assert "mal_indirect" in families["malicious"]
    assert "risky_settings" in families["risky"]
    card = CARD.read_text(encoding="utf-8")
    for names in families.values():
        assert all(f"`{name}`" in card for name in names)


def test_noise_types_present(rows: list[dict]) -> None:
    seen = {n for r in rows for n in r["noise"]}
    assert {"typo_swap", "typo_drop", "no_punct", "yo", "translit", "mixed_case"} <= seen


def test_no_real_urls_or_secrets(rows: list[dict]) -> None:
    for row in rows:
        text = row["text"].lower()
        for host in re.findall(r"https?://([^/\s]+)", text):
            assert host in ALLOWED_HOSTS
        for domain in re.findall(r"[\w.]+@([\w.]+)", text):
            assert domain.rstrip(".!") == "example.com"
        assert not re.search(r"sk-[a-z0-9]{10,}|password\s*[:=]", text)
