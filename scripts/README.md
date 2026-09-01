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

`--plan` points at a **JSON** file:

```json
{
  "feature": "sample-feature",
  "tasks": [
    {"id": "T-01", "type": "docs"},
    {"id": "T-02", "type": "docs", "depends_on": ["T-01"]}
  ]
}
```

…or, for a `.md` path, a **Markdown** plan (`pipeline_core/plan_md.py`, CR-05). Two shapes
are accepted, tried in order:

1. a pipe table whose header names an `ID`, a `Type`, and a `Depends on` column
   (`depends_on` / `dependencies` / `dependency` / `depends` also accepted; column order
   free; other columns ignored);
2. failing a table, every `### <ID>` heading under a `## Phase` section, with `Type` /
   `Depends on` read from the `## Execution Metadata` block of
   `<plan-dir>/tasks/<ID>_*.md`.

In both: `(none)` / `-` / empty in a Depends-on value means no dependency; the feature name
is the filename stem minus a leading `YYYY-MM-DD-`, else a slug of `# Feature: <name>`;
`--feature` overrides it. The reader fails closed (`EXIT_ERROR`) on no tasks, a task with
no ID or Type, a missing task file (shape 2), a duplicate ID, or an unknown / self
dependency. Downstream construction — selection, routing, gates, exit codes, `--dry-run`
output — is identical to the JSON path.

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

`--attest-dependency DEP_ID=SOURCE_FEATURE` is a real control here too, not a no-op: `--dry-run`
parses and validates it with the same syntax/scope/duplicate rules as `execute` mode and
resolves the source run read-only (see `execute` mode below), so `C6` reflects the attested
`DEP_ID` as satisfied (no `dependency-not-satisfied`) instead of a hardcoded "verified nothing".
An invalid or unverifiable attestation fails the dry run closed with the same stable `30`-exit
reason a real run would use, before any plan is printed.

### `execute` mode (stages 5–9)

`--mode execute` runs the minimum execution engine end to end and **stops after stage 9**.
For every dependency-ready selected task, in plan order, it: resolves the complete task
contract, initializes (or `--resume`s) the durable schema-v2 run, acquires the pipeline and
per-task write leases, dispatches the executor, runs the task-declared verification commands
as runner-owned evidence, obtains two independent read-only verifier verdicts, and repairs
within the task's bounded attempt limit — ending each task at `verified` or at a truthful
non-zero terminal state. No documentation, Graphify, final verification, release, recovery,
or archive/purge code is reachable from `execute`.

- **Plan gate.** The first executor dispatch is refused until the plan gate is satisfied:
  pass `--approve-plan`, or opt in explicitly with `--unattended`. Without either, the run
  exits `10` and writes no run state.
- **Real controls.** `--adapter {claude,codex,auto}` selects and *pins* the execution
  adapter (a resume that would switch it fails closed with `adapter-unavailable` /
  `adapter-switch`); `codex` is recognized but unavailable, so it never falls back to
  `claude`. `--max-repair-attempts N` overrides every selected task's declared bound.
  `--routine-output-byte-budget` / `--diagnostic-output-byte-budget`, `--resume`, `--task`,
  and `--through` are recorded on `run.json` with their source (`explicit` / `default`).
