# Feature Pipeline Skill

This independently versioned repository contains a portable, standard-library-only core for
feature-pipeline orchestration.

## Layout

- `pipeline_core/` — importable core package.
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

Run the test suite from the repository root:

```text
python -m unittest discover
```
