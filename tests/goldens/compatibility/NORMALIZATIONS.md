# Compatibility goldens — source scenarios and normalizations

Every file in this directory is a **reviewed** capture of a declared public output of the
portable core, frozen by BL-01 before any structural refactoring. `tests/compat_goldens.py`
regenerates each capture; `tests/test_compatibility_goldens.py` diffs the regeneration
against the committed file. A diff here is a compatibility decision — review it, then
regenerate deliberately with `python -m tests.compat_goldens --write`. The goldens are
**never** updated automatically during a later refactor.

## Normalizations applied to every capture

The harness only rewrites values that are *intentionally* nondeterministic. Everything else
is compared byte-for-byte.

| Token | Replaces | Why it is nondeterministic |
|---|---|---|
| `<project_root>` | the seeded fixture's project root (real temp path) | a fresh temp dir per run |
| `<agents_root>` | the seeded `agents/` anchor | a fresh temp dir per run |
| `<core_root>` | the seeded `core/` anchor | a fresh temp dir per run |
| `<workdir>` | the enclosing per-scenario temp directory | a fresh temp dir per run |
| `<home>` / `<redacted>` / `<tmp>` | applied by `pipeline_core.redaction.build_rules()` | host user home, secrets, OS temp root |
| `<python>` | the test interpreter path (`sys.executable`, incl. its post-redaction `<home>…python(.exe)` form in a diagnostic's recorded argv) | uv-managed interpreter path is host-specific |
| `<timestamp>` | any `YYYY-MM-DDTHH:MM:SSZ` / `HH-MM-SS` UTC stamp | wall-clock time |
| `<run-id>` | the `"run_id"` value in `run.json` (a `<timestamp>-<feature>` string) | derived from wall-clock time |
| `0` (JSON `"duration"`) / `(<duration>s)` (diagnostic) | a recorded command's wall-clock duration | timing jitter |

CRLF is normalized to LF so the fixtures are identical on Windows and Linux.

## Per-golden source scenario

All CLI scenarios run `pipeline_core.runner_cli.main` in-process over a copy of
`fixtures/shared-project/` (one closed registry; `SF-01`/`SF-02` routable, `plan-s5.json`'s
`SF-04` deliberately unroutable). The non-CLI captures build their subject through the
public API only.

| Golden | Scenario | Frozen exit |
|---|---|---|
| `cli-help.txt` | `run_pipeline.py --help` — the full option surface and every help string | 0 |
| `plan-only-plan.txt` | `--plan plan.json --dry-run --mode plan-only` — the C1–C8 deterministic dry-run plan for the routable sub-graph | 10 |
| `denial-execute-plan-gate.txt` | `--plan plan-exec.json --mode execute` with a full-metadata plan and the plan gate unsatisfied — no executor dispatched, no run state written | 10 |
| `denial-push.txt` | `--push` — refused before any anchor is resolved, verbatim baseline message | 1 |
| `error-unknown-task-type.txt` | `--plan plan-s5.json --task SF-04 --dry-run` — a task whose type has no registry route; fails closed `unknown-task-type` | 30 |
| `error-absolute-logical-path.txt` | `--plan /srv/shared-project/plan.json --dry-run` — an absolute logical path rejected before load (`--plan must be relative`) | 30 |
| `blocked-unmet-dependency.txt` | `--plan plan.json --task SF-02 --dry-run` — `SF-02` selected while `SF-01` is not verified; `dependency-not-satisfied` | 20 |
| `status-no-recorded-run.txt` | `--plan plan.json --status` — the read-only inspector with nothing recorded yet | 0 |
| `run-state-v2.json` | a `schema_version: 2` `run.json` built via `Run` public API: `T-01` verified (two PASS verdicts), `T-02` implemented, `T-03` blocked (external cause), `T-04` pending; one sourced control, one recorded command | — |
| `diagnostic-report.md` | `write_diagnostic_report` over a running task with one recorded command — the five contract sections in fixed order | — |
| `executor-status-envelope.json` | the strict executor launch status envelope — exactly `pipeline_core.reports.ENVELOPE_KEYS`, a known `STATUS_TOKENS` value | — |
| `verifier-verdict-envelope.json` | the strict verifier verdict envelope — a single `verdict` key, a known `VERIFY`/`VERDICTS` token | — |
| `exit-code-table.txt` | the observed exit code of each CLI scenario above, asserted against the frozen `{0,1,10,20,30}` contract | — |
