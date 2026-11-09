# RestoreProof implementation roadmap

Planning date: 2026-11-09. Schedule authority: [`project.config.json`](../project.config.json). Contracts/evaluation: [`reports/proposal.md`](../reports/proposal.md). All implementation remains planned; this assignment completes only outstanding Week 10 planning. No backdating or automatic active-week credit.

P0 = gate prerequisite; P1 = required deliverable; P2 = optional. Statuses: TODO, IN_PROGRESS, BLOCKED, VERIFYING, DONE. DONE requires verified acceptance and relevant tests; code also requires green CI for the latest relevant commit. Planning can be DONE after local completeness/accuracy checks; publication is tracked separately. Do not progress dependent work before prerequisites are verified. Tasks target one focused session each; split oversized tasks using suffix IDs while retaining dependency links.

## Tasks

| ID | Task | Priority | Dependencies | Acceptance criteria | Verification / evidence | Status |
|---|---|---|---|---|---|---|
| W10-01 | Confirm fork and baseline | P0 | None | Student origin, actual parent, clean baseline and requirements inspected | GitHub metadata; inherited `b537533`; evidence below | DONE |
| W10-02 | Complete proposal and late report | P0 | W10-01 | Required sections, contracts, safety, evaluation and honest dates | Official proposal/report checks; manual review | DONE |
| W10-03 | Establish roadmap and compact memory | P0 | W10-02 | Config-aligned schedule, stable tasks/gates, operating policy, actionable compact map | Cross-document/diff review and map size check | DONE |
| W10-04 | Publish checked planning | P1 | W10-03 | Actual-dated meaningful commit pushed to student fork; exact-SHA CI checked | Commit/push and Actions run | VERIFYING |
| W11-01 | Establish Python and sandbox feasibility | P0 | W10-03 | Python >=3.12 environment/lock strategy; Docker usable; pinned profile enforces CPU/RAM/disk caps; no host mounts/network/socket in restore container | Setup transcript, bounded isolation smoke check; no production access | TODO |
| W11-02 | Select provider and real tool-call adapter | P0 | W11-01 | Accessible configurable native-tool model returns valid typed harmless call; credentials excluded | Real API smoke trace; adapter success/error tests; CI | TODO |
| W11-03 | Add minimal validated request interface | P1 | W11-02 | CLI goal validation, structured output, bounded API errors and no-tool answer; thin FastAPI wrapper only if time permits | Valid/invalid/no-tool/error tests, setup instructions, CI | TODO |
| W12-01 | Generate artificial fixtures and references | P0 | W11-03 | S1 custom dump, independent expectations, hashes/timestamp provenance; no real data | Rebuild fixture and compare manifest against source DB | TODO |
| W12-02 | Implement inspector/compatibility catalog | P0 | W12-01 | Registered fixture/profile IDs; format/version/dependency findings; unsafe/unsupported inputs rejected | Archive/compatibility/argument tests and bounded commands | TODO |
| W12-03 | Implement sandbox and strict restore | P0 | W12-02 | Run-owned ready DB, fixed argument arrays, bounded restore, owned cleanup on success/failure | Disposable restore and ownership/timeout tests; CI | TODO |
| W12-04 | Implement verifier and verdict rules | P0 | W12-03 | Read-only object/count/checksum/dependency/freshness checks; explicit missing evidence; exit 0 insufficient | T01, T05–T08; missing-data counterexample; CI | TODO |
| W13-01 | Expose typed tools through policy dispatcher | P0 | W12-04 | >=3 genuinely model-selected tools; typed run-bound IDs, state preconditions, permissions | Real call trace; rejected args/foreign IDs never execute; CI | TODO |
| W13-02 | Add diagnostic probes/failure fixtures | P0 | W13-01 | S2–S4 ground truth; bounded archive/role/extension/log probes | Fixture reproduction and T02–T04 cause assertions | TODO |
| W13-03 | Add LangGraph observation loop | P0 | W13-02 | Multi-step model-selected investigations of >=2 failure types; observations returned; bounded loop | Real traces with distinct observation-driven sequences; CI | TODO |
| W14-01 | Persist explicit state/traces in SQLite | P0 | W13-03 | Goals/plans/arguments/evidence/counters/errors/stop reasons persisted; secrets redacted; no unsafe replay | State/trace consistency and restart-to-safe-stop tests; CI | TODO |
| W14-02 | Add adaptive alternatives and evidence report | P0 | W14-01 | Replan from roles/extensions; approved no_owner only; fresh sandbox; degraded outcome retained; linked report | T09 and full adaptive trace with verifier evidence; CI | TODO |
| W14-03 | Select eligible scientific paper | P1 | W10-03 | Technical LLM/agent paper; original release June 22–Dec 22, 2026; complete paper-selection report by Dec 6 | Primary publication/version history; authors/date/link/contribution and figure checked; instructor review if requested | TODO |
| W15-01 | Harden validation/policy/approval boundary | P0 | W14-02 | No arbitrary path/SQL/shell/Docker; untrusted content cannot grant permissions; run/action-bound approvals | G01/G03, traversal and foreign-resource tests | TODO |
| W15-02 | Enforce budgets/recovery/safe cleanup | P0 | W15-01 | Step/tool/retry/token/subprocess/wall limits before actions; bounded cancellation/cleanup; API failure handling | G02/G04, T10/T11 and cleanup checks | TODO |
| W15-03 | Verify mandatory guardrail gate | P0 | W15-02 | All four guardrails safe; zero unauthorized effects; stop reasons/traces available | G01–G04 and relevant suite pass on latest green CI | TODO |
| W16-01 | Run reproducible full evaluation | P0 | W15-03 | >=12 application cases incl. four guardrails (target 16); S1–S6 x3 real-agent runs; strict baseline; failures retained | pytest/CI; results with denominators/config/hash/trace links | TODO |
| W16-02 | Correct failures and demonstrate workflow | P0 | W16-01 | Metrics assessed against proposal; regressions fixed or explicit limitations; full autonomous workflow demonstrated | Relevant reruns, reproducible live demo, final green CI | TODO |
| W16-03 | Prepare documentation and paper defense | P1 | W16-02, W14-03 | Setup/architecture/AI disclosure/evaluation current; Week 16 report; 5–7 minute paper method/experiment/limitations presentation | Clean setup and timed demo/paper/Q&A rehearsal | TODO |
| W17-01 | Freeze final repository submission | P0 | W16-03 | Final report, honest weekly reports, evidence, URL and final hash; no secrets | Official checks, app suite, exact-SHA green CI and remote hash comparison | TODO |
| W17-02 | Submit and defend | P0 | W17-01 | Submit by Dec 22 23:59 UTC+05; autonomous live demo, unseen guardrail challenge, paper presentation | Actual submission receipt/defense evidence; no advance delivery claims | TODO |

