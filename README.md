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
- `execute` — run stages 5–9 for every dependency-ready selected task: executor dispatch,
  runner-owned verification commands, two independent verifier verdicts, and the bounded
  repair loop, ending each task at `verified` or at a truthful non-zero terminal state. It
  **stops before stage 10** — no documentation, Graphify, final verification, release, or
  archive/purge code runs. It refuses to dispatch the first executor until the plan gate is
  satisfied (`--approve-plan`, or an explicit `--unattended` opt-in). See
  [`scripts/README.md`](scripts/README.md) and
  [`fixtures/execution/README.md`](fixtures/execution/README.md).

Run the test suite from the repository root:

```text
python -m unittest discover
```
