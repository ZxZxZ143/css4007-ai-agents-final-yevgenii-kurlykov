# RestoreProof — current project map

Read after `AGENTS.md`; follow relevant roadmap tasks only. Updated 2026-11-09.

- **Goal:** Level A PostgreSQL recovery-verification agent. Controlled artificial `pg_dump -Fc` fixtures, Docker restore, adaptive diagnosis, independent manifest checks and evidence report; S1–S6 in proposal. No production/arbitrary backups, PITR, cloud or UI.
- **Phase:** Week 10 planning complete, Nov 9; local checks pass. No Week 11 implementation; inherited baseline is not student activity.
- **Implemented:** instructor Node utilities/sample tests only; `src/model-client.mjs` is a stub. No RestoreProof Python services, fixtures, real LLM or application tests.
- **Architecture:** LangGraph StateGraph/conditional edges orchestrate; OpenAI GPT via `langchain-openai` selects tools/replans. No OpenAI Agents SDK. Python >=3.12/Pydantic services enforce permissions, independent verification and cleanup; CLI/minimal FastAPI, Docker/PostgreSQL, pytest/Actions. Contracts/state in proposal.
- **Persistence:** SQLite LangGraph checkpoints own control state; application ledger owns operations/approvals/evidence. Resume reconciles operation IDs/owned resources before effects; never blindly repeat restore. Human approval uses validated action-bound interrupt/resume; deadlines/budgets survive resume.
- **Next:** W10-01…04 DONE (evidence in roadmap). W11-01 environment/Docker caps → W11-02 real OpenAI tool call → W11-03 interface; all TODO.
- **Unresolved:** exact configurable GPT model/API access/cost; package lock/runtime compatibility; host Docker/resource caps; pinned PostgreSQL profile; fixture generation. Provider is decided. Paper not selected; due Week 14; original release June 22–Dec 22, 2026.
- **Invariants:** registered fixture/run IDs, trusted independent expectations/freshness metadata; no model shell/SQL/filesystem/Docker control. Run-owned resources only. Explicit initial sandbox authorization; separate action-bound ownership-bypass approval. Caps before dispatch; bounded retries/cleanup; sanitized untrusted observations/traces. Docker is not complete isolation. VERIFIED requires all declared checks; concessions stay DEGRADED; failure/insufficient evidence remain explicit.
- **Workflow:** preserve instructor assets and future reports; no backdating/invented evidence. DONE requires criteria/tests; code requires latest relevant green CI. Push only to student `origin`, never instructor.
- **Sources:** `docs/ROADMAP.md` tasks/gates/evidence; `reports/proposal.md` scope/contracts/evaluation; `reports/week-10.md` actual late activity; `A.md`, `C.md`, `D.md`, `README.md` rules; `project.config.json` dates. Deadline Dec 22, 2026, 23:59 UTC+05.

Keep 400–700 tokens (900 soft ceiling); remove obsolete duplicates, retain actionable blockers, dependencies, constraints and caveats.
