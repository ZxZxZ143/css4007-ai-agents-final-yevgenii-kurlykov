# Pre-filter holdout set

File: `data/prefilter/holdout.jsonl` (UTF-8, LF, one JSON object per line).

## Authorship

Written on 2026-11-26 by an AI assistant subagent (Claude Sonnet) working under the project orchestrator. Every request was written individually, not produced from templates or code, and independently of the generator that builds the training set. Only the row order was shuffled (fixed seed) and the ids assigned by a script.

## Rules

- Labels: `malicious` (damages the system or security, or steals/exfiltrates secrets), `risky` (legitimate but consequential user-data or system actions), `safe` (everything else).
- Borderline items follow these definitions; the `note` field holds a short reason or tag.
- Request styles: casual and formal, typos, missing punctuation, voice-transcript style, Russian typed in Latin letters, mixed RU/EN, indirect or polite phrasing, context before the request, and near-misses (a harmless request that shares words with a dangerous one, or the reverse).
- Names, files and addresses are invented. No real secrets or personal data.
- Tags in `note`: `near-miss`, `voice`, `translit`, `typo`.

## Counts

Total rows: 357

| Label | Rows |
|---|---|
| safe | 142 |
| risky | 104 |
| malicious | 111 |

| Lang | Rows |
|---|---|
| ru | 204 |
| en | 104 |
| mixed | 49 |

| Tag | Rows |
|---|---|
| near-miss | 44 |
| voice | 20 |
| translit | 27 |
| typo | 9 |

Transliterated Russian rows carry `lang: ru`. Rows with no punctuation at all are common; together with the typo rows they exceed 30.

## Usage rule

This set is never used for training, feature selection or threshold selection. It is read only to report final metrics of the pre-filter classifier. If a classifier change was informed by looking at holdout errors, the set is spent and must be replaced.

## Known limits

- Still AI-written: phrasing is more uniform than real user logs, and the author shares biases with the model family used elsewhere in the project.
- Small: per-class confidence intervals are wide.
- Label boundaries between risky and malicious (for example formatting a removable drive, or showing saved browser passwords) are judgement calls recorded in `note`.
- The student may add their own items later in a separate file; this file stays frozen.
