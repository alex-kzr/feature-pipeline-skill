# REC-01 - Restore executor context isolation and deliver the task-kind catalog

Plan - [2026-09-08-universal-pipeline-task-model-routing.md](../2026-09-08-universal-pipeline-task-model-routing.md)

## Status

- [ ] To Do
- [ ] In Progress
- [x] Done

## Result

Completed 2026-09-10T14:51:14Z for run `2026-09-10T14-32-58Z-rec-01-catalog-revalidation` — outcome: **verified**.

- Repairs: 0
- Gate failures: 1
- Task verifier verdict: PASS
- Test verifier verdict: PASS

Verification commands:
- `feature-pipeline-skill -> uv run python -m unittest discover -s tests -t .` — exit 0
- `feature-pipeline-skill -> git diff --check` — exit 0
- `. -> git diff --check` — exit 0

Evidence:
- .pipeline/runs/rec-01-catalog-revalidation/reports/REC-01/verify-1/task-verifier-1.md
- .pipeline/runs/rec-01-catalog-revalidation/reports/REC-01/verify-1/test-verifier-1.md

## Execution Metadata

- Type: python
- Executor: python-executor
- Depends on: TC-03
- Allowed scope: `feature-pipeline-skill/pipeline_core/adapters.py`, `feature-pipeline-skill/pipeline_core/lifecycle.py`, `feature-pipeline-skill/src/feature_pipeline/bootstrap.py`, `feature-pipeline-skill/src/feature_pipeline/application/verified_reuse.py`, `feature-pipeline-skill/src/feature_pipeline/catalogs/**`, `feature-pipeline-skill/src/feature_pipeline/domain/task_kinds.py`, `feature-pipeline-skill/pyproject.toml`, `feature-pipeline-skill/tests/**`
- Out of scope: `.pipeline/**`, `.gitmodules`, `.agents/**`, `docs/kanban.md`, `docs/plans/2026-09-08-universal-pipeline-task-model-routing.md`, `docs/plans/tasks/**`
- Required skills: `.agents/skills/software-development/test-driven-development/SKILL.md`, `.agents/skills/software-development/backend-dev/python/python-type-safety/SKILL.md`
- Maximum repair attempts: 2
- Documentation impact: `docs/agents/**`, `feature-pipeline-skill/README.md`
- Verification commands:
  - `feature-pipeline-skill` -> `uv run python -m unittest discover -s tests -t .`
  - `feature-pipeline-skill` -> `git diff --check`
  - `.` -> `git diff --check`
- Blocking conditions: Missing preceding verified dependency; inability to construct an immutable executor context without widening write access; or unresolved catalog contract prerequisite.

## Supersession

- Supersedes: TC-04

## Purpose

Replace the blocked TC-04 delivery with a scoped implementation that supplies executors their required immutable context without widening their writable workspace, then delivers the versioned task-kind catalog required by R01.

## Context

TC-04 is blocked because its `feature-pipeline-skill/` working root cannot read the task, plan, prompt, or required skills stored under the project root and `agents_root`. Temporary external-directory grants did not become effective sandbox access. The runner must provide this required read context itself, while keeping executor writes confined to the declared task scope.

The replacement must also fulfill TC-04's catalog responsibility. Reconcile the research §5.2 inventory with `docs/validation/task-model-routing/TC-01-baseline.md`, including the 16 producer/operation rows, six production adapter call sites, and library/manual lifecycle surfaces. The catalog must describe truthful implementation status; push has no routable kind and commit remains reserved.

## Requirements

