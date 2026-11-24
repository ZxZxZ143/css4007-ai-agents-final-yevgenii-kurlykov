# DeskPilot — project operating policy

This file is the standing instruction set for every agent session in this repository. Read it fully before doing anything else.

## 1. What we are building

**DeskPilot** is a voice- and text-controlled autonomous desktop agent for Windows (CSS 4007 final project, Level A). The user speaks or types a goal ("open the browser, find why `pip install` fails on Windows and leave the page with the fix open"). The agent plans, calls tools through adapters (system, files, commands, browser, apps), observes results, replans, verifies the outcome and reports. Every action passes a code-enforced policy gate. Consequential actions require action-bound approval by text or voice.

Owning documents:

| Document | Owns |
|---|---|
| `docs/PROJECT_MAP.md` | Current state, blockers, next action. Read second, every session |
| `docs/ROADMAP.md` | Functional milestones, tasks, dependencies, acceptance criteria, statuses |
| `docs/ARCHITECTURE.md` | Layers, policy model, adapter contract, technical decisions |
| `docs/orchestration-log.md` | Subagent runs, review verdicts and usage observations |
| `reports/proposal.md` | Course proposal (tools, evaluation plan, example requests) |
| `reports/week-NN.md` | Dated evidence for each official course week |

## 2. Session start checklist

1. Read this file, then `docs/PROJECT_MAP.md`.
2. Read the relevant milestone in `docs/ROADMAP.md` and the relevant part of `docs/ARCHITECTURE.md`.
3. Inspect `git status`, the current branch and recent commits. Preserve uncommitted work.
4. Pick the next unblocked task whose dependencies are DONE. If the user named a task, use that.

## 3. Roles and orchestration protocol

The main session is the **orchestrator and foreman**. It does not write feature code itself unless a change is trivial (a few lines) or a subagent failed twice on it.

1. **Plan.** Take a roadmap task, define the goal and split it into micro-tasks. Each micro-task has: an ID (`<task>-<n>`, e.g. `P-02.1`), owned files, explicit acceptance criteria, a verification command and a list of files it must not touch.
2. **Parallelize.** Micro-tasks with disjoint files and no data dependency run in parallel, each in its own git worktree (`isolation: "worktree"`). Dependent micro-tasks run in sequence. Never let two writers own the same file.
3. **Delegate.** Use the subagents in `.claude/agents/`, all on Sonnet:
   - `implementer` — writes code and its tests for one micro-task.
   - `reviewer` — independently checks one finished micro-task against its brief. Read-only.
   - `researcher` — read-only investigation of libraries, APIs or existing code. Writes no code.
4. **Language.** Briefs, messages to subagents and their reports are **in English**. The orchestrator talks to the user in **Russian**.
5. **Reports.** Every subagent ends with one JSON object matching `docs/agent-report.schema.json`, and nothing else. Keep it minimal: facts, paths, commands, counts.
6. **Review loop.** After an implementer reports `done`, run the `reviewer` on the same micro-task. The orchestrator then reads the reviewer verdict, spot-checks the diff and reruns the verification command. The orchestrator alone decides the final status.
   - `accept` → merge the worktree branch, mark the micro-task done.
   - `revise` → send the concrete findings back to the implementer (max two rounds), then take over or re-split.
   - `reject` → discard the worktree and re-plan.
7. **Usage tracking.** After each subagent run, append one line to `docs/orchestration-log.md` (task, agent, rounds, verdict, tokens/duration if the tool result reports them). If the reviewer step roughly doubles cost without catching real defects over several tasks, or usage grows fast, **tell the user in Russian** and propose a cheaper mode (e.g. review only policy/security code).

Brief template for subagents:

```
TASK <id>: <one-line goal>
CONTEXT: <2-5 lines: why, relevant architecture, links to files>
OWNED FILES: <paths you may create/modify>
DO NOT TOUCH: <paths>
ACCEPTANCE: <numbered, testable criteria>
VERIFY: <exact command(s)>
CONSTRAINTS: <policy rules relevant to this task>
REPORT: JSON per docs/agent-report.schema.json, nothing else.
```

## 4. Hard technical constraints

- Python 3.12+, `uv`, Pydantic v2, pytest. LangGraph orchestrates the agent loop. OpenAI GPT through `langchain-openai` selects tools. The model ID stays configurable (default `gpt-5.6-luna`). OpenAI Agents SDK is excluded.
- **Two policy layers.** (a) The request pre-filter (lexicon, fuzzy matching, supervised classifier) may block or flag a request but never grants permission. (b) The action policy gate checks **every** tool call in Python before execution. Prompts are never the security boundary.
- **Hard ceilings are code, not settings.** The system denylist (Windows, Program Files, ProgramData, AppData, credential and key files, browser profiles, registry, drive roots), forbidden command classes and mandatory approval for dangerous actions cannot be relaxed by configuration. Settings only adjust convenience inside those bounds.
- Paths are checked after real-path resolution (symlinks, junctions, 8.3 names, `..`, environment variables, UNC).
- Commands are parsed (PowerShell AST), classified and executed without shell string interpolation, with timeouts and bounded output. No `Invoke-Expression`, download-and-execute, registry edits, boot or security configuration.
- Approvals are bound to the exact action and arguments, single-use and expiring. Voice approval uses a random challenge code and falls back to text on low confidence.
- Tool output, web pages, file contents and command output are untrusted data. They are bounded, tagged with provenance and can never grant permission.
- Every run is bounded (steps, tool calls, retries, file operations, wall time, tokens) and ends with an explicit outcome: `completed`, `partial`, `failed` or `unverified`, plus a stop reason.
- Traces record goal, actions, validated arguments, policy decisions, outcomes, errors and stop reason. Never log secrets or private reasoning.
- Tests run offline against mock adapters, a sandbox directory and local HTML fixtures. Real desktop, browser and voice are for manual checks and the demo only.
- Secrets live in `.env` (never committed). Never put user data into external requests beyond what a tool explicitly needs.

## 5. Course rules

- Preserve instructor files: `README.md`, `A.md`, `C.md`, `D.md`, `templates/`, `scripts/`, `diagnostic/`, `examples/`, `tests/starter.test.mjs`, `project.config.json`, `.github/workflows/validate.yml` (only add jobs) and the report paths.
- Course checks: `npm test` and `npm run validate`. Python checks: `uv run pytest` once the project exists.
- Weekly reports record work in its actual window with real dates and commits. Label late or revised work. Leave future reports as templates. Never fabricate results, dates, citations or completion.
- The project topic changed on November 25, 2026 (Week 13) from AgentShield Lab to DeskPilot. The earlier work remains in Git history and in the Week 11–12 reports.
- Disclose AI assistance (Claude Code orchestrator and subagents) in reports.
- Hard dates: paper selection by December 6, final deadline December 22, 2026 23:59 UTC+05.

## 6. Definition of done

A task is DONE only when its acceptance criteria are verified: relevant tests pass locally, the reviewer verdict is `accept` (or the orchestrator documents why review was skipped), and for published code the CI run for that exact commit is green. Commit and push only when the user asks; push only to the student fork.

## 7. After work

1. Review the diff and run the applicable checks. Record the actual outcomes.
2. Update task statuses in `docs/ROADMAP.md`. Record changed decisions in `docs/ARCHITECTURE.md`.
3. Refresh `docs/PROJECT_MAP.md`: state, blockers, next action. Keep it at 400–700 tokens (900 max). Replace history, do not append it.
4. Inspect the final `git status`.
