# `fixtures/execution/` — execute-mode integration fixtures (EMI-01)

Deterministic fake-adapter fixtures for `pipeline_core.execution.execute_run`. No real
`claude` process is launched: every executor and verifier launch outcome is scripted, so one
`execute_run` invocation is byte-reproducible across machines. `tests/test_execute_mode.py`
turns each scenario into an `ExecuteRequest` and asserts the terminal status and exit code.

## Contents

| File | Purpose |
|---|---|
| `plan.json` | Three fully-specified tasks — `EX-01`, `EX-02` (depends on `EX-01`), `EX-03` (depends on `EX-02`). Each entry carries the complete execution metadata (`task_type`, `executor`, `allowed_scope`, `acceptance_criteria`, …) that `--mode execute` requires. |
| `tasks/EX-0*.md` | Human-readable task files (also the `## Blockers` write target when a task is blocked at the repair limit). |
| `scenario_adapters.py` | `ScriptedExecutor` / `ScriptedVerifier` (the exact two-call adapter protocol the real `ClaudeAdapter` speaks) and the `SCENARIOS` table. |

## Scenarios

Single-invocation scenarios in `SCENARIOS` (asserted by `ScenarioTableTests`):

| Scenario | Shape | Exit |
|---|---|---|
| `direct-success` | one ready task → launch, checks, two `PASS` → `verified` (AC-1) | `0` |
| `successful-repair` | first gate `FAIL`, one bounded repair, second gate `PASS` | `0` |
| `repair-exhaustion` | `FAIL` with no repair budget → `blocked` + diagnostic (AC-2) | `20` |
| `external-blocker` | a `BLOCKED` verdict → `blocked`, no repair attempt spent (AC-4) | `20` |
| `executor-cannot-implement` | executor reports `blocked` before any gate (AC-4) | `20` |
| `malformed-verifier-output` | unparseable verdict envelope → diagnostic, never `verified` (AC-4) | `20` |
| `dependency-ordering` | `EX-02` becomes ready only after `EX-01` verifies (AC-1) | `0` |
| `dependency-suppression` | `EX-01` blocks; `EX-02`/`EX-03` suppressed, never dispatched (AC-2/AC-4) | `20` |
| `adapter-unavailable` | no adapter in the environment → runner error, nothing substituted (AC-4) | `30` |
| `gate-pending` | no `--approve-plan` / `--unattended` → no executor dispatched (AC-5) | `10` |

Multi-invocation / out-of-band scenarios are bespoke methods in `ResumeAndSafetyTests`:

- **crash / resume (AC-3)** — a run that failed a gate is reloaded, its task rolled back to
  `verification_failed`, and `execute_run(resume=True)` continues the *already-open* repair
  without re-counting the attempt; the pinned adapter is unchanged.
- **adapter switch (AC-4)** — a resume whose persisted `adapter_resolved` no longer matches
  the resolved adapter fails closed with `adapter-switch` (`exit 30`).
- **live lease (AC-4)** — a live foreign `pipeline.lock` blocks the run (`exit 20`).
- **`--unattended` opt-in (AC-5)** — satisfies the plan gate without `--approve-plan`.

The out-of-scope-change path is attributed at the worktree level in `tests/test_worktree.py`
and `tests/test_dispatch.py` (RDS-05); in execute mode it surfaces as a verifier `FAIL` and
is exercised by `successful-repair` / `repair-exhaustion`.