## Weekly gates

Dates inclusive, 2026; each report must describe actual work with meaningful commits and verification. Leave future report templates unchanged until work occurs.

| Week | Window | Minimum verified result before dependent work |
|---|---|---|
| 10 | Nov 2–8; late planning Nov 9 | Complete proposal/report and reviewed roadmap/map/policy; local checks pass; W10-04 separately confirms publication |
| 11 | Nov 9–15 | Reproducible environment/caps; real schema-valid LLM call and basic request/error handling |
| 12 | Nov 16–22 | Controlled dump restored and independently verified by deterministic services; false-success counterexample caught |
| 13 | Nov 23–29 | >=3 model-selected tools, multi-step investigation of two failures with distinct observed-action traces |
| 14 | Nov 30–Dec 6 | Persistent state, adaptive diagnosis, evidence report and approved degraded path; eligible paper selection |
| 15 | Dec 7–13 | All four mandatory guardrails tested; failures and exhausted budgets stop safely |
| 16 | Dec 14–20 | >=12 application tests incl. guardrails pass; real evaluation/full autonomous demo recorded; presentation rehearsed |
| 17 | Dec 21–22 | Final reports/URL/hash/green CI ready; submission and live defense by deadline |

## Critical path and risks

Critical implementation path: W11-01 → W11-02 → W11-03 → W12-01…04 → W13-01…03 → W14-01…02 → W15-01…03 → W16-01…03 → W17-01…02. W14-03 is an independent time-sensitive prerequisite for W16-03. W10-04 publication should finish immediately; it is not implementation.

