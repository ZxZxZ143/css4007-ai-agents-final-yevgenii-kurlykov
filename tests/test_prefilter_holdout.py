"""Checks for the hand-written pre-filter holdout set."""

import json
import re
from collections import Counter
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "prefilter"
HOLDOUT = DATA_DIR / "holdout.jsonl"
CARD = DATA_DIR / "HOLDOUT.md"

LABELS = {"safe", "risky", "malicious"}
LANGS = {"ru", "en", "mixed"}


def _load() -> list[dict[str, str]]:
    lines = HOLDOUT.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    return _load()


def test_file_is_lf_only() -> None:
    assert b"\r" not in HOLDOUT.read_bytes()


def test_schema(rows: list[dict[str, str]]) -> None:
    for row in rows:
        assert set(row) <= {"id", "text", "label", "lang", "note"}
        assert {"id", "text", "label", "lang"} <= set(row)
        assert re.fullmatch(r"ho-\d{4}", row["id"])
        assert row["label"] in LABELS
        assert row["lang"] in LANGS
        assert isinstance(row["text"], str) and row["text"].strip()
        assert 1 <= len(row["text"].split()) <= 40
        assert isinstance(row.get("note", ""), str)


def test_unique_ids_and_texts(rows: list[dict[str, str]]) -> None:
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    texts = [_normalize(r["text"]) for r in rows]
    assert len(set(texts)) == len(texts)


def test_size_and_shares(rows: list[dict[str, str]]) -> None:
    total = len(rows)
    assert 300 <= total <= 360
    labels = Counter(r["label"] for r in rows)
    assert 0.35 <= labels["safe"] / total <= 0.45
    assert 0.25 <= labels["risky"] / total <= 0.35
    assert 0.25 <= labels["malicious"] / total <= 0.35
    langs = Counter(r["lang"] for r in rows)
    assert langs["ru"] / total >= 0.55
    assert langs["en"] / total >= 0.25
    assert langs["mixed"] > 0


def test_style_coverage(rows: list[dict[str, str]]) -> None:
    def tagged(tag: str) -> int:
        return sum(tag in r.get("note", "") for r in rows)

    plain = sum("typo" in r.get("note", "") or not re.search(r"[.,!?;:]", r["text"]) for r in rows)
    assert tagged("near-miss") >= 40
    assert plain >= 30
    assert tagged("voice") >= 15
    assert tagged("translit") >= 10


def test_holdout_card_counts_match(rows: list[dict[str, str]]) -> None:
    card = CARD.read_text(encoding="utf-8")
    documented = {
        key: int(value) for key, value in re.findall(r"^\| ([\w-]+) \| (\d+) \|$", card, re.M)
    }
    assert int(re.search(r"Total rows: (\d+)", card).group(1)) == len(rows)
    actual: Counter[str] = Counter()
    actual.update(r["label"] for r in rows)
    actual.update(r["lang"] for r in rows)
    for tag in ("near-miss", "voice", "translit", "typo"):
        actual[tag] = sum(tag in r.get("note", "") for r in rows)
    for key, value in actual.items():
        assert documented.get(key) == value, key
