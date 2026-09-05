# Test suite map (QG-01)

`docs/plans/tasks/QG-01_test-suite-structure.md`. This file names every suite the flat
`tests/` package carries, gives each one a documented purpose and a deterministic discovery
command, and records where reusable test support now lives. It does **not** change what any
test asserts — no production behavior changed as part of this reorganization (AC-3).

## Canonical, full-suite command

```
feature-pipeline-skill -> uv run python -m unittest discover -s tests -t .
```

This is the declared verification command for every task in this repository and remains the
single source of truth for "did the whole suite pass". Every command below is a documented
*subset* of exactly that discovery, useful when iterating on one concern without paying for the
whole suite.

## Reusable test support (`tests/support/`)

Not a test suite — imported by test suites. Consolidates what was previously duplicated,
near-verbatim, across individual test modules:

| Module | Purpose |
| --- | --- |
| `tests/support/contracts.py` | The structural `launch(request) -> LaunchResult` contract every fake executor/verifier satisfies (`LaunchAdapterDouble`), plus `AdapterConformanceMixin` to check a fake against it. |
| `tests/support/fakes.py` | `ScriptedExecutor` / `StubVerifier` — the two-call executor/verifier test doubles shared by the repair and execution-orchestration suites (previously duplicated in `tests/test_repair.py` and `tests/test_execution.py`). |
| `tests/support/builders.py` | `initialize_run`, `bounded_repair_spec`, `narrow_blocker_spec`, `build_execution` — `Run`/`RunLifecycle`/`TaskExecution` construction helpers. |
| `tests/support/fixtures.py` | `temp_root()`, `write_file()` — filesystem fixture helpers. |
| `tests/support/assertions.py` | `assert_launch_ok`, `assert_repair_report_numbers` — small, repeated assertion helpers. |

`tests/test_repair.py` and `tests/test_execution.py` now import `ScriptedExecutor`/
`StubVerifier` and the `Run`/`TaskExecution` builders from `tests.support` instead of defining
their own copies; `tests/test_concrete_tool_grant.py` and `tests/test_runner_cli.py` continue to
import them by their original names (`tests.test_execution._spec`/`_run`/`_execution`,
`tests.test_repair.ScriptedExecutor`/`StubVerifier`) unchanged, so this consolidation is
behavior-preserving (AC-2, AC-3).

## Suites

Each suite below is a *documented grouping* of existing `tests/` modules by primary concern.
Two suites (`contract` structural-conformance, and the `support` package above) are physically
separated into their own directory as the first slice of this restructuring; the remaining
suites are documented groupings of the current flat modules, each with its own explicit,
deterministic discovery command, pending the rest of the physical move (see "Status" below).

### unit — pure domain/value-object behavior, no filesystem or subprocess

Typed state, transitions, vocabulary, and value objects with no I/O.

```
uv run python -m unittest tests.test_domain_states tests.test_primitives \
  tests.test_lease_manager tests.test_task_graph tests.test_domain_vocabulary \
  tests.test_domain_controls tests.test_domain_result_mapper tests.test_ports_clock \
  tests.test_diagnostics
```

### contract — schema, config, and structural conformance boundaries

Physically separated: `tests/contract/` (discovery: `uv run python -m unittest discover -s
tests/contract -t .`) plus the following flat modules covering profile/task/report schema and
loader contracts:

```
uv run python -m unittest tests.test_contracts tests.test_safety_ports tests.test_packaging \
  tests.test_task_files tests.test_integrations tests.test_verification_commands \
  tests.test_report_settlement tests.test_input_normalization \
  tests.test_project_profile_compatibility
```

### integration — end-to-end orchestration through real collaborators + fakes

The execution/verification/repair loop, the CLI, the pipeline engine, lifecycle, and
release/post-task stages, each driven through deterministic fake adapters rather than real
subprocesses.

```
uv run python -m unittest tests.test_execution tests.test_repair tests.test_verification \
  tests.test_bootstrap_cli tests.test_dispatch tests.test_execute_mode \
  tests.test_execute_engine_cutover tests.test_compiled_plan_cutover tests.test_pipeline_engine \
  tests.test_generic_stages tests.test_lifecycle tests.test_post_task_lifecycle \
  tests.test_release tests.test_runner_cli tests.test_plan_md \
  tests.test_transactional_state_repository tests.test_state_v2 tests.test_state_schema_v3 \
  tests.test_application_services tests.test_application_control_resolution \
  tests.test_execution_plan_compiler tests.test_adapters tests.test_commands \
  tests.test_dry_run_harness tests.test_legacy_adapter tests.test_profile_composition \
  tests.test_concrete_tool_grant
```

### installed-package — the built wheel installed in isolation

Builds and installs a wheel, then exercises it as an external consumer would — the only suite
that does not import `pipeline_core`/`feature_pipeline` from the working tree. `tests/
installed_wheel.py` is the harness (support module, not itself a test module).

```
uv run python -m unittest tests.test_installed_wheel tests.test_installed_contract \
  tests.test_src_package_compatibility
```

### platform — OS-sensitive process, filesystem, and worktree behavior

Windows/POSIX process-tree termination and repository worktree content attribution.

```
uv run python -m unittest tests.test_process_runner tests.test_worktree \
  tests.test_worktree_bounded_attribution
```

### fault-injection — adversarial, denial, and contention paths

Deliberately hostile inputs and concurrent/adversarial conditions: lease contention and stale
owners, the Git read-only allowlist, launch-control denial paths, and the eight characterized
ambiguous behaviors.

```
uv run python -m unittest tests.test_concurrency tests.test_scope_gate \
  tests.test_git_safety_allowlist tests.test_launch_controls \
  tests.test_critical_behavior_characterization
```

### performance — budget/regression assertions over hot paths

Structural and budget assertions over the bounded worktree inspector. `tests/worktree_perf.py`
is the harness (support module, not itself a test module).

```
uv run python -m unittest tests.test_worktree_performance_baseline
```

### compatibility — golden output baselines

The Phase 0 compatibility golden suite: CLI help, plan-only output, diagnostic report, exit-code
table, and the executor/verifier envelope shapes, regenerated and diffed byte-for-byte.
`tests/compat_goldens.py` is the harness (support module, not itself a test module); fixtures
live under `tests/goldens/compatibility/` and are preserved unchanged.

```
uv run python -m unittest tests.test_compatibility_goldens
```

## Status

This pass (QG-01) consolidates `tests/support/` (AC-2), adds the physically separated
`tests/contract/` conformance suite, and documents the full suite map with deterministic,
explicit discovery commands over the existing flat layout (AC-1). Moving every remaining module
into its documented suite directory is the next slice of this restructuring; do it in small,
count-and-skip-verified batches per module (Implementation Notes), not as one mechanical pass.

## Worktree performance recipe

`tests.test_worktree_performance_baseline` keeps the WT-01 retained-heap guard at a >=10x
bounded-versus-legacy ratio. Its deterministic large recipe includes 600 tracked source files
and a 32 MiB tracked binary. The binary is intentionally not changed by the executor window:
the bounded marker retains only candidate metadata and open dirty bodies, while the legacy
snapshot retains the binary body. This keeps the guard structural and stable on Windows CPython,
where transient index-metadata allocations can otherwise obscure the retained-content gap in a
small fixture.
