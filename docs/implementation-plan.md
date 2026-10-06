# Implementation Plan — Full Conformance to `herdr-bartender-plan.md`

## Inputs
- Spec: `herdr-bartender-plan.md` (§1–§11), amended by `docs/plan-resolutions.md` (R1–R23).
- Gap inventory: `docs/audit/gaps.json`. It holds 182 gaps (9 critical, 50 high, 83 medium, 40 low), found by 7 area auditors and a completeness critic. Each gap carries `area`, `id`, `plan_refs`, `code_refs` and `required_change`.

## Baseline (2026-10-04)
- `bin/herdr-bartender` is a single 5,240-line Python file, with a 2,000-line embedded test function (`--unit-test`).
- The suite fails on Linux at the hook-guard `stat -f %m` step, and it writes into the real `$HOME`.
- Several core guarantees are broken:
  - the lock proceeds without being acquired;
  - the event path never takes the contention spool path;
  - a `break` skips the post-lock dispatch;
  - `main()` ignores the stdin envelope;
  - the background reconciler inherits the 1.4s budget.

## Architecture Target
- Package `herdr_bartender/` with modules of 800 lines or less (200–400 typical), organized by domain:
  - `config`/`paths`: state dir, orphan path, vendor hooks dir with test override, port validation.
  - `clock`: an injectable clock.
  - `process`: liveness, start times, the 0.5s cache, subprocess timeouts.
  - `sanitize`/`normalize`.
  - `cache`: lock, atomic save, salvage.
  - `bridge`: HTTP with no redirects, no proxy, classification, liveness gate.
  - `markers`/`vendor`.
  - `spool`/`results`.
  - `orphans`.
  - `sender`: the Universal Sender Protocol (Step A/B/C), shared by every caller.
  - `handlers/`: status, pane close, cascades.
  - `reconciler`.
  - `hooks`: installer, plus `hook_guard.sh` as a resource file.
  - `cli`.
- `bin/herdr-bartender` becomes a thin launcher. It resolves symlinks, puts the repo root on `sys.path` and calls `cli.main()`. The manifest path does not change.
- `scripts/rollback.sh` holds the §9.1 rollback script.
- Tests use stdlib `unittest` under `tests/`, with one file per area. Shared fixtures live in `tests/support/`:
  - a sandboxed HOME, XDG_STATE_HOME and HERDR_PLUGIN_STATE_DIR, and a scrubbed env;
  - PATH shims for `pgrep`, `ps`, `osascript` and `herdr`, controlled by files in the sandbox;
  - a scriptable mock bridge;
  - a fake clock;
  - a subprocess multi-process harness.
- `--unit-test` runs the discovery. Each of the 68 §10.1 invariants has at least one test whose docstring starts `Plan §10.1 #N`. No test may touch the real HOME, real vendor hooks or real processes. The suite must pass identically on Linux and macOS.

## Waves
1. **W1 Foundation** (one implementer, then verify/fix loop)
   - Split the code into modules with behavior preserved.
   - Build the test infrastructure.
   - Port every existing test into `tests/` with sandboxing.
   - Apply the isolation and portability fixes needed for a truthful baseline:
     - vendor hooks dir override;
     - orphan path through `HOME`;
     - portable `stat` in the guard (R17).
   - Ported tests that fail because of documented gaps get `@unittest.expectedFailure` with the gap id. Later waves remove those markers.
2. **W2 Edges** (three parallel implementers on disjoint modules, then verify):
   - **(A) Hooks.** Guard, installer/uninstaller, NO_HOOKS, SHA allowlist and HOOK_NEEDS_REVIEW, rollback script.
   - **(B) Platform/bridge.** Process liveness, earliest start time, subprocess timeouts, 0.5s cache, HTTP no-redirect/no-proxy, error classification codes, port validation, liveness gate, perms/umask.
   - **(C) Intake/normalize.** stdin envelope (R20), admission cases A/B/C, sanitization, context hierarchy, precise container matching, host/session-id bounds.
3. **W3 Dispatch core** (one implementer, deep):
   - Real lock with contention spooling.
   - Step A spool replay, then live evaluation.
   - One Universal Sender (Step A/B/C) used by the status, close, cascade, cleanup, reconciler and replay paths.
   - Results dir, persisted compensations and vendor cleanups (stage, then send, then clear).
   - Post-lock dispatch that always runs.
   - Tombstones, agent_exits and generations.
   - 256-cap pruning.
   - Cascade clamp/handoff.
   - Watchdog and critical-section semantics.
4. **W4 Reconciler & lifecycle** (one implementer):
   - Background budget, so it does not inherit the 1.4s deadline.
   - Retry schedule 0/1/2/4/8 with the lease released during sleeps.
   - Spawn rules and the unconditional watchdog.
   - 60s idle exit.
   - Herdr dead for more than 5 minutes.
   - Restart payloads and the re-sync after DELIVERY_DOWN.
   - Horizons: 12h past TTL, 12h vendor_active, terminal horizon.
   - Dismissal cadence.
   - Hook integrity through the W2A API.
   - `--cleanup` and `--replay-orphans` contracts.
5. **W5 Test completeness & docs**:
   - All 68 invariants covered meaningfully, plus the clock and multi-process races.
   - README, manifest and `--live-test`.
   - Annotate the plan with a pointer to the resolutions.
6. **W6 Adversarial review** (loop until dry, at most 3 rounds):
   - Lens reviewers: correctness/concurrency, spec-conformance, tests-hollowness, security, bash guard.
   - Each finding is verified independently.
   - A fixer agent applies the fixes, and the suite is re-run.

## Gates (every wave)
- `python3 bin/herdr-bartender --unit-test` and `python3 -m unittest discover -s tests` pass, and the only `expectedFailure` markers belong to gaps owned by later waves.
- Every module is 800 lines or less, and every function is under 50 lines where practical.
- `bash -n` passes on the guard and the rollback script; `shellcheck` too, if installed.
- The real-HOME pollution check passes: run the suite with a canary HOME and assert that nothing outside the sandbox changed.
- Conventional commit after each wave.
