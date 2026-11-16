# AgentShield Lab implementation roadmap

The windows below follow [project.config.json](../project.config.json). The [proposal](../reports/proposal.md) owns the architecture, tool contracts and evaluation criteria. The Week 11 prototype passed verification on November 17 during Week 12. Deliver the S1 vertical slice before expanding scenarios. Keep academic dates and actual commit dates separate.

P0 gates dependent work. P1 is a required deliverable. Statuses are TODO, IN_PROGRESS, BLOCKED, VERIFYING and DONE. DONE requires acceptance evidence. Code tasks also need relevant tests and green CI for their latest relevant commit.

| ID | Task | Priority | Depends on | Acceptance | Verification | Status |
|---|---|---|---|---|---|---|
| W10-01 | Verify fork and requirements | P0 | — | Student fork and course calendar confirmed | Origin, GitHub parent and config reviewed | DONE |
| W10-02 | Define AgentShield Lab and academic plan | P0 | W10-01 | All required proposal and elapsed-week report sections complete and factual | Course validators and manual historical review | DONE |
| W10-03 | Align roadmap, project map and operating policy | P0 | W10-02 | One consistent architecture, dependencies and actionable next step | Review of current docs and task IDs | DONE |
| W10-04 | Publish planning revision | P1 | W10-03 | Publish to student main and verify CI for that SHA | Published commit `91f391c` and [successful Actions run](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/43891953333) | DONE |
| W11-01 | Establish Python environment | P0 | W10-03 | Python 3.12+, compatible package lock, clean setup and local mock runner prerequisites | Fresh locked Python 3.12 install, imports, offline catalog and CI | DONE |
| W11-02 | Verify OpenAI model and tool-call adapter | P0 | W11-01 | Configurable GPT via langchain-openai emits a schema-valid test call with recorded model, usage and latency | Luna selected `list_scenarios` through LangGraph. Python returned S2, backed by live trace and six offline tests | DONE |
| W11-03 | Add validated CLI entry point | P0 | W11-02 | Parse goals/config, report structured errors and handle a no-tool request | Live CLI tool and no-tool requests, invalid category and missing credential checks | DONE |
| W12-01 | Build synthetic fixtures and scenario registry | P0 | W11-03 | S1 goal, revisioned mock data, actor and separate oracle, with reset between tests | Fixture and reproducibility tests | TODO |
| W12-02 | Implement Helpdesk Target and profile policy | P0 | W12-01 | Minimal mock search, status and ticket creation tools. Profiles share tools and ordinary capabilities. Protected validates scope and approval | Tool contract and profile parity tests | TODO |
| W12-03 | Capture target traces and mock effects | P0 | W12-02 | Trace correlates model calls, arguments, decisions, failures and state snapshots | Replay S1 from registered inputs and verify event joins | TODO |
| W12-04 | Complete deterministic vertical slice | P0 | W12-03 | S1 runs end to end in both profiles. Task failure and missing evidence are distinguishable | T01 plus missing-evidence counterexample | TODO |
| W13-01 | Expose Auditor tools through typed dispatcher | P0 | W12-04 | Six planned services with audit scope, structured errors, budget checks and evidence IDs | Contract tests and trace inspection | TODO |
| W13-02 | Add first attack and failure scenarios | P0 | W13-01 | S2 and S5 registered with objective effect/cause oracles | Fixed fixture tests and independently evaluated runs | TODO |
| W13-03 | Implement autonomous Auditor loop | P0 | W13-02 | LangGraph uses observations to branch and selects at least three meaningful tools through GPT | Real-model traces distinguish model selection from required nodes and show different follow-ups | TODO |
| W14-01 | Persist checkpoints and audit ledger | P0 | W13-03 | Resume preserves budget and reconciles operation IDs before mock effects | Restart and duplicate-effect tests | TODO |
| W14-02 | Compare profiles and report evidence | P0 | W14-01 | Paired inputs, explicit policy differences and reproducible report with inconclusive handling. Complete catalog S1–S8 | Matched run artifacts and deterministic report checks | TODO |
| W14-03 | Select eligible research paper | P1 | W10-03 | LLM/agent paper originally released June 22–December 22, 2026. Complete paper selection by December 6 | Verify original publication metadata and fill required fields | TODO |
| W15-01 | Harden containment, permissions and approval | P0 | W14-02 | Untrusted data cannot grant privileges. Enforce typed IDs, mock-only tools and valid action-bound approval | G01/G03, T03/T04 and scope tests | TODO |
| W15-02 | Enforce budgets and failure recovery | P0 | W15-01 | Calls, retries, steps, tokens and deadlines bounded independently for Auditor and Target | G02/G04, timeout, malformed-result and resume tests | TODO |
| W15-03 | Verify all mandatory guardrails | P0 | W15-02 | Four guardrail tests pass and show trace evidence of prevention or safe stopping | pytest and green CI | TODO |
| W16-01 | Run paired real-model evaluation | P0 | W15-03 | At least 12 application tests including four guardrails, target 16. Run matched S1–S8 and compute metrics | pytest, CI, raw evidence and documented denominators | TODO |
| W16-02 | Refine and rehearse live demo | P0 | W16-01 | Reproduce a complete autonomous audit and explain findings and limitations | Rerun regressions and rehearse with real model | TODO |
| W16-03 | Prepare defense materials | P1 | W16-02, W14-03 | Setup instructions, Week 16 report, AI-use disclosure and 5–7 minute research presentation | Clean setup and timed rehearsal | TODO |
| W17-01 | Verify submission package | P0 | W16-03 | Final report, honest weekly reports, test evidence, fork URL and final hash | Official checks, app tests, exact-SHA green CI | TODO |
| W17-02 | Submit and defend | P0 | W17-01 | Submit by deadline and deliver live demo, unseen guardrail challenge and research presentation | Submission receipt and actual defense | TODO |

## Weekly gates

| Week | Official window (2026) | Gate |
|---|---|---|
| 10 | Nov 2–8 | Proposal and planning documentation |
| 11 | Nov 9–15 | Reproducible Python setup and real OpenAI tool call. Carryover into Week 12 |
| 12 | Nov 16–22 | S1 completes end to end with mock traces and deterministic checks |
| 13 | Nov 23–29 | Auditor selects three tools and adapts its next test to observed behavior |
| 14 | Nov 30–Dec 6 | Safe resume, baseline/protected comparison, evidence report and paper selection |
| 15 | Dec 7–13 | Four mandatory guardrail tests and safe stopping |
| 16 | Dec 14–20 | Application test suite, paired evaluation and live demo rehearsal |
| 17 | Dec 21–22 | Final submission and defense. Deadline Dec 22 at 23:59 UTC+05 |

Protect the vertical slice and genuine tool selection. If time is tight, defer a web UI, optional Helpdesk tools and extra variants. Keep paired inputs and deterministic oracles independent of model output. W12-01 is unblocked. Paper selection can proceed independently by December 6.

The Week 11 code commit `6aa8dfa` passed the Python and Node jobs. The live model trace and local verification are recorded in the dated report. [GitHub Actions](https://github.com/ZxZxZ143/css4007-ai-agents-final-yevgenii-kurlykov/actions/runs/43951538156) · [Week 12 report](../reports/week-12.md)
