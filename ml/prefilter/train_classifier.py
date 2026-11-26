"""Train the pre-filter classifier and export it as JSON (no pickle).

Run from the repository root: ``uv run python ml/prefilter/train_classifier.py``.

Honest-evaluation protocol:
- Train rows that equal or nearly equal (difflib ratio >= 0.90) a holdout text are dropped.
  This is the only use of holdout texts before the final evaluation.
- C and both thresholds are chosen on out-of-fold predictions of 5-fold stratified CV over the
  de-leaked training rows (seed 4007). The holdout is never used for any choice.
- The holdout is evaluated exactly once, at the end of ``main``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import FeatureUnion, Pipeline

from deskpilot.prefilter.classifier import CLASSES, FORMAT_VERSION, PrefilterClassifier, preprocess

ROOT = Path(__file__).resolve().parents[2]
TRAIN_PATH = ROOT / "data" / "prefilter" / "train.jsonl"
HOLDOUT_PATH = ROOT / "data" / "prefilter" / "holdout.jsonl"
MODEL_PATH = ROOT / "src" / "deskpilot" / "prefilter" / "model" / "classifier.json"
METRICS_PATH = ROOT / "ml" / "prefilter" / "metrics.json"
METRICS_MD_PATH = ROOT / "ml" / "prefilter" / "METRICS.md"

SEED = 4007
C_GRID = (1.0, 3.0, 10.0, 30.0, 100.0)
CV_FOLDS = 5
WORD_MAX_FEATURES = 15000
CHAR_MAX_FEATURES = 30000
SIMILARITY_LIMIT = 0.90
MAX_FALSE_REFUSAL = 0.01
MIN_FLAG_RECALL = 0.95
REFUSE_FLOOR = 0.5  # never refuse below an argmax-level probability
FLAG_CAP = 0.5  # never require more than an even chance to flag
PARITY_ROWS = 200
TOKEN_PATTERN = r"(?u)\b\w\w+\b"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def data_sha256(path: Path) -> str:
    """Hash with normalized line endings so it is stable across checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def drop_overlaps(
    train: list[dict[str, Any]], holdout: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Drop train rows equal (after normalization) or >= 0.90 similar to a holdout text."""
    targets = sorted({preprocess(r["text"]) for r in holdout})
    kept: list[dict[str, Any]] = []
    for row in train:
        norm = preprocess(row["text"])
        matcher = SequenceMatcher(autojunk=False)
        matcher.set_seq2(norm)
        leaked = False
        for target in targets:
            if target == norm:
                leaked = True
                break
            matcher.set_seq1(target)
            if (
                matcher.real_quick_ratio() >= SIMILARITY_LIMIT
                and matcher.quick_ratio() >= SIMILARITY_LIMIT
                and matcher.ratio() >= SIMILARITY_LIMIT
            ):
                leaked = True
                break
        if not leaked:
            kept.append(row)
    return kept, len(train) - len(kept)


def make_pipeline(c: float) -> Pipeline:
    word = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        sublinear_tf=True,
        min_df=2,
        preprocessor=preprocess,
        token_pattern=TOKEN_PATTERN,
        max_features=WORD_MAX_FEATURES,
    )
    char = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        sublinear_tf=True,
        min_df=2,
        preprocessor=preprocess,
        max_features=CHAR_MAX_FEATURES,
    )
    clf = LogisticRegression(C=c, class_weight="balanced", max_iter=5000, random_state=SEED)
    return Pipeline([("features", FeatureUnion([("word", word), ("char", char)])), ("clf", clf)])


def out_of_fold(texts: list[str], labels: np.ndarray, c: float) -> np.ndarray:
    probs = np.zeros((len(texts), len(CLASSES)))
    folds = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=SEED)
    arr = np.array(texts, dtype=object)
    for fit_idx, val_idx in folds.split(arr, labels):
        pipe = make_pipeline(c).fit(arr[fit_idx].tolist(), labels[fit_idx])
        probs[val_idx] = pipe.predict_proba(arr[val_idx].tolist())
    return probs


def choose_thresholds(probs: np.ndarray, labels: np.ndarray) -> dict[str, float]:
    """Selection rule (on out-of-fold validation predictions only).

    refuse_threshold: smallest t in [0.50, 0.99] (step 0.01) with p(malicious) >= t refusing at
    most 1% of safe rows; the smallest such t maximizes malicious recall.
    flag_threshold: largest t in [0.01, 0.50] (step 0.01) such that p(risky)+p(malicious) >= t
    flags at least 95% of risky+malicious rows; the largest t minimizes safe rows flagged.
    """
    mal, safe = CLASSES.index("malicious"), CLASSES.index("safe")
    is_safe = labels == "safe"
    refuse = next(
        t / 100
        for t in range(int(REFUSE_FLOOR * 100), 100)
        if np.mean(probs[is_safe, mal] >= t / 100) <= MAX_FALSE_REFUSAL
    )
    flag_score = 1.0 - probs[:, safe]
    flag = max(
        (
            t / 100
            for t in range(1, int(FLAG_CAP * 100) + 1)
            if np.mean(flag_score[~is_safe] >= t / 100) >= MIN_FLAG_RECALL
        ),
        default=0.01,
    )
    return {"refuse_threshold": refuse, "flag_threshold": flag}


def rounded(values: Any) -> list[Any]:
    return np.round(np.asarray(values, dtype=float), 8).tolist()


def fit_final(texts: list[str], labels: np.ndarray, c: float) -> Pipeline:
    return make_pipeline(c).fit(texts, labels)


def export_model(
    pipe: Pipeline, thresholds: dict[str, float], training: dict[str, Any]
) -> dict[str, Any]:
    union: FeatureUnion = pipe.named_steps["features"]
    clf: LogisticRegression = pipe.named_steps["clf"]
    assert tuple(clf.classes_) == CLASSES
    vectorizers = []
    for name, vec in union.transformer_list:
        vectorizers.append(
            {
                "name": name,
                "analyzer": vec.analyzer,
                "ngram_range": list(vec.ngram_range),
                "token_pattern": vec.token_pattern if vec.analyzer == "word" else None,
                "sublinear_tf": vec.sublinear_tf,
                "norm": "l2",
                "terms": vec.get_feature_names_out().tolist(),
                "idf": rounded(vec.idf_),
            }
        )
    return {
        "format_version": FORMAT_VERSION,
        "preprocess": "nfkc-lower-yo-punct-space-v1",
        "classes": list(CLASSES),
        "vectorizers": vectorizers,
        "coef": [rounded(row) for row in clf.coef_],
        "intercept": rounded(clf.intercept_),
        "thresholds": thresholds,
        "training": training,
    }


def dump_model(model: dict[str, Any]) -> str:
    return json.dumps(model, ensure_ascii=False, separators=(",", ":")) + "\n"


def prepare_training() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    train = read_jsonl(TRAIN_PATH)
    kept, dropped = drop_overlaps(train, read_jsonl(HOLDOUT_PATH))
    info = {
        "train_sha256": data_sha256(TRAIN_PATH),
        "rows_total": len(train),
        "rows_dropped_overlap": dropped,
        "rows_used": len(kept),
        "label_counts": dict(sorted(Counter(r["label"] for r in kept).items())),
    }
    return kept, info


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def rate(hits: int, total: int) -> dict[str, Any]:
    return {
        "k": hits,
        "n": total,
        "rate": round(hits / total, 4) if total else None,
        "wilson95": wilson(hits, total),
    }


def decisions(probs: np.ndarray, labels: np.ndarray, thresholds: dict[str, float]) -> dict:
    mal, safe = CLASSES.index("malicious"), CLASSES.index("safe")
    refused = probs[:, mal] >= thresholds["refuse_threshold"]
    flagged = refused | ((1.0 - probs[:, safe]) >= thresholds["flag_threshold"])
    is_mal, is_safe = labels == "malicious", labels == "safe"
    return {
        "malicious_refused": rate(int(refused[is_mal].sum()), int(is_mal.sum())),
        "safe_wrongly_refused": rate(int(refused[is_safe].sum()), int(is_safe.sum())),
        "risky_malicious_flagged": rate(int(flagged[~is_safe].sum()), int((~is_safe).sum())),
        "safe_wrongly_flagged": rate(int(flagged[is_safe].sum()), int(is_safe.sum())),
    }


def class_metrics(probs: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    pred = np.array(CLASSES, dtype=object)[probs.argmax(axis=1)]
    prec, rec, f1, support = precision_recall_fscore_support(
        labels, pred, labels=list(CLASSES), zero_division=0
    )
    return {
        "per_class": {
            c: {
                "precision": round(float(prec[i]), 4),
                "recall": round(float(rec[i]), 4),
                "f1": round(float(f1[i]), 4),
                "support": int(support[i]),
            }
            for i, c in enumerate(CLASSES)
        },
        "macro_f1": round(float(f1_score(labels, pred, average="macro")), 4),
        "confusion_matrix": {
            "labels_true_rows_pred_cols": list(CLASSES),
            "matrix": confusion_matrix(labels, pred, labels=list(CLASSES)).tolist(),
        },
    }


def evaluate_holdout(
    pipe: Pipeline, holdout: list[dict[str, Any]], thresholds: dict[str, float]
) -> dict[str, Any]:
    texts = [r["text"] for r in holdout]
    labels = np.array([r["label"] for r in holdout], dtype=object)
    langs = np.array([r["lang"] for r in holdout], dtype=object)
    probs = pipe.predict_proba(texts)
    pred = np.array(CLASSES, dtype=object)[probs.argmax(axis=1)]
    result = class_metrics(probs, labels)
    result["n"] = len(holdout)
    result["decisions_overall"] = decisions(probs, labels, thresholds)
    result["decisions_by_lang"] = {
        lang: decisions(probs[langs == lang], labels[langs == lang], thresholds)
        for lang in sorted(set(langs))
    }
    result["errors"] = [
        {"id": r["id"], "true": r["label"], "predicted": str(p), "text": r["text"][:80]}
        for r, p in zip(holdout, pred, strict=True)
        if r["label"] != p
    ][:20]
    result["n_errors_total"] = int((labels != pred).sum())
    return result


def parity_fixture(pipe: Pipeline, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    step = max(1, len(rows) // PARITY_ROWS)
    chosen = rows[::step][:PARITY_ROWS]
    probs = pipe.predict_proba([r["text"] for r in chosen])
    return [
        {"id": r["id"], "probs": np.round(p, 9).tolist()}
        for r, p in zip(chosen, probs, strict=True)
    ]


def render_markdown(m: dict[str, Any]) -> str:
    d, v, h = m["data"], m["validation"], m["holdout"]
    t = m["thresholds"]
    lines = [
        "# Pre-filter classifier metrics",
        "",
        "Generated by `ml/prefilter/train_classifier.py`. Do not edit by hand.",
        "",
        "## Caveats",
        "",
        "- Training data is synthetic (template-generated, see `data/prefilter/DATASET_CARD.md`).",
        f"- The holdout is AI-written, hand-curated and small (n = {h['n']}). Confidence",
        "  intervals are wide; treat all numbers as indicative, not as a guarantee.",
        "- The classifier only refuses or flags. It never grants permission; the action policy",
        "  gate remains the security boundary.",
        "- The trained refuse_threshold is informational. The runtime decision uses the constants",
        "  in `src/deskpilot/prefilter/decide.py` (see the combined section below).",
        "",
        "## Data and protocol",
        "",
        f"- Train rows: {d['rows_total']}; dropped as holdout overlap (normalized equal or "
        f"difflib ratio >= 0.90): **{d['rows_dropped_overlap']}**; used: {d['rows_used']} "
        f"{d['label_counts']}.",
        f"- Holdout rows: {h['n']} (evaluated exactly once, after all choices were frozen).",
        f"- Choices (C, thresholds) use out-of-fold predictions of {CV_FOLDS}-fold stratified CV "
        f"(seed {SEED}) on the de-leaked train rows. The final model is refit on all of them.",
        f"- C grid {list(C_GRID)}, chosen C = **{m['chosen_c']}** (best CV macro F1).",
        f"- Features: word 1-2 grams (max {WORD_MAX_FEATURES}) + char_wb 2-5 grams "
        f"(max {CHAR_MAX_FEATURES}), min_df=2, sublinear TF, l2 per vectorizer.",
        "",
        "## Thresholds",
        "",
        f"- `refuse_threshold` = **{t['refuse_threshold']}** on p(malicious): smallest value in "
        f"[0.50, 0.99] (step 0.01) refusing <= 1% of safe validation rows.",
        f"- `flag_threshold` = **{t['flag_threshold']}** on p(risky)+p(malicious): largest value "
        f"in [0.01, 0.50] flagging >= 95% of risky+malicious validation rows.",
        "- A request is flagged when it is refused or reaches the flag threshold.",
        "- A value equal to the floor (0.50) or cap (0.50) means that guard was binding: the "
        "validation rows alone would have allowed a looser value.",
        "",
        "## Validation (out-of-fold)",
        "",
        f"- Macro F1 by C: {m['cv_macro_f1_by_c']}",
        f"- At the chosen C: macro F1 {v['macro_f1']}; per class {v['per_class']}",
        f"- Decisions: {_fmt_decisions(v['decisions_overall'])}",
        "",
        "## Holdout",
        "",
        f"- Macro F1: **{h['macro_f1']}**; errors: {h['n_errors_total']} of {h['n']}.",
        "",
        "| class | precision | recall | F1 | support |",
        "|---|---|---|---|---|",
    ]
    for c, s in h["per_class"].items():
        lines.append(f"| {c} | {s['precision']} | {s['recall']} | {s['f1']} | {s['support']} |")
    lines += [
        "",
        "Confusion matrix (rows = true, columns = predicted, order "
        f"{h['confusion_matrix']['labels_true_rows_pred_cols']}):",
        "",
    ]
    lines += [f"    {row}" for row in h["confusion_matrix"]["matrix"]]
    lines += ["", "### Decision metrics at the thresholds (rate, k/n, Wilson 95% CI)", ""]
    lines.append(
        "| split | malicious refused | safe wrongly refused | risky+mal flagged "
        "| safe wrongly flagged |"
    )
    lines.append("|---|---|---|---|---|")
    splits = {"all": h["decisions_overall"], **h["decisions_by_lang"]}
    for name, dec in splits.items():
        cells = " | ".join(_fmt_rate(dec[k]) for k in _DECISION_KEYS)
        lines.append(f"| {name} | {cells} |")
    lines += ["", f"### Holdout errors (up to 20 of {h['n_errors_total']})", ""]
    lines += [
        f"- {e['id']}: true={e['true']}, predicted={e['predicted']}: {e['text']}"
        for e in h["errors"]
    ]
    lines += ["", "## Changes after a holdout run", "", "None: the holdout was evaluated once.", ""]
    return "\n".join(lines)


BEGIN_MARK = "<!-- R-03:BEGIN -->"
END_MARK = "<!-- R-03:END -->"
FOREIGN_SECTION = "## Combined pre-filter (R-04)"


def write_markdown(own: str) -> None:
    """Write the R-03 part between markers and keep every other section of METRICS.md."""
    rest = ""
    if METRICS_MD_PATH.exists():
        current = METRICS_MD_PATH.read_text(encoding="utf-8")
        if END_MARK in current:
            rest = current.split(END_MARK, 1)[1].strip("\n")
        elif FOREIGN_SECTION in current:  # file written before the markers existed
            rest = current[current.index(FOREIGN_SECTION) :].strip("\n")
    text = f"{BEGIN_MARK}\n{own.strip(chr(10))}\n{END_MARK}\n"
    if rest:
        text += "\n" + rest + "\n"
    METRICS_MD_PATH.write_text(text, encoding="utf-8", newline="\n")


_DECISION_KEYS = (
    "malicious_refused",
    "safe_wrongly_refused",
    "risky_malicious_flagged",
    "safe_wrongly_flagged",
)


def _fmt_rate(r: dict[str, Any]) -> str:
    if r["rate"] is None:
        return "n/a"
    lo, hi = r["wilson95"]
    return f"{r['rate'] * 100:.1f}% ({r['k']}/{r['n']}, {lo * 100:.1f}-{hi * 100:.1f})"


def _fmt_decisions(dec: dict[str, Any]) -> str:
    return "; ".join(f"{k} {_fmt_rate(dec[k])}" for k in _DECISION_KEYS)


def main() -> None:
    rows, data_info = prepare_training()
    texts = [r["text"] for r in rows]
    labels = np.array([r["label"] for r in rows], dtype=object)

    cv: dict[float, np.ndarray] = {c: out_of_fold(texts, labels, c) for c in C_GRID}
    cv_f1 = {
        c: round(
            float(
                f1_score(labels, np.array(CLASSES, dtype=object)[p.argmax(axis=1)], average="macro")
            ),
            4,
        )
        for c, p in cv.items()
    }
    best_c = max(C_GRID, key=lambda c: (cv_f1[c], -c))
    thresholds = choose_thresholds(cv[best_c], labels)

    pipe = fit_final(texts, labels, best_c)
    model = export_model(pipe, thresholds, {**data_info, "c": best_c})
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(dump_model(model), encoding="utf-8", newline="\n")

    runtime = PrefilterClassifier.load(MODEL_PATH)
    parity = parity_fixture(pipe, rows)
    by_id = {r["id"]: r["text"] for r in rows}
    max_diff = max(
        abs(runtime.classify(by_id[p["id"]]).probs[c] - p["probs"][i])
        for p in parity
        for i, c in enumerate(CLASSES)
    )
    assert max_diff < 1e-6, f"pure-Python parity broken: {max_diff}"

    # Final, single holdout evaluation. Nothing above may depend on it.
    holdout = evaluate_holdout(pipe, read_jsonl(HOLDOUT_PATH), thresholds)

    validation = class_metrics(cv[best_c], labels)
    validation["decisions_overall"] = decisions(cv[best_c], labels, thresholds)
    metrics = {
        "data": data_info,
        "chosen_c": best_c,
        "cv_macro_f1_by_c": {str(c): f for c, f in cv_f1.items()},
        "thresholds": thresholds,
        "validation": validation,
        "holdout": holdout,
        "model_file_sha256": hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest(),
        "model_file_bytes": MODEL_PATH.stat().st_size,
        "parity_max_abs_diff": max_diff,
        "parity_sklearn_probs": parity,
    }
    METRICS_PATH.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    write_markdown(render_markdown(metrics))
    print(json.dumps({k: metrics[k] for k in ("chosen_c", "thresholds", "model_file_bytes")}))
    print("holdout macro F1", holdout["macro_f1"], holdout["decisions_overall"])


if __name__ == "__main__":
    main()