| High-risk dependency/blocker | Early response |
|---|---|
| Provider access, schema support, cost | Resolve W11-02 through real call; another accessible native-tool model if needed; mocks never replace live proof |
| Windows Docker and disk quota enforcement | Resolve W11-01; use dedicated compatible local Linux/VM if required caps cannot be enforced; never silently weaken policy |
| Versions/extensions/role semantics | One pinned PostgreSQL family and one approved extension profile first; unsupported combinations stop with evidence |
| Independent expectations/freshness | Generate trusted references from source fixture DB, not restored DB; S6 uses reference metadata and fixed evaluation clock |
| Non-deterministic diagnosis/false success | Repeat live evaluations, retain failures, enforce deterministic verdict; improve observations rather than hardcode tool sequences |
| Sandbox/cleanup failure | Ownership ledger, timeouts, quotas and explicit residual-resource reporting; dedicated environment required |
| Academic schedule | Paper by Dec 6; guardrails Dec 13; core/evaluation freeze Dec 20; final Dec 22 23:59 UTC+05 |

If behind, cut UI/FastAPI polish, broad version support, extra probes and optional alternative profiles. Retain CLI, one supported PostgreSQL family, S1–S6, adaptive tool choice, independent verdicts, >=12 tests/four guardrails, traces and paper deliverables. Do not reduce safety or falsify completion. Report unmet requirements; declared Level A is not guaranteed. Week 17 has only two days and is reserved for submission/defense.

## Planning verification evidence

- Baseline: clean `main`, inherited `b537533bff85ec71879b903ecb70ed16f3759965`. Student `origin`: `https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov.git`.
- GitHub: `fork: true`, parent `ardakshalkar/css4007-ai-agents-final`; instructor HEAD matches baseline. No instructor writes.
- Node v24.13.0 satisfies >=22; official scripts need no dependency install. LLM access/Docker caps remain unverified Week 11 prerequisites.
- Local checks on Nov 9: `npm run proposal`, `npm run report -- 10`, `npm run validate`, `npm run activity` all exited 0. `npm test`: 10/10 instructor starter tests pass; these are not application tests. Activity initially showed only inherited Week 10 history, which earns no student credit.
- Manual review: all 12 proposal and seven report sections filled; S1–S6, tool contracts, four guardrails, calendar/gates and scope agree across documents. Only the five intended Markdown files changed; instructor assets/future reports preserved. `git diff --check` passes. Map: 2,704 characters before final status refresh, approximately 676 tokens by characters/4 (estimate, not tokenizer count).
- W10-02/03 acceptance is verified locally; all local Markdown links resolve. No RestoreProof application tests or evaluation measurements exist.
- Planning commit `97162498a858e9dd382482b227688a82386c3114` was pushed to student `origin/main`, actual date Nov 9 at 23:06:46 UTC+05. `npm run activity` correctly places it in Week 11's calendar window; it is late Week 10 planning, not initial LLM integration.
- CI workflow `Repository checks` is active. Manual Week 10 run [42671186615](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/42671186615) targets that exact SHA; last observed queued with no jobs. W10-04 stays VERIFYING until a completed successful run is observed. After this evidence update, also check CI for the final HEAD; remote queue scheduling is the remaining publication-verification dependency, not a local check failure.