- **`--attest-dependency DEP_ID=SOURCE_FEATURE`.** `--task`-only, repeatable: treats `DEP_ID`
  (one of the tracked task's own declared dependencies) as satisfied because the
  already-closed run `SOURCE_FEATURE` (a bare directory name under the run-storage root)
  verified it, without ever dispatching `DEP_ID` in this run. Read-only — only the source
  run's `run.json` is read, and nothing is written to it. Resolved and validated once, at run
  creation (source exists and is readable, its `prompt_path`/`plan_path` match this run's
  exactly, it tracks `DEP_ID` at status `verified`); a resume keeps the recorded attestations,
  and a resume that also passes `--attest-dependency` must name exactly that set (any order).
  Invalid with `--through` or an unfiltered run, with a `DEP_ID` outside the tracked task's
  own dependencies, or with a `DEP_ID` repeated across flags — each denial is a distinct,
  stable `30`-exit reason (`attestation-requires-task-scope`,
  `attestation-not-a-dependency`, `duplicate-attestation-dependency`,
  `attestation-unsafe-source`, `attestation-source-missing`,
  `attestation-source-identity-mismatch`, `attestation-source-dependency-absent`,
  `attestation-source-not-verified`, `attestation-mismatch` on a mismatched resume) and
  writes no partial state. An attested dependency never satisfies an unattested sibling
  dependency.
- **Exit codes.** `0` every selected task `verified`; `10` plan gate pending; `20` a task
  ended `blocked` (repair limit reached, external `BLOCKED` verdict, malformed verifier
  evidence, out-of-scope change, live foreign lease, dependency suppression); `30` runner
  error (unknown/unavailable adapter, resume identity or adapter mismatch, incomplete task
  metadata). The run never reports `0` while any selected task is non-terminal.
- **Plan input.** `execute` needs the full execution metadata per task: a Markdown plan
  whose `tasks/<ID>_*.md` files carry an `## Execution Metadata` block, or a JSON plan whose
  entries carry `task_type` / `executor` / `allowed_scope` / `acceptance_criteria`. The
  id+type-only shape the dry run accepts is rejected.
- **Run directory.** `<profile storage>/<feature>/run.json`, or
  `<project>/.pipeline/runs/<feature>/run.json` when the first task's type has no registry
  route.

Deterministic fake-adapter acceptance fixtures live in
[`fixtures/execution/`](../fixtures/execution/); the accepted slice and every deferred
mode/stage are enumerated in `docs/acceptance/core-execution-engine.md` (parent repository).

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
| `--adapter {claude,codex,auto}` | real control in `--mode execute` | selects and pins the execution adapter for `execute`; accepted as a no-op in `plan-only` / `unattended` |
| `--max-repair-attempts N` | real control in `--mode execute` | overrides each selected task's declared repair bound for `execute`; accepted as a no-op in `plan-only` / `unattended` |
| `--routine-output-byte-budget N` / `--diagnostic-output-byte-budget N` | recorded control in `--mode execute` | persisted on `run.json` with its source; accepted as a no-op in `plan-only` / `unattended` (redaction still runs with defaults) |

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
legacy baseline. Per scenario it copies the **shared-project fixture** to a fresh temp
directory, runs the runner CLI with the scenario's arguments, and captures the generated
argv, stdout, stderr, and exit code with every host path normalized to `<project_root>` /
`<agents_root>` / `<core_root>` and no user name, machine name, credential, or token left
behind. The capture is byte-identical across runs from independent clean fixture copies.

**Fixture (CR-04).** `fixtures/shared-project/` holds the **core inputs** —
`profile.json`, `plan.json` (SF-01, SF-02), `plan-s5.json` (SF-03, SF-04) — of the shared
fixture the parallel-acceptance gate drives both runners from. It is a deliberate vendored
copy of the canonical fixture in the parent repo
(`docs/acceptance/fixtures/shared-project/`, owned by CA-04), which also carries the
Markdown board the legacy runner consumes. `tests/test_dry_run_harness.py` asserts the two
copies stay byte-identical whenever the parent path is resolvable (it skips in a standalone
child checkout). Keep them in sync: any change to the canonical fixture's core inputs must
be mirrored here.

The scenario argument table lives in `SCENARIOS` inside the module. Exit codes: S1/S3/S6/S7
→ `10` (a delivery gate is pending), S2 → `20` (unmet dependency), S4 → `1` (`--push`
denied), S5a/S5b → `30` (absolute logical path / unknown task type). The harness writes
only under a temp directory (never under `docs/acceptance/` — that is CX-01's scope), never
passes `--commit`, and passes `--push` only in the S4 denial scenario.

Regenerate the captures (prints to stdout; inspect for host leakage):

```
python scripts/dry_run_harness.py            # every S1–S7 capture
python scripts/dry_run_harness.py --scenario S3 --scenario S5
```

The same captures back the determinism and redaction assertions in
`tests/test_dry_run_harness.py`.
