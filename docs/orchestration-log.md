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
| 2026-11-25 | P-01 | implementer | 1 | done | 30k | 105 s | policy types; 144 tests |
| 2026-11-25 | P-01 | reviewer | 1 | revise | 20k | 33 s | real defects: nested args mutable after construction (TOCTOU on fingerprint), paths not in fingerprint, model_copy/model_construct bypass validation |
| 2026-11-25 | P-04 | implementer | 1 | done | 32k | 86 s | budget tracker; 12 tests |
| 2026-11-25 | P-01 | implementer | 2 | done | 40k (cum.) | 66 s | frozen args, paths in fingerprint, validated copy; probed by orchestrator (only deliberate dict.__setitem__ bypass remains) |
| 2026-11-25 | P-04 | reviewer | 1 | accept | 24k | 36 s | minor: understated total_tokens; fixed by orchestrator (charge max of totals) |
| 2026-11-25 | P-03 | implementer | 1 | done | 41k | 121 s | approvals; 41 tests |
| 2026-11-25 | P-02 | implementer | 1 | done | 39k | 186 s | path policy; 53 tests, junction/8.3/symlink cases ran |
| 2026-11-25 | P-06 | implementer | 1 | done | 47k | 191 s | fast-path router; 54 tests |
| 2026-11-25 | P-03 | reviewer | 1 | revise | 21k | 28 s | real defects: multi-number transcript brute-forces the voice code; TTS prompt with the code can self-approve via echo; clock rollback revives expired approvals |
| 2026-11-25 | P-02 | reviewer | 1 | revise | ~25k | — | real defect: hard deny vanished with a minimal env (C:\Windows allowed under root C:\); drive-root allowed roots accepted |
| 2026-11-25 | P-06 | reviewer | 1 | revise | ~30k | — | real defect: non-canonical loopback URLs (127.1, 0x7f…, *.localhost) took the fast path; minor: Cf chars, bare-domain false positives, no RouteDecision validator |
| 2026-11-25 | P-03 | implementer | 2 | done | 56k (cum.) | 78 s | single-number rule, code only on screen, latched expiry; probed by orchestrator, accepted |
| 2026-11-25 | P-06 | implementer | 2 | done | 67k (cum.) | 101 s | host/loopback rules, Cf chars, TLD allowlist, code-level app denylist; probed by orchestrator, accepted |
| 2026-11-25 | P-02 | implementer | 2 | done | 59k (cum.) | 127 s | OS-derived hard-deny union, fail-closed roots, Cf/NFKC; probed with empty env by orchestrator, accepted; orchestrator moved scratch tmp_path fixture to conftest |
| 2026-11-25 | P-05 | implementer | 1 | done | 57k | 161 s | ActionGate, Permit, PolicyDenied; 569 tests total |
| 2026-11-25 | P-05 | reviewer | 1 | accept | 35k | 70 s | no bypass found (code reading only); low: request_approval trusted caller Decision channel, trace-failure ordering, summary length |
| 2026-11-25 | P-05 | implementer | 2 | done | 65k (cum.) | 98 s | request_approval re-evaluates, trace ordering, 200-char payloads; end-to-end smoke by orchestrator, accepted |

## Usage observations

- M0 (2026-11-25): 12 runs, about 0.39M subagent tokens in total (resumed runs report cumulative counts). The reviewer pass was worth its cost: on F-02 and F-03 it found real security defects that the implementer's own tests missed. Review per roadmap task (not per micro-task) stays the default.
- F-02..F-04 ran in parallel in the main checkout rather than in worktrees: worktrees start from HEAD and would not contain the uncommitted F-01 skeleton. Disjoint owned files made this safe. Once the base is committed, worktrees can be used again.
- M1 (2026-11-25): 12 runs (6 implementers incl. resumed revise rounds, 6 reviewers), about 0.49M subagent tokens (cumulative counts for resumed runs). Reviews found real security defects in 4 of 6 tasks (P-01 args mutable after fingerprinting, P-02 hard deny vanished with a minimal env, P-03 voice code brute force and TTS self-approval, P-06 non-canonical loopback URLs). Review per task stays the default for policy code. Revise fixes were verified by orchestrator probes instead of a second review round.
