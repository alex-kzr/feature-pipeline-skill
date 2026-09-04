# `fixtures/state/` — durable run-state fixtures for the schema-v3 boundary (DS-02)

These fixtures are the **preserved originals** the schema-v3 reader/writer and the
deterministic v2 -> v3 migration are tested against
(`docs/plans/tasks/DS-02_state-schema-v3-migration.md`, Phase 5 of
`docs/plans/2026-09-03-feature-pipeline-refactor.md`).

They are immutable: the migration never rewrites them, and a failing load must leave the
file byte-for-byte unchanged (AC-2, AC-3). Regenerating them by hand is a deliberate,
reviewed act, never a side effect of a test run.

## `v2/` — valid schema-v2 run states (AC-1: migrate without data loss)

| File | Shape it pins |
|---|---|
| `run-state-basic.json` | The `tests/goldens/compatibility/run-state-v2.json` shape with concrete ids/timestamps — four tasks spanning `verified` / `implemented` / `blocked` / `pending`, one recorded command, a `stages` index, a sourced `max_repair_attempts` control. |
| `run-state-full.json` | Every optional schema-v2 task field populated at once — `promotion`, `attested_dependencies`, non-default launch generations, a captured `execution_evidence` implementation block, `external_launch_failures`, `maintenance_audit`, `environment`, `artifacts`. |

## `malformed/` — corrupt or future state (AC-2: fail at load with field-level diagnostics)

| File | Expected failure `code` | Offending `field` |
|---|---|---|
| `unknown-schema-version.json` | `unsupported-schema-version` | `schema_version` |
| `missing-required-field.json` | `missing-field` | `tasks[1].id` |
| `wrong-field-type.json` | `invalid-field-type` | `tasks[0].attempts` |
| `invalid-status.json` | `invalid-field-value` | `tasks[0].status` |
| `invalid-nested-evidence.json` | `invalid-field-type` | `tasks[0].execution_evidence.implementation` |
| `unknown-field.json` | `unknown-field` | `tasks[0].mystery_field` |
