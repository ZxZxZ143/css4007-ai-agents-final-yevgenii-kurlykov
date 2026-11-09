# RestoreProof — current project map

Read after `AGENTS.md`; follow relevant roadmap tasks only. Updated 2026-11-09.

- **Goal:** Level A PostgreSQL recovery-verification agent. Controlled artificial `pg_dump -Fc` fixtures, Docker restore, adaptive diagnosis, independent manifest checks and evidence report; S1–S6 in proposal. No production/arbitrary backups, PITR, cloud or UI.
- **Phase:** Week 10 planning completed late Nov 9; local checks pass. No Week 11 implementation. Baseline `b537533` is inherited instructor work, not student activity.
- **Implemented:** instructor Node utilities/sample tests only; `src/model-client.mjs` is a stub. No RestoreProof Python services, fixtures, real LLM or application tests.
- **Architecture:** Python >=3.12, CLI/minimal FastAPI, LangGraph single agent, native LLM tool calls, Pydantic, bounded deterministic Docker/PostgreSQL services, SQLite state/traces, pytest. Model selects diagnostics/replans; code enforces policy/verdicts. Contracts/state are in proposal.
- **Active:** W10-01…03 DONE; W10-04 publication/CI VERIFYING. Next implementation: W11-01 environment and enforceable Docker caps → W11-02 real tool-call proof → W11-03 validated interface. This assignment adds no Week 11 code.
- **Unresolved:** exact model/provider/access/cost; host Docker/resource-limit feasibility; pinned PostgreSQL image/profile; fixture generation remains future work. Paper not selected; due Week 14; original public release June 22–Dec 22, 2026.
- **Invariants:** registered fixture/run IDs, trusted independent expectations/freshness metadata; no model shell/SQL/filesystem/Docker control. Run-owned resources only. Explicit initial sandbox authorization; separate action-bound ownership-bypass approval. Caps before dispatch; bounded retries/cleanup; sanitized untrusted observations/traces. Docker is not complete isolation. VERIFIED requires all declared checks; concessions stay DEGRADED; failure/insufficient evidence remain explicit.
- **Workflow:** preserve instructor assets and future reports; no backdating/invented evidence. DONE requires criteria/tests; code requires latest relevant green CI. Push only to student `origin`, never instructor.
- **Sources:** `docs/ROADMAP.md` tasks/gates/evidence; `reports/proposal.md` scope/contracts/evaluation; `reports/week-10.md` actual late activity; `A.md`, `C.md`, `D.md`, `README.md` rules; `project.config.json` dates. Deadline Dec 22, 2026, 23:59 UTC+05.

Keep this an actionable index: about 400–700 tokens, 900 soft ceiling. Remove obsolete history/duplicates; keep short pointers. Never discard active blockers, dependencies, safety constraints or implementation caveats for size.
