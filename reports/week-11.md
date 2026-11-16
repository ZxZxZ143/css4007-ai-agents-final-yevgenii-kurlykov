# Semester Week 11 Progress Report

## Completed Work

During November 9–15, 2026, the work recorded on November 9 consisted of late Week 10 documentation and a design revision under the prior project topic. The design separated LangGraph orchestration, OpenAI model decisions and deterministic verification. Codex assisted with the documentation and repository checks.

## Important Commits

- `9716249` — initial proposal, roadmap and project instructions, November 9.
- `a3d99db` — architecture revision covering tool selection, checkpoints and approval, November 9. [Repository checks for this commit](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/42675859841)

## Current Functionality

The instructor's Node.js starter checks ran successfully. The Python environment, live model adapter and model-selected tool prototype were still pending at the end of Week 11.

## Problems Encountered

The initial LLM integration milestone was not reached. OpenAI API access and model behavior were unverified.

## Solutions

Carry the environment and live tool-call checks into the next implementation window. Treat documentation checks as evidence of repository completeness.

## What I Learned

The architecture review identified the need to separate graph checkpoints from operation evidence. A resumed node must reconcile completed operations before issuing another write.

## Next Week

Begin AgentShield Lab with the Python setup in W11-01, followed by the live OpenAI tool call and minimal CLI. Then build the first synthetic Helpdesk scenario and verify it through recorded tool behavior and mock state.

## Carryover completed November 17

The Week 11 prototype was implemented during Week 12 ahead of the November 18 classroom review. The locked Python setup, CLI and both live GPT-5.6 Luna checks passed locally. The [Week 12 report](week-12.md) records the implementation commit and tool-call evidence.
