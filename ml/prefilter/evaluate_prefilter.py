"""Evaluate the combined pre-filter on the holdout, once, next to its two parts.

Run from the repository root: ``uv run python ml/prefilter/evaluate_prefilter.py``.
The decision constants live in ``deskpilot.prefilter.decide`` and were fixed by design before
this run; nothing here tunes them. Writes ``combined_metrics.json`` and (re)writes the
"Combined pre-filter (R-04)" section at the end of ``METRICS.md``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskpilot.prefilter import evaluate, scan, strongest
from deskpilot.prefilter.classifier import PrefilterClassifier, load_default
from deskpilot.prefilter.decide import REFUSE_CLASSIFIER_ONLY, REFUSE_WITH_LEXICON

ROOT = Path(__file__).resolve().parents[2]
HOLDOUT_PATH = ROOT / "data" / "prefilter" / "holdout.jsonl"
JSON_PATH = ROOT / "ml" / "prefilter" / "combined_metrics.json"
MD_PATH = ROOT / "ml" / "prefilter" / "METRICS.md"
SECTION = "## Combined pre-filter (R-04)"

Verdict = str  # "refuse" | "flag" | "pass"
METRICS = (
    ("malicious_refused", "malicious refused"),
    ("safe_wrongly_refused", "safe wrongly refused"),
    ("flagged_or_refused_unsafe", "risky+malicious flagged or refused"),
    ("safe_wrongly_flagged_or_refused", "safe wrongly flagged or refused"),
    ("risky_wrongly_refused", "risky wrongly refused"),
)


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def lexicon_verdict(text: str) -> Verdict:
    label = strongest(scan(text))
    return "refuse" if label == "malicious" else "flag" if label == "risky" else "pass"


def classifier_verdict(model: PrefilterClassifier, text: str) -> Verdict:
    probs = model.classify(text).probs
    if probs["malicious"] >= model.refuse_threshold:
        return "refuse"
    return "flag" if probs["malicious"] + probs["risky"] >= model.flag_threshold else "pass"


def rates(rows: list[dict[str, Any]], verdicts: list[Verdict]) -> dict[str, dict[str, Any]]:
    pairs = list(zip((r["label"] for r in rows), verdicts, strict=True))

    def count(labels: tuple[str, ...], outcomes: tuple[str, ...]) -> tuple[int, int]:
        chosen = [v for label, v in pairs if label in labels]
        return sum(v in outcomes for v in chosen), len(chosen)

    both = ("refuse", "flag")
    cells = {
        "malicious_refused": count(("malicious",), ("refuse",)),
        "safe_wrongly_refused": count(("safe",), ("refuse",)),
        "flagged_or_refused_unsafe": count(("risky", "malicious"), both),
        "safe_wrongly_flagged_or_refused": count(("safe",), both),
        "risky_wrongly_refused": count(("risky",), ("refuse",)),
    }
    return {
        name: {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}
        for name, (k, n) in cells.items()
    }


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    model = load_default()
    systems: dict[str, Callable[[str], Verdict]] = {
        "lexicon_alone": lexicon_verdict,
        "classifier_alone": lambda text: classifier_verdict(model, text),
        "combined": lambda text: evaluate(text, model).verdict,
    }
    return {
        "holdout_rows": len(rows),
        "constants": {
            "refuse_with_lexicon": REFUSE_WITH_LEXICON,
            "refuse_classifier_only": REFUSE_CLASSIFIER_ONLY,
            "classifier_refuse_threshold": model.refuse_threshold,
            "classifier_flag_threshold": model.flag_threshold,
        },
        "systems": {
            name: rates(rows, [verdict(r["text"]) for r in rows])
            for name, verdict in systems.items()
        },
    }


def cell(m: dict[str, Any]) -> str:
    lo, hi = m["wilson95"]
    return f"{m['rate'] * 100:.1f}% ({m['k']}/{m['n']}, {lo * 100:.1f}-{hi * 100:.1f})"


def render(report: dict[str, Any]) -> str:
    systems = report["systems"]
    c = report["constants"]
    lines = [
        SECTION,
        "",
        f"Holdout rows: {report['holdout_rows']}. Cells: rate (k/n, Wilson 95% CI in %).",
        "",
        "| Metric | Lexicon alone | Classifier alone | Combined |",
        "|---|---|---|---|",
    ]
    for key, title in METRICS:
        row = [cell(systems[s][key]) for s in ("lexicon_alone", "classifier_alone", "combined")]
        lines.append(f"| {title} | " + " | ".join(row) + " |")
    lines += [
        "",
        "Rules. Lexicon alone: refuse = malicious, flag = risky. Classifier alone: refuse = "
        f"p(malicious) >= {c['classifier_refuse_threshold']}, flag = p(risky)+p(malicious) >= "
        f"{c['classifier_flag_threshold']}. Combined: refuse if (lexicon malicious and "
        f"p(malicious) >= {c['refuse_with_lexicon']}) or p(malicious) >= "
        f"{c['refuse_classifier_only']}; else flag if lexicon risky or malicious, or "
        "p(risky)+p(malicious) >= the flag threshold; else pass.",
        "",
        "Honesty note: the combined constants (0.25 and 0.85) were fixed by design before this "
        "run and were not tuned on the holdout. The holdout is small, AI-written and hand-curated "
        "(see Caveats), so the intervals are wide. The combined filter may refuse or flag a "
        "request; it never grants permission, and the action gate still checks every tool call.",
        "",
    ]
    return "\n".join(lines)


def write_section(text: str) -> None:
    current = MD_PATH.read_text(encoding="utf-8")
    head = current.split(SECTION)[0].rstrip("\n")
    MD_PATH.write_text(head + "\n\n" + text, encoding="utf-8", newline="\n")


def main() -> None:
    rows = [
        json.loads(line) for line in HOLDOUT_PATH.read_text(encoding="utf-8").splitlines() if line
    ]
    report = build_report(rows)
    JSON_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    write_section(render(report))
    print(render(report))


if __name__ == "__main__":
    main()