- Build a runner-owned immutable executor context bundle containing the assigned task contract, plan/prompt content needed for the task, and required skill content; bind each entry to a safe logical source and SHA-256 digest.
- Make the Claude adapter consume that bundle without requiring task/plan/skill filesystem reads outside its working root and without granting the executor additional write roots.
- Preserve scoped external-directory behavior for legitimate writes; do not use a temporary wrapper, ambient CLI setting, or broad parent-directory `--add-dir` as the solution.
- Preserve actual dependency edges when verification is reused or a blocked run is recovered; a compatible resume of `TC-01 -> TC-02 -> TC-03 -> REC-01` must not fail from a synthetic task-set mismatch.
- Add standard-library loaders/validators and versioned JSON resources for every catalog operation; include ownership, truthful implementation status, schemas, roles, stack/capability constraints, risk/complexity defaults, context/budget, independence, retry/repair, verification, provenance, lifecycle, aliases and replacements.
- Reject invalid schemas, unknown versions, dangling references, duplicate identities, incompatible namespaced extensions, and implicit executable plugins before dispatch.
- Generate the Markdown inventory from JSON and package resources so wheel and sdist fixtures load exact catalog revisions without a source checkout.

## Implementation Notes

- Use strict RED-GREEN-REFACTOR. Add a focused failing test for each repaired boundary, observe the failure, implement the smallest change, then run focused tests and the declared commands.
- The context bundle is runner-owned evidence, not a second task board or mutable executor report. It may embed only task-relevant inputs and must not leak credentials or host-absolute paths.
- Keep the portable runtime standard-library-only. Reuse existing domain/application/port boundaries and do not create another run-state store.
- Do not change the blocked TC-04 task contract, historical run bytes, task Results, acceptance checkboxes, or the active board during implementation. The runner projects state after independent verification.

## Testing

- [ ] Focused tests prove a child working root receives the exact task/plan/skill context while writes outside Allowed scope remain denied.
- [ ] Focused tests prove reused dependency edges survive human recovery and resume compatibility checks.
- [ ] Positive, compatibility and denial cases cover catalog loading, extension lifecycle, generated inventory, and installed-package resources.
- [ ] Declared verification commands pass; skipped or live-unrun cases are explicit.

## Acceptance Criteria

- [ ] AC-1 - A `feature-pipeline-skill/` executor receives an immutable, digest-bound task/plan/prompt/skill context without filesystem access outside its working root or additional write roots.
- [ ] AC-2 - Reused verification and human recovery preserve the original dependency graph, and compatible resume does not fail with `task-set-mismatch`.
- [ ] AC-3 - Every characterized operation has a catalog record; push has no routable kind and reserved commit mechanics remain disabled.
- [ ] AC-4 - Invalid schemas, dangling references, duplicate identities and incompatible extensions fail before dispatch.
- [ ] AC-5 - Installed-package fixtures load exact catalog revisions and generated views agree with JSON.

## Affected Files / Components

- `feature-pipeline-skill/pipeline_core/adapters.py`
- `feature-pipeline-skill/src/feature_pipeline/bootstrap.py`
- `feature-pipeline-skill/src/feature_pipeline/catalogs/`
- `feature-pipeline-skill/src/feature_pipeline/domain/task_kinds.py`
- `feature-pipeline-skill/tests/test_adapters.py`
- `feature-pipeline-skill/tests/test_runner_cli.py`
- `feature-pipeline-skill/tests/test_packaging.py`
- `feature-pipeline-skill/tests/test_installed_wheel.py`

## Risks / Dependencies

- This supersedes only the blocked TC-04 delivery. TC-05 continues to name TC-04 as its dependency; the existing supersession contract may satisfy that edge only after REC-01 is independently verified.
- Preserve plan/final-diff gates, no-push behavior, bounded repair, preconditions, verified reuse, immutable evidence and convergent board projection.
- Model and effort transmission are deliberately excluded; they remain RD-07 work.

## Validation Steps

1. Confirm TC-03 verified evidence and inspect the blocked TC-04 evidence plus current adapter/bootstrap behavior.
2. Prove the focused context and dependency-resume failures before implementing their corrections.
3. Exercise catalog positive and denial cases, then run each Verification commands entry from its declared working directory.
4. Compare the scoped diff with every acceptance criterion and hand off `implemented` for independent task and test verification.
