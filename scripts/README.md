# Scripts

Command-line entry points for the portable core.

Every entry point accepts explicit `project_root`, `agents_root`, and `core_root` anchors and
never infers a project layout.

## `run_pipeline.py`

The portable core runner. It is a thin shim over `pipeline_core.runner_cli`; all behavior and
its tests live there.

### Anchor contract

| Argument | Meaning |
|---|---|
| `--project-root DIR` | filesystem root the project's logical paths resolve below |
| `--agents-root DIR` | filesystem root the shared agents/skills tree resolves below |
| `--core-root DIR` | filesystem root this portable core resolves below |
| `--profile REL` | profile file, relative to `--project-root` |
| `--project-skill REL` | project skill directory, relative to `--agents-root` |
| `--plan REL`, `--prompt REL` | relative to the resolved project directory |

`--profile`, `--project-skill`, `--plan`, and `--prompt` are **logical** paths: an absolute
path, a `~` prefix, a `..` segment, or a `\` separator is rejected before anything is loaded
(fail closed, non-zero exit, message with no host path). Each logical path is resolved only
below its declared anchor; a value that escapes its anchor is rejected.

### Run selection and gates

`--task ID`, `--through ID`, `--resume`, and `--mode {plan-only,unattended}` select scope.
Delivery gates are closed by default and ordered `plan`, `final-diff`, `commit`, verification
verdict. `--approve-plan` satisfies only the plan gate. `--commit` never bypasses the plan
gate and this runner has no commit code path. `--push` is refused before any run artifact
exists, with exit code `1` and the message
`push is denied at this stage and the runner has no push code path.`

Exit codes: `0` ok, `10` a delivery gate is pending, `20` blocked (unmet dependency, held
lease), `30` error (invalid anchor or profile, unroutable task).

### Plan file

`--plan` points at a JSON file in the project:

```json
{
  "feature": "sample-feature",
  "tasks": [
    {"id": "T-01", "type": "docs"},
    {"id": "T-02", "type": "docs", "depends_on": ["T-01"]}
  ]
}
```

Each task `type` must be a core task type and must have a route in the profile's registry.
`design` and any unknown type fail closed (`unresolved-executor` / `unknown-task-type`).

### `--dry-run`

Prints a deterministic, redacted plan and writes nothing outside a temporary run directory.
The plan has the fixed sections of the parallel-acceptance comparison matrix: `C1` selected
task IDs, `C2` per-task resolved route, `C3` generated shell-free argv, `C4` pending delivery
gates in order, `C5` exit code, `C6` planned state transitions, `C7` redacted evidence
manifest, `C8` safety posture. Host paths appear only as `<project_root>`, `<agents_root>`,
and `<core_root>`.

```
python scripts/run_pipeline.py \
  --project-root <dir> --agents-root <dir> --core-root <dir> \
  --profile <rel> --plan <rel> --dry-run --mode plan-only
```
