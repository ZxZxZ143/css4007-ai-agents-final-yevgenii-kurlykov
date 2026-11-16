# Semester Week 12 Progress Report

## Completed Work

On November 17, 2026, I completed the technical work carried over from Week 11. The Python 3.12 project installs from `uv.lock`, and the CLI runs a LangGraph agent with a read-only synthetic Helpdesk catalog. Codex assisted with implementation, tests, verification and documentation.

## Important Commits

- [`6aa8dfa`](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/commit/6aa8dfae0d6ef6a3d2dbd1d9d1e54bd2422a507b) added the prototype, the locked dependencies, six Python tests and the Python CI job on November 17 at 01:34 UTC+05.
- The Python and Node CI jobs passed for that exact code commit. [Repository checks](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/43951538156)

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

## Solutions

The prototype uses Pydantic for tool arguments, a six-step graph limit, a per-request timeout and concise errors for missing credentials and API failures. CI runs the Python checks without live API requests.

## What I Learned

The tool call ID links the model's request to the Python tool result. The returned catalog entry and the model's final text can therefore be checked independently.

## Next Week

Build the S1 ticket lookup fixture, mock Helpdesk Target and independent expected-status assertion. Verify matched baseline and protected runs before adding adversarial cases.
