# DeskPilot functional roadmap

Milestones are ordered by dependency, not by date. The orchestrator splits each task into micro-tasks (`AGENTS.md` §3). Only fixed course dates matter: paper selection by **December 6** and final submission by **December 22, 2026 23:59 UTC+05**.

Statuses: TODO, IN_PROGRESS, BLOCKED, REVIEW, DONE. DONE requires verified acceptance criteria (`AGENTS.md` §6). **Par** = tasks with the same letter in one milestone can run in parallel once their dependencies are done.

## Open decisions

| ID | Decision | Needed before | Status |
|---|---|---|---|
| D-01 | Instructor agreement on file-system scope and the restricted command layer (A.md forbids *unrestricted* shell) | SY-03, final scope of SY-01 | open — student talks to instructor |
| D-02 | Free speech-to-text engine with Russian streaming support | V-02 | open — research in V-01 |
| D-03 | Web search API provider (free tier) | B-02 | open |
| D-04 | Integration method for the user's own browser (extension vs. CDP) | B-05 | open |
| D-05 | Final project name (working name: DeskPilot) | final report | open |

## M0 — Foundation

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| F-01 | Python project skeleton: `uv`, Python 3.12, `src/deskpilot`, pytest, lint, CLI entry point, Python CI job next to the course job | — | — | `uv sync --locked`, `uv run pytest` and CI green; course checks unchanged | DONE (2026-11-25, CI run 45570618449 green on windows-latest for 9b5be4c) |
| F-02 | Settings model and loader (TOML, Pydantic, hard maxima clamp, safe defaults) | F-01 | a | Invalid settings rejected with clear errors; values above hard maxima clamped; hard-deny list not configurable | DONE (local, 2026-11-25) |
| F-03 | Trace recorder: JSONL, run/event/operation IDs, redaction, bounded fields | F-01 | a | Events correlate model call → tool call → decision → result; secrets redacted; tests | DONE (local, 2026-11-25) |
| F-04 | Model client: configurable OpenAI model via `langchain-openai`, timeouts, typed API errors, fake model for tests | F-01 | a | Offline tests with fake model; missing key → controlled error; one live tool call recorded as evidence | DONE (2026-11-25; live tool call by gpt-5.6-luna via Responses API, evidence in `.private/evidence/f04-live-tool-call.json`) |

## M1 — Action policy gate

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| P-01 | Core policy types: `ActionRequest`, `RiskClass`, `Decision`, reason codes | F-01 | — | Strict models, exhaustive enums, tests | DONE (2026-11-25, CI run 45651579196 green for e89d3bc) |
| P-02 | Path policy: zones, env expansion, UNC/device rejection, real-path resolution (symlink, junction, 8.3, `..`), hard deny | P-01, F-02 | b | Attack-path test table passes (traversal, junction into Windows, 8.3 alias, case tricks, UNC) on Windows | DONE (2026-11-25, CI run 45651579196 green for e89d3bc) |
| P-03 | Approval service: action-bound, single-use, expiry, text channel, voice-channel interface with challenge code | P-01 | b | Changed args, reuse, expiry and wrong run all rejected; tests | DONE (2026-11-25, CI run 45651579196 green for e89d3bc) |
| P-04 | Budgets and limits: steps, calls, retries, file ops, time, tokens | P-01, F-02 | b | Exhaustion stops dispatch with a stop reason; tests | DONE (2026-11-25, CI run 45651579196 green for e89d3bc) |
| P-05 | Gate integration: `evaluate(action) → allow / needs_approval / deny`, settings-driven channel per risk class, risky-session flag | P-02, P-03, P-04, P-06, F-03 | — | Decision matrix tests per risk class; every decision traced; `dangerous` never auto-allowed | DONE (2026-11-25, CI run 45651579196 green for e89d3bc) |
| P-06 | Fast-path router: simple single-intent "open" requests (allowlisted app, known folder, explicit safe path, http/https URL; RU/EN) become a deterministic `ActionRequest` without a model call; everything else goes to the model | P-01 | b | Positive/negative routing table passes (compound, unknown app, executables, non-http schemes, injection text → model); output is a gate-ready `ActionRequest`; no model budget spent | DONE (2026-11-25, CI run 45651579196 green for e89d3bc) |

