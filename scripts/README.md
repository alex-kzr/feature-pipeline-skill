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
verdict. `--approve-plan`, `--approve-final-diff`, and `--commit-approved-manifest` each
advance the matching gate in the dry-run plan and **never bypass an earlier, still-pending
gate**. `--commit`, `--commit-approved-manifest` never bypass the plan gate and this runner
has no commit code path. `--push` is refused before any run artifact exists, with exit code
`1` and the message
`push is denied at this stage and the runner has no push code path.`

Exit codes: `10` ok / a delivery gate is pending — **every non-blocked run, including a
`--dry-run`, returns `10`** (the legacy dry-run contract and the MI-01 exit-code table:
"`10` … preserve"); `20` blocked (unmet dependency, held lease); `30` error (invalid anchor
or profile, unroutable task); `1` `--push` refused (outside the `{0,10,20,30}` baseline set
on purpose — the MI-02 record shows `Exit 1`). `0` is not returned by a run; it appears only
for the read-only `--status` inspector.

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
A non-blocked `--dry-run` exits `10` (a delivery gate is pending), matching the legacy
runner; a blocked one still exits `20`, and an invalid anchor / profile / route still `30`.
The plan has the fixed sections of the parallel-acceptance comparison matrix: `C1` selected
task IDs, `C2` per-task resolved route, `C3` generated shell-free argv, `C4` pending delivery
gates in order, `C5` exit code (`Exit code: 10 (a delivery gate is pending)`), `C6` planned
state transitions, `C7` redacted evidence manifest, `C8` safety posture. Host paths appear
only as `<project_root>`, `<agents_root>`, and `<core_root>`.

### Compatibility surface (flags forwarded from the legacy CLI)

The compatibility launcher forwards the legacy `run` operational surface verbatim. Every
MI-01 **core**-classified flag below is accepted by this CLI — implemented, or accepted as a
documented no-op — so a forwarded legacy invocation never dies with an argparse error inside
the core.

| Flag | Decision | Rationale |
|---|---|---|
| `--approve-final-diff` | implemented | advances the `final-diff` gate in the C4 plan, exactly as `--approve-plan` advances the plan gate; never bypasses an earlier gate; no write / no commit path |
| `--commit-approved-manifest` | implemented | advances the `commit` gate in the C4 plan; behaves like `--commit` on a real run (never bypasses the plan gate, never writes) |
| `--status` | implemented | resolves the anchors + profile + plan, prints the recorded run state (`feature`, `status`, per-task status) or `no recorded run`, and exits `0`; read-only |
| `--unattended` | implemented | alias of `--mode unattended` |
| `--verbose` / `--quiet` | accepted no-op | the portable core emits only the deterministic plan / advisory text; reporter verbosity is owned by the project launcher |
| `--adapter {claude,codex,auto}` | accepted no-op | adapter routing is not wired in the portable core; the project launcher owns it (OF-02) |
| `--max-repair-attempts N` | accepted no-op | the repair-loop bound is not wired in the portable core; the project launcher owns it |
| `--routine-output-byte-budget N` / `--diagnostic-output-byte-budget N` | accepted no-op | output budgeting is not wired in the portable core (redaction still runs with defaults) |

Known **unimplemented** surface (not stubbed): the legacy `archive` / `purge` / `retry`
subcommands have no lifecycle-maintenance counterpart in the portable core yet. Passing one
still reaches an argparse error. This is handed to the MI-01 amendment tracked by OI-02 /
CA-03; it is out of scope for CR-03.

Recovery / maintenance flags (`maintenance apply`, `--unblock`, …) are **deliberately absent**
from the core CLI and its `--help` — they stay launcher-only (OF-02 §4).

```
python scripts/run_pipeline.py \
  --project-root <dir> --agents-root <dir> --core-root <dir> \
  --profile <rel> --plan <rel> --dry-run --mode plan-only
```

## `dry_run_harness.py`

The core-side dry-run acceptance harness. It produces the deterministic, redacted S1–S7
dry-run captures that the parallel-acceptance gate (CA-02 / CX-01) compares against the
legacy baseline. Per scenario it copies a portable fixture to a fresh temp directory, runs
the runner CLI with the scenario's arguments, and captures the generated argv, stdout,
stderr, and exit code with every host path normalized to `<project_root>` / `<agents_root>`
/ `<core_root>` and no user name, machine name, credential, or token left behind. The
capture is byte-identical across runs from independent clean fixture copies.

The scenario argument table lives in `SCENARIOS` inside the module — the single source of
truth so the legacy baseline and CX-01 drive both runners with identical arguments. The
harness writes only under a temp directory (never under `docs/acceptance/` — that is
CX-01's scope), never passes `--commit`, and passes `--push` only in the S4 denial
scenario.

Regenerate the captures (prints to stdout; inspect for host leakage):

```
python scripts/dry_run_harness.py            # every S1–S7 capture
python scripts/dry_run_harness.py --scenario S3 --scenario S5
```

The same captures back the determinism and redaction assertions in
`tests/test_dry_run_harness.py`.
