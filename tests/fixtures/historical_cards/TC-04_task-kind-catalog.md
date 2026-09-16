# TC-04 - Define and package the versioned task-kind catalog

Plan - [2026-09-08-universal-pipeline-task-model-routing.md](../2026-09-08-universal-pipeline-task-model-routing.md)

## Status

- [x] To Do
- [ ] In Progress
- [ ] Done

## Execution Metadata

- Type: python
- Executor: python-executor
- Depends on: TC-03
- Allowed scope: `feature-pipeline-skill/src/feature_pipeline/catalogs/**`, `feature-pipeline-skill/src/feature_pipeline/domain/task_kinds.py`, `feature-pipeline-skill/pyproject.toml`, `feature-pipeline-skill/tests/**`
- Out of scope: `.pipeline/**`, `.gitmodules`, `.agents/**`, `docs/kanban.md`, `docs/plans/2026-09-08-universal-pipeline-task-model-routing.md`, `docs/plans/tasks/**`
- Required skills: `.agents/skills/software-development/test-driven-development/SKILL.md`, `.agents/skills/software-development/backend-dev/python/python-type-safety/SKILL.md`
- Maximum repair attempts: 2
- Documentation impact: `docs/agents/**`, `feature-pipeline-skill/README.md`
- Verification commands:
  - `feature-pipeline-skill` -> `uv run python -m unittest discover -s tests -t .`
  - `feature-pipeline-skill` -> `git diff --check`
  - `.` -> `git diff --check`
- Blocking conditions: Missing preceding verified dependency or unresolved contract/scope prerequisite; live-only work requires its concrete authorized budget.

## Purpose

Make task definitions a complete editable contract with a portable packaged source of truth.

## Context

Research work package: **R01 - Task catalog and work contracts**. Read the [proposal](../../../.prompts/2026-09-07-universal-pipeline-task-model-routing-proposal.md) and [detailed research](../../../.prompts/2026-09-07-universal-pipeline-task-model-routing-research.md), plus the tracked plan's requirement and checkpoint contracts. The research's documents-only statement describes its earlier delivery; this task specifies subsequent implementation work when execution is requested.

Block exit checkpoint: **TC-06**. Any scope expansion or new prerequisite must be routed through that review/amendment process before later work starts.

R00 review (TC-03) input: reconcile the §5.2 catalog against `docs/validation/task-model-routing/TC-01-baseline.md` section "Dispatch producer and operation inventory" (16 producer/operation rows), the exactly six production adapter call sites it records (`pipeline_core/dispatch.py:241,298` and `pipeline_core/verification.py:345,346,584,624`), and its "Route and check observations" table. Those rows are descriptive inventory labels, not proof that a task-kind registry already exists; every characterized operation, including the library/manual post-task, Graphify, release and generic-stage surfaces, needs a catalog record with truthful implementation status.

## Requirements

- Add standard-library loaders/validators and versioned JSON resources for every research §5.2 operation, reconciled with TC-01; distinguish runner, LLM and human ownership and truthful implementation status.
- Require purpose/exclusions, input/output schemas, roles, stack constraints, capabilities, risk/complexity defaults, context/budget, independence, retry/repair and verification policies, owner/provenance and lifecycle metadata.
- Support namespaced explicit extensions, revisions, aliases/replacements and draft/active/deprecated/retired history; reject unknown versions and implicit executable plugins.
- Generate a Markdown task inventory from JSON and ensure wheel/sdist resources can be loaded without a source checkout.

## Implementation Notes

- Read the linked plan's contracts and the latest preceding review report before editing. Paths below name current entry points; new modules/resources in Allowed scope are proposed deliverables, not claims that they exist.
- For behavior changes, add a focused failing regression, confirm the failure, implement the smallest change, then run the focused checks and the declared verification commands. For characterization, keep observed behavior explicit rather than adding a known-red delivery gate.
- Keep the portable runtime standard-library-only, reuse existing domain/application/port boundaries, and avoid a second state store. Limit changes to this task's responsibility even where tests use a directory glob.
- The runner owns durable state and command evidence. Hand off implemented; independent task and test verifiers decide PASS. Do not mark this task Done, write its Result, or edit later task files.

## Testing

- [ ] Focused positive, compatibility and failure/denial cases cover the changed boundary.
- [ ] Declared verification commands pass and skipped/live-unrun cases are explained.
- [ ] Independent task and test verification evidence supports the outcome.

## Acceptance Criteria

- [ ] AC-1 - Every characterized operation has a catalog record; push has no routable kind and reserved commit mechanics remain disabled.
- [ ] AC-2 - Invalid schemas, dangling references, duplicate identities and incompatible extensions fail before dispatch.
- [ ] AC-3 - Installed-package fixtures load the exact catalog revisions and generated views agree with JSON.

## Affected Files / Components

Read these entry points and the explicitly allowed destinations; inspection does not grant writes:

- `feature-pipeline-skill/src/feature_pipeline/contracts.py`
- `feature-pipeline-skill/src/feature_pipeline/domain/models.py`
- `feature-pipeline-skill/tests/test_packaging.py`
- `feature-pipeline-skill/tests/test_installed_wheel.py`

## Risks / Dependencies

- Dependency order is deliberate: the prior checkpoint can change this unstarted task before it is compiled.
- Preserve plan/final-diff gates, no-push behavior, bounded repair, preconditions, verified reuse, supersession, immutable evidence and convergent board projection.
- The shared research inputs live in ignored `.prompts/`; the tracked plan carries the essential requirements. If a prompt is unavailable at execution, restore its source before dispatch instead of guessing intent.
- No ambient provider setting, new model release or prose status is proof of entitlement, isolation or measured suitability; use explicit evidence.

## Validation Steps

1. Confirm the preceding dependency/checkpoint and inspect the current files listed below.
2. Exercise the positive and denial/failure cases in the acceptance criteria; use repository-defined commands and fake/local fixtures unless a separately budgeted live operation is explicitly authorized.
3. Run each Verification commands entry from its declared cwd; expected outcome is exit 0, with no unexplained failures or skips. Record actual argv/cwd/exit and durable evidence references through the runner.
4. Compare the scoped diff with every criterion and exclusions, then hand off implemented for independent acceptance and test verification.

## Blockers

- [2026-09-08T18-54-16Z-universal-pipeline-task-model-routing] executor reported blocked
  - Task: TC-04
  - Repair attempts: 0 of 2
  - Diagnostic: .pipeline/runs/universal-pipeline-task-model-routing/reports/TC-04/attempt-1/diagnostic-report.md
  - Recorded: 2026-09-08T18:55:44Z

- [2026-09-09T02-32-42Z-universal-pipeline-task-model-routing-r00-r01] executor reported blocked
  - Task: TC-04
  - Repair attempts: 0 of 2
  - Diagnostic: .pipeline/runs/universal-pipeline-task-model-routing-r00-r01/reports/TC-04/attempt-1/diagnostic-report.md
  - Recorded: 2026-09-09T02:47:24Z
