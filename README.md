# Feature Pipeline Skill

This independently versioned repository contains a portable, standard-library-only core for
feature-pipeline orchestration.

## Layout

- `pipeline_core/` — importable core package. `pipeline_core/runner_cli.py` is the public CLI
  entry point (`main`); it only parses argv, builds a typed command, dispatches to a use case,
  and prints the rendered result — every parsing, dispatch, and rendering concern behind it
  lives in `src/feature_pipeline/cli/` (see below).
- `src/feature_pipeline/cli/` — the thin CLI boundary, split by responsibility so no module
  mixes flag definitions with routing, gate, or lifecycle decisions: `parser.py` (the argparse
  surface and its frozen constants), `errors.py` (`CliError`, the one fail-closed exception),
  `commands.py` (`RunCommand`, the typed argparse-free view of a parsed invocation), `use_cases.py`
  (status/dry-run/execute, dispatched from one typed `dispatch()`, each returning a typed
  `feature_pipeline.application.results.PipelineResult` independent of rendering — no use case
  mutates domain state or launches a process itself), and `renderers.py` (pure text rendering).
  See the package docstring for the full contract.
- `scripts/` — portable command-line entry points.
- `roles/`, `reports/`, and `schemas/` — versioned core contracts.
- `templates/` — portable core templates.
- `tests/` — core test suite.

## Launch contract

Every launcher receives these explicit filesystem anchors:

- `--project-root <path>`: the repository where a feature is being run.
- `--agents-root <path>`: the shared agent configuration root.
- `--core-root <path>`: this core repository root.

Callers supply all anchors. The core does not infer host-specific locations or project policy.

## Run modes

`scripts/run_pipeline.py --mode` selects how far a run goes:

- `plan-only` (default) / `unattended` — resolve the profile, select and route tasks, and
  print the deterministic dry-run plan. No executor is launched.
- `release-dry-run` — `plan-only` plus the post-task lifecycle: stages 10–16 (documentation
  and its audit, the Graphify refresh and verification, final verification, the human
  final-diff gate, and the release dry run) and their gates are laid out in order in the
  `C4` / `C6` plan, read from the tool-integration and release-policy files beside the
  profile. It implies `--dry-run`, writes nothing, and stops at the release dry-run boundary
  with exit `10`; the release stage has no commit or push code path.
- `execute` — run stages 5–9 for every dependency-ready selected task: executor dispatch,
  runner-owned verification commands, two independent verifier verdicts, and the bounded
  repair loop, ending each task at `verified` or at a truthful non-zero terminal state. It
  **stops before stage 10** — no documentation, Graphify, final verification, release, or
  archive/purge code runs. It refuses to dispatch the first executor until the plan gate is
  satisfied (`--approve-plan`, or an explicit `--unattended` opt-in). See
  [`scripts/README.md`](scripts/README.md) and
  [`fixtures/execution/README.md`](fixtures/execution/README.md).

## Safety contracts

- **Read-only Git is a parsed allowlist, not a denylist.** Every argv handed to `GitPort`
  passes through `feature_pipeline.infrastructure.git.safety.parse_read_only_git`: the
  subcommand must be the first token and one of a fixed set of read-only operations
  (`rev-parse`, `ls-files`, `status`, `diff`, `show`, `log`, …), and every remaining token
  must match that operation's argument schema. A leading global option, a `-c` alias
  override, an attached `--git-dir=` redirect, and an unrecognised subcommand all fail
  closed — an unknown spelling is never a reason a mutating call runs. `push` and every
  history-rewriting operation stay permanently unavailable.
- **A Git inspection never presents missing evidence as a clean result.**
  `feature_pipeline.infrastructure.git.inspector.inspect` returns `available=False` with a
  stated reason for a policy denial, a launch failure, a timeout, or a non-zero exit;
  `available=True` is returned only for a real exit-zero run.
- **Every accepted launch control is validated, persisted, and applied.**
  `feature_pipeline.application.launch_controls.resolve_launch_controls` rejects an unknown
  control key by name and a malformed value with its reason *before* any run state, lease,
  or process exists; `plan_launch` then threads the resolved timeout, the per-kind output
  byte budget, and the serialized-program mutex into each launch. No operational control is
  a silent no-op.
- **Codex and Claude use explicit adapter composition.** Codex launches through shell-free
  `codex exec` argv with JSONL result parsing, resolved extra-directory grants, and the same
  bounded process timeout handling as Claude. Its status-envelope continuation starts a fresh
  read-only execution because `codex exec resume` cannot carry those grants. An explicit
  unavailable Codex selection fails closed; `auto` retains the registry's compatibility order.
- **An out-of-scope executor change is a mechanical block, not a verifier's opinion.**
  `feature_pipeline.application.execute_task.enforce_scope_gate` runs immediately after the
  bounded worktree attribution and *before* any verifier launches. It re-derives every
  changed path's classification from the task's `Allowed scope` with
  `feature_pipeline.domain.scope.AllowedScope` (it does not trust the label attribution
  attached), and returns a deterministic decision: `clean` (verification proceeds),
  `scope-violation` (an executor-owned change landed outside the scope — the task is
  blocked), or `attribution-unavailable` (the window could not be attributed with
  confidence — fail closed). The gate **never reverts or cleans a file** — out-of-scope
  paths may hold user work — and a file the user left dirty before the run that the
  executor never touched is not a candidate and cannot trip it. Its only side effect is a
  bounded manifest and diff, published through the typed artifact store with explicit run
  and repository coordinates for diagnostics and human review.
- **An unsupported pipeline stage fails before any run state exists.**
  `feature_pipeline.domain.stages.compile_stage_sequence` builds the immutable stage sequence
  `feature_pipeline.application.pipeline_engine.PipelineEngine` executes, and rejects — at
  compile time, before a run is created — a stage with no handler and no declared deferred
  capability, a duplicate, or one out of the documented order. A deferred stage is recorded
  as a compile-time capability, not callable dead code; `PipelineEngine` never calls it. The
  engine holds no policy of its own: `TaskExecutionStage` wraps the existing `TaskEngine`
  repair loop unchanged, and a run's terminal outcome maps onto the same `gate-pending` /
  `blocked` / `error` exit codes the CLI already produces.
- **Stages 5-9 have exactly one production path.** Whenever a real `execute` invocation has a
  compiled plan (every CLI run, since CP-02), `pipeline_core.execution.execute_run` drives
  each dependency-ready task through the one `PipelineEngine`-compiled sequence — task
  selection and executor routing are recorded as evidence from the already-resolved
  `CompiledRunPlan`, never re-decided, and stages 7-9 stay `TaskExecutionStage`'s unchanged
  `TaskEngine` delegation. `pipeline_core.stages` (the generic, project-declared runner) is a
  distinct, still-connected path for stages 10-16 (`post_task.py`, `release.py`) and is out of
  this cutover's scope. `pipeline_core.archive` and `pipeline_core.recovery_descriptors` are
  explicitly deferred primitives — no accepted stage reaches either today
  (`tests/test_safety_ports.py::DeferredPrimitiveTests` proves it) — kept for a future,
  separately approved archive/purge/recovery stage rather than removed.

Run the test suite from the repository root:

```text
python -m unittest discover
```
