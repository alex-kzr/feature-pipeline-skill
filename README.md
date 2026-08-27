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

Run the test suite from the repository root:

```text
python -m unittest discover
```
