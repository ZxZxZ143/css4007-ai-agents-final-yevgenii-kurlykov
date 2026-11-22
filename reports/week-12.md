# Semester Week 12 Progress Report

## Completed Work

On November 17, 2026, I completed the technical work carried over from Week 11. The Python 3.12 project installs from `uv.lock`, and the CLI runs a LangGraph agent with a read-only synthetic Helpdesk catalog. Codex assisted with implementation, tests, verification and documentation.

On November 22, Codex assisted with W12-01. The registered S1 fixture includes a user goal, actor, allowed ticket ID, two synthetic tickets and explicit scenario/data revisions. Each preparation creates an independent state and run ID. A separate deterministic evaluator checks lookup evidence, the structured final answer and unchanged ticket state.

Local verification on November 22 passed all 24 Python tests, including 18 S1 cases. They cover state reset, input validation, correct completion, incorrect answers and lookup results, missing evidence, foreign run/revision evidence, call correlation, state changes and unsupported oracle revisions. These are offline checks using constructed evidence.

W12-02 adds a Helpdesk Target through the shared LangGraph loop. It binds three local tools to a fresh S1 session, with baseline/protected policy profiles, a shared tool budget and action-bound single-use creation approvals. A new `target` CLI command selects the profile and optionally grants one exact-subject approval. Codex assisted with implementation and tests.

Local verification passed 48 Python tests, including 24 Helpdesk cases, and ten instructor Node tests. Helpdesk tests cover ordinary behavior in both profiles, separate owner/scope checks, denied writes, mismatched/expired/consumed approvals, argument validation, call budgets, state isolation, graph routing and CLI approval handling. The proposal, Week 10–12 reports and repository validators passed. CLI help and the existing offline catalog worked. A missing-key Target command returned a controlled error and exit code 1.

## Important Commits

- [`6aa8dfa`](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/commit/6aa8dfae0d6ef6a3d2dbd1d9d1e54bd2422a507b) added the prototype, the locked dependencies, six Python tests and the Python CI job on November 17 at 01:34 UTC+05.
- The Python and Node CI jobs passed for that exact code commit. [Repository checks](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/43951538156)
- [`a2b661d`](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/commit/a2b661d96db2532785cffa0552073ab9ad79e633) implements W12-01 on November 22. All 24 Python tests, ten Node tests and course checks passed locally. Both CI jobs passed for this code commit. [S1 implementation checks](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/45042828574)

## Current Functionality

The graph routes model-selected calls to `list_scenarios` after validating the category. It returns the catalog result to the model and ends when the model produces a direct answer. The CLI displays the answer, tool outcome and available model usage in verbose mode.

Both live checks used `gpt-5.6-luna` through `ChatOpenAI` and the OpenAI Responses API.

| Prompt | Observed outcome | Usage and elapsed time |
|---|---|---|
| List available security testing scenarios. | The model issued `call_UGNFKLeul3Ub6twKGcRO1bdd` for `list_scenarios` with `{"category":"security"}`. Python returned S2, Knowledge-base prompt injection. The next model response listed only S2 and its catalog description. | 2 model responses, 1 tool execution, 647 total tokens, 7.79 s |
| What is AgentShield Lab? | The model described the project's security, reliability and task correctness evaluation for synthetic Helpdesk agents. | 1 model response, 0 tool executions, 310 total tokens, 3.21 s |

The fresh Python 3.12 install and import check passed. Six offline pytest tests and ten instructor Node.js starter tests passed. The proposal validator, Week 10 and 11 report validators, repository validation and activity script ran successfully. The offline CLI returned S5 for reliability and exited with code 2 for an invalid category.

## Problems Encountered

The official Week 11 window ended November 15. The November 17 prototype verifies catalog lookup and leaves the Helpdesk Target and scenario evaluator for the S1 vertical slice.

The November 22 evaluator is exercised with constructed offline evidence. W12-02 Target tests use mocked model responses with real Python tools. A new live OpenAI Target invocation has not been verified. Trusted trace recording in W12-03 and automatic paired S1 evaluation in W12-04 remain open.

## Solutions

The prototype uses Pydantic for tool arguments, a six-step graph limit, a per-request timeout and concise errors for missing credentials and API failures. CI runs the Python checks without live API requests.

## What I Learned

The tool call ID links the model's request to the Python tool result. The returned catalog entry and the model's final text can therefore be checked independently.

## Next Week

Record Target tool effects in W12-03, then run S1 with matched baseline and protected inputs and automatic evaluation in W12-04.
