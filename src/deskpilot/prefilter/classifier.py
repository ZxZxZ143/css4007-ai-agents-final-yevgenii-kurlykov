"""Pure-Python inference for the pre-filter classifier (TF-IDF + logistic regression).

The model is exported to JSON by ``ml/prefilter/train_classifier.py``. This module reproduces
scikit-learn's word and char_wb TF-IDF features and the linear scores with the stdlib only.
The classifier may refuse or flag a request; it never grants permission.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

FORMAT_VERSION = 1
MAX_INPUT_CHARS = 2000
CLASSES = ("malicious", "risky", "safe")
_TOKEN_PATTERN = re.compile(r"(?u)\b\w\w+\b")
_WHITESPACE = re.compile(r"\s+")

Label = Literal["safe", "risky", "malicious"]


class ClassifierModelError(ValueError):
    """The packaged or supplied model file is missing, malformed or inconsistent."""


def preprocess(text: str) -> str:
    """NFKC, lower case, yo -> ye, punctuation and symbols -> space, collapsed whitespace."""
    folded = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    spaced = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in folded)
    return _WHITESPACE.sub(" ", spaced).strip()


@dataclass(frozen=True)
class ClassifierResult:
    label: Label
    probs: Mapping[str, float]
    malicious_score: float


@dataclass(frozen=True)
class _Vectorizer:
    analyzer: str
    ngram_range: tuple[int, int]
    index: Mapping[str, int]
    idf: tuple[float, ...]
    offset: int

    def features(self, text: str) -> dict[int, float]:
        """Sublinear TF-IDF, l2-normalized, keyed by the global feature index."""
        counts = Counter(t for t in self._terms(text) if t in self.index)
        raw = {
            self.index[t]: (1.0 + math.log(c)) * self.idf[self.index[t]] for t, c in counts.items()
        }
        norm = math.sqrt(sum(v * v for v in raw.values()))
        if norm == 0.0:
            return {}
        return {self.offset + i: v / norm for i, v in raw.items()}

    def _terms(self, text: str) -> list[str]:
        low, high = self.ngram_range
        if self.analyzer == "word":
            tokens = _TOKEN_PATTERN.findall(text)
            terms = list(tokens) if low == 1 else []
            for n in range(max(low, 2), high + 1):
                terms.extend(" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
            return terms
        terms = []
        for word in text.split():
            padded = f" {word} "
            for n in range(low, high + 1):
                terms.extend(padded[i : i + n] for i in range(max(len(padded) - n + 1, 1)))
                if len(padded) <= n:
                    break
        return terms


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ClassifierModelError(message)


class PrefilterClassifier:
    """Linear classifier over word and char n-gram TF-IDF features."""

    def __init__(
        self,
        vectorizers: list[_Vectorizer],
        coef: list[list[float]],
        intercept: list[float],
        refuse_threshold: float,
        flag_threshold: float,
    ) -> None:
        self._vectorizers = vectorizers
        self._coef = coef
        self._intercept = intercept
        self.refuse_threshold = refuse_threshold
        self.flag_threshold = flag_threshold

    @classmethod
    def load(cls, path: Path | None = None) -> PrefilterClassifier:
        """Load and validate a model file; fail closed on anything malformed."""
        try:
            if path is None:
                resource = resources.files("deskpilot.prefilter") / "model" / "classifier.json"
                raw = resource.read_text(encoding="utf-8")
            else:
                raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ClassifierModelError(f"cannot read classifier model: {exc}") from exc
        try:
            return cls._from_dict(data)
        except ClassifierModelError:
            raise
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ClassifierModelError(f"malformed classifier model: {exc!r}") from exc

    @classmethod
    def _from_dict(cls, data: Any) -> PrefilterClassifier:
        _require(isinstance(data, dict), "model root must be an object")
        _require(
            data.get("format_version") == FORMAT_VERSION,
            f"unsupported format_version {data.get('format_version')!r}",
        )
        _require(tuple(data["classes"]) == CLASSES, "unexpected classes")
        vectorizers: list[_Vectorizer] = []
        offset = 0
        for spec in data["vectorizers"]:
            terms, idf = spec["terms"], spec["idf"]
            _require(len(terms) == len(idf) > 0, "terms and idf lengths differ")
            _require(spec["analyzer"] in ("word", "char_wb"), "unknown analyzer")
            low, high = spec["ngram_range"]
            _require(1 <= low <= high <= 8, "bad ngram_range")
            index = {t: i for i, t in enumerate(terms)}
            _require(len(index) == len(terms), "duplicate terms")
            vectorizers.append(
                _Vectorizer(spec["analyzer"], (low, high), index, tuple(map(float, idf)), offset)
            )
            offset += len(terms)
        coef = [[float(w) for w in row] for row in data["coef"]]
        intercept = [float(b) for b in data["intercept"]]
        _require(len(coef) == len(CLASSES) == len(intercept), "coef shape mismatch")
        _require(all(len(row) == offset for row in coef), "coef width != feature count")
        thresholds = data["thresholds"]
        refuse, flag = float(thresholds["refuse_threshold"]), float(thresholds["flag_threshold"])
        _require(0.0 < refuse <= 1.0 and 0.0 < flag <= 1.0, "thresholds out of range")
        return cls(vectorizers, coef, intercept, refuse, flag)

    def classify(self, text: str) -> ClassifierResult:
        clean = preprocess(text[:MAX_INPUT_CHARS])
        features: dict[int, float] = {}
        for vec in self._vectorizers:
            features.update(vec.features(clean))
        scores = [
            b + sum(w * row[i] for i, w in features.items())
            for row, b in zip(self._coef, self._intercept, strict=True)
        ]
        peak = max(scores)
        exps = [math.exp(s - peak) for s in scores]
        total = sum(exps)
        probs = {c: e / total for c, e in zip(CLASSES, exps, strict=True)}
        label: Label = max(probs, key=probs.__getitem__)  # type: ignore[assignment]
        return ClassifierResult(label=label, probs=probs, malicious_score=probs["malicious"])


@lru_cache(maxsize=1)
def load_default() -> PrefilterClassifier:
    """The packaged model, loaded once."""
    return PrefilterClassifier.load()