## M2 — Agent core

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| A-01 | Adapter SDK v0: tool declaration (args/result models, risk, paths, mock, verify), registry | P-01 | — | Registry exposes tools to the model; conformance checks enforced at registration | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-02 | LangGraph loop: plan → act → gate → execute → observe → assess → replan/verify/report, explicit typed state | F-04, P-05, A-01 | — | Scripted fake-model runs show branching on observations, not a fixed sequence | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-03 | Untrusted-content wrapping: bounds, provenance, observation IDs | A-02 | c | Injection text in observations cannot change settings or satisfy approval; tests | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-04 | Failure recovery: typed tool errors, transient retry, fallback, safe stop | A-02 | c | Repeated failure stops after configured retries with evidence (G2) | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-05 | Outcome verification and final report: completed / partial / failed / unverified | A-02 | c | Claimed effects re-checked by verifiers; unverifiable → `unverified` | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-06 | Text CLI: run a goal, approve in console, print trace summary, `--sandbox` | A-02, P-03 | c | End-to-end run against mock adapters from the CLI | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-07 | Recipe cache: a repeated goal replays its stored, verified tool-call sequence without a model call (added 2026-11-25 at the user's request) | A-01, A-02, A-05 | — | Second identical goal runs with 0 model calls and still passes the gate; tainted, dangerous, failed or unverified runs are never stored; a failed replay invalidates the entry and falls back to the model | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |
| A-08 | Model tiers: fast default model, strong model for hard goals via a rule-based start or a model-requested escalation tool (added 2026-11-25 at the user's request) | F-04, A-02 | — | Code-like goal starts on the strong tier; the fast model's escalation call switches tier once; repeated failures auto-escalate; strong-call budget enforced; every switch traced | LOCAL OK (2026-11-25; reviewed, 863 tests; awaiting commit + CI) |

## M3 — Request pre-filter

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| R-01 | Lexicon + synonyms (RU/EN) + fuzzy matching | F-01 | d | Typos and word forms of dangerous intents detected; tests | TODO |
| R-02 | Dataset: generated training set + hand-written holdout, labels safe/risky/malicious, dataset card | — | d | Versioned data files; holdout never used for training | TODO |
| R-03 | Classifier (TF-IDF + logistic regression), threshold selection, metrics report | R-02 | — | Precision/recall per class on holdout recorded; model file reproducible from script | TODO |
| R-04 | Pre-filter integration before the graph: refuse / flag / pass, never grant | R-01, R-03, A-02 | — | Malicious request refused before any tool call; risky flag forces approval for `modify` | TODO |
| R-05 | Optional: Isolation Forest over action-sequence features for mid-run anomaly flags | A-02, F-03 | — | Anomalous synthetic sequences flagged; false-positive rate on normal runs recorded | TODO |

## M4 — Adapter SDK v1 and tooling

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| S-01 | Scaffold generator `deskpilot adapter new <name>` with template tool, mock and tests | A-01 | e | Generated adapter passes conformance kit out of the box | TODO |
| S-02 | Conformance test kit: strict schemas, declared risk, mock present, gate called, bounded output | A-01 | e | Kit fails on a deliberately broken adapter | TODO |
| S-03 | `docs/ADAPTER_SDK.md`: how to build, test and register an adapter | S-01, S-02 | — | A new adapter can be built by following only this guide | TODO |

## M5 — System adapters

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| SY-01 | Files: list, search, read text (bounded), create folder/file, write, rename, move, copy, delete to Recycle Bin | P-05, S-02 | f | All tools zone-checked for source and destination; sandbox tests | TODO |
| SY-02 | Apps: open/focus/close from allowlist | S-02 | f | Non-allowlisted app denied; tests with mock process layer | TODO |
| SY-03 | Command layer: PowerShell AST parse, classification, timeout, output bound | P-05, S-02, D-01 | f | Forbidden-command test table denied before execution; unknown → approval | TODO |
| SY-04 | System info: processes, disk space, network, battery (read-only) | SY-03 or psutil | — | Read-only, bounded output | TODO |
| SY-05 | Multi-step file scenario end to end (e.g. collect this week's PDFs into a folder and write a list) | SY-01, A-06 | — | Real-model run in sandbox: plan, approval, retry on locked file, verified outcome | TODO |

## M6 — Browser and search

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| B-01 | Managed Playwright browser: navigate (http/https), read text, list/follow links, back, keep tab | S-02 | g | Non-http schemes denied; page text bounded and untrusted | TODO |
| B-02 | Web search via API + local mock search provider | S-02, D-03 | g | Mock provider used in tests; API key only from `.env` | TODO |
| B-03 | Local HTML fixture site: solution, no-solution, injection, broken pages | — | g | Served locally in tests | TODO |
| B-04 | "Find the fix and leave the page open" scenario with replanning | B-01, B-02, B-03, A-02 | — | Agent skips the no-solution page, ignores the injection, ends on the solution page (G1 evidence) | TODO |
| B-05 | User's own browser adapter (existing tabs) | B-01, D-04 | — | Every action `dangerous`; research result recorded | TODO |

## M7 — Voice

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| V-01 | Research: free STT with Russian streaming, latency and CPU on the target laptop | — | h | Recommendation with measurements → D-02 | TODO |
| V-02 | Audio capture + push-to-talk + streaming transcript into the session | V-01 | — | Recorded audio fixtures transcribed in tests | TODO |
| V-03 | Wake phrase detection | V-02 | i | False-activation rate measured on background audio | TODO |
| V-04 | Voice approval channel: challenge code, confidence threshold, text fallback | V-02, P-03 | i | Wrong code, low confidence and timeout never approve | TODO |
| V-05 | Optional spoken responses (TTS) | V-02 | i | Can be disabled in settings | TODO |

## M8 — Desktop UI

| ID | Task | Depends on | Par | Acceptance | Status |
|---|---|---|---|---|---|
| U-01 | Tray application lifecycle and minimal settings window | F-02 | j | Starts, stops, edits settings safely | TODO |
| U-02 | Island window states: listening, transcript, executing, awaiting approval, result, error; animations | U-01 | — | Visual states demonstrable with a scripted event stream | TODO |
| U-03 | Session event bus wiring UI ↔ agent | U-02, A-02 | — | UI shows real run progress; no policy logic in UI | TODO |
| U-04 | Approval UI (expand, summary, approve/deny, voice) | U-03, V-04 | — | Approval only for the displayed action; timeout denies | TODO |
| U-05 | Global hotkey and emergency stop | U-03 | — | Stop halts dispatch within one step and is traced | TODO |

## M9 — Additional adapters (on demand, one at a time)

| ID | Task | Depends on | Acceptance | Status |
|---|---|---|---|---|
| X-01 | Windows UI Automation adapter (experimental) | S-03 | Passes conformance kit; actions on non-allowlisted windows denied | TODO |
| X-nn | Further adapters requested by the user | S-03 | Conformance kit + scenario test | TODO |

## M10 — Course deliverables and evaluation

| ID | Task | Depends on | Acceptance | Status |
|---|---|---|---|---|
| C-01 | Select eligible research paper (released June 22 – December 22, 2026) and fill `reports/paper-selection.md` | — | Metadata verified; **due December 6** | TODO |
| C-02 | Test suite: ≥12 application tests (target 16) incl. G1–G4, mapped in the proposal | A-04, P-05, B-04 | pytest + CI green | TODO |
| C-03 | Real-model evaluation runs on scenario set, metrics with denominators | SY-05, B-04 | Raw evidence stored, failures kept | TODO |
| C-04 | Live demo script and rehearsal (voice + text fallback) | C-03, U-04 | Timed rehearsal recorded | TODO |
| C-05 | Research presentation (5–7 min) | C-01 | Rehearsed | TODO |
| C-06 | Weekly reports, final report, AI-use disclosure, submission | all | Course validators + exact-SHA green CI | TODO |

## Minimum path to Level A

If time runs short, protect this chain: F-01 → F-04 → P-01…P-05 → A-01 → A-02…A-06 → SY-01 → B-01…B-04 → C-01…C-03. Voice (M7) and UI (M8) are the showcase and come after the text agent passes the guardrail tests; the text CLI remains the fallback for the defense.
