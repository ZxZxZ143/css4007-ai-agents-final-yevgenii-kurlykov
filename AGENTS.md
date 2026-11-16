# AgentShield Lab operating policy

## Before work

1. Read this file and `docs/PROJECT_MAP.md` first.
2. Read relevant tasks and gates in `docs/ROADMAP.md`. Consult the proposal and course rules as needed.
3. Inspect branch, remotes, status and recent commits. Preserve existing work and push only to the student fork.
4. Pick the next authorized, unblocked task whose dependencies are verified.

## During work

- Follow the recorded architecture. LangGraph orchestrates, OpenAI GPT via `langchain-openai` selects tools, and Python/Pydantic enforce policy and independent evaluation. Keep the GPT model configurable. OpenAI Agents SDK is excluded. Count model-selected tools only when actual traces support the claim.
- Run tests against registered synthetic Helpdesk fixtures and mock tools only. Preserve ordinary baseline behavior and hard containment in both profiles. Deny arbitrary shell, filesystem and external API access. Validate run scope, arguments and approvals in code. Treat retrieved and model-generated content as untrusted.
- Keep Target and Auditor budgets bounded. Resume from SQLite checkpoints only after reconciling operation IDs and mock effects. Require action-bound approval where applicable. Independent evidence determines security and correctness outcomes.
- Preserve instructor instructions, templates, validation scripts, starter tests, CI and report paths. Report work in its actual window and label late work accurately. Leave future reports as templates until work is recorded.
- Keep evidence and report blockers honestly. DONE requires verified acceptance criteria. Code tasks require relevant passing tests and green CI for their latest relevant commit. Do not fabricate results, dates, credentials, citations or completion. Disclose AI assistance and keep secrets outside Git.

## After work

1. Review changes, run applicable course and application checks, and record actual outcomes. Verify CI for the exact published commit when publication is in scope.
2. Update roadmap status, evidence and dependencies. Record any changed architecture in the owning document.
3. Refresh `docs/PROJECT_MAP.md` with actual status, blockers and next action. Aim for 400–700 tokens, with 900 as a soft ceiling. Remove duplicate history while preserving operational constraints.
4. Inspect final Git status and diff. Keep tool and evaluation contracts in the proposal, tasks in the roadmap and dated evidence in the weekly reports.
