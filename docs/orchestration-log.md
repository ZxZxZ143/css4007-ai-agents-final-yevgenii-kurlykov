# Orchestration log

One line per subagent run. The orchestrator uses this to judge whether the implementer → reviewer loop is worth its cost (see `AGENTS.md` §3.7).

| Date | Micro-task | Agent | Round | Status / verdict | Tokens | Duration | Notes |
|---|---|---|---|---|---|---|---|
| 2026-11-25 | F-01 | implementer | 1 | done | 21k | 76 s | skeleton, CI job |
| 2026-11-25 | F-01 | reviewer | 1 | accept | 19k | 27 s | info notes only |
| 2026-11-25 | F-02 | implementer | 1 | done | 27k | 68 s | |
| 2026-11-25 | F-02 | reviewer | 1 | revise | 23k | 37 s | real defect: `..` in allowed_roots bypassed drive-root check |
| 2026-11-25 | F-02 | implementer | 2 | done | 37k (cum.) | 72 s | fixes verified by orchestrator, accepted |
| 2026-11-25 | F-03 | implementer | 1 | done | 36k | 122 s | flagged over-redaction of token counters |
| 2026-11-25 | F-03 | implementer | 2 | done | 40k (cum.) | 160 s | orchestrator fix: numeric token counters kept |
| 2026-11-25 | F-03 | reviewer | 1 | revise | 24k | 50 s | real defects: forgeable run_finished, finish race, reasoning_content leak, token-mapping leak |
| 2026-11-25 | F-03 | implementer | 3 | done | 57k (cum.) | 73 s | fixes probed by orchestrator, accepted |
| 2026-11-25 | F-04 | implementer | 1 | done | 37k | 134 s | |
| 2026-11-25 | F-04 | reviewer | 1 | accept | 28k | 49 s | minor hardening requested |
| 2026-11-25 | F-04 | implementer | 2 | done | 41k (cum.) | 33 s | diagnostics, quota non-retryable, live-test skip |

## Usage observations

- M0 (2026-11-25): 12 runs, about 0.39M subagent tokens in total (resumed runs report cumulative counts). The reviewer pass was worth its cost: on F-02 and F-03 it found real security defects that the implementer's own tests missed. Review per roadmap task (not per micro-task) stays the default.
- F-02..F-04 ran in parallel in the main checkout rather than in worktrees: worktrees start from HEAD and would not contain the uncommitted F-01 skeleton. Disjoint owned files made this safe. Once the base is committed, worktrees can be used again.
