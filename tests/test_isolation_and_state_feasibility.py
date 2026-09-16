"""TC-02: observed strict-isolation launch surface and durable-persistence ownership.

These are *passing characterizations* of the revision that later routing/durability tasks
will change (block R00, exit review TC-03). They separate what the installed launch
surface actually enforces from what only a prompt promises, and they pin which persistence
path production writes today so RD-12 / RD-13 cut over deliberately.

Nothing here launches a live Claude/Codex process or writes into the user's ``.pipeline/``
history: the argv builders render a shell-free command line and the state repository runs
against a throwaway temp file. Change an expectation here only when the matching contract
in ``docs/validation/task-model-routing/TC-02-capabilities.md`` changes with it.
"""

from __future__ import annotations

import dataclasses
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pipeline_core.execution as execution_module
import pipeline_core.state as legacy_state_module
from pipeline_core.adapters import (
    WRITE_CAPABILITIES,
    WRITING_TOOLS,
    AdapterError,
    LaunchRequest,
    LaunchResult,
    build_claude_argv,
    build_codex_argv,
    effective_grant,
    inline_agents_json,
    scoped_add_dirs,
)

from feature_pipeline.infrastructure.state.errors import SchemaVersionError
from feature_pipeline.infrastructure.state.migration import load_state, migrate_v2_to_v3
from feature_pipeline.infrastructure.state.repository import StateRepository
from feature_pipeline.infrastructure.state.schema_v3 import (
    CONTRACT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    SUPPORTED_READ_SCHEMA_VERSIONS,
    RunStateV3,
)
from feature_pipeline.ports.state_repository import StateRevisionConflict


def _verifier_request(**overrides: object) -> LaunchRequest:
    base: dict[str, object] = dict(
        role="task_verifier",
        task_id="TC-02",
        prompt="Return the verdict.",
        report_path=Path("report.json"),
        read_only=True,
    )
    base.update(overrides)
    return LaunchRequest(**base)  # type: ignore[arg-type]


def _blank_state(**overrides: object) -> RunStateV3:
    base: dict[str, object] = dict(
        feature="tc02",
        prompt_path="prompt.md",
        plan_path=None,
        run_id="tc02-run",
        status="pending",
        contract_version=CONTRACT_SCHEMA_VERSION,
    )
    base.update(overrides)
    return RunStateV3(**base)  # type: ignore[arg-type]


class StrictIsolationPositiveObservationTests(unittest.TestCase):
    """What the installed launch surface *does* enforce (capability-matrix positive cells)."""

    def test_claude_read_only_launch_isolates_discovery_and_denies_writes(self) -> None:
        argv = build_claude_argv(_verifier_request(), executable="claude")
        # Discovery isolation: no MCP server is reachable and a machine-local settings file
        # cannot loosen the grant.
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user,project")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        # Bundle validation: the role travels as an inline ``--agents`` definition plus the
        # ``--agent`` that selects it, so the launch never depends on an on-disk
        # ``.claude/agents/<name>.md`` in the target project.
        self.assertEqual(argv[argv.index("--agents") + 1], inline_agents_json(_verifier_request(), read_only=True))
        self.assertEqual(argv[argv.index("--agent") + 1], "task-verifier")
        # Write denial reaches the CLI, not only the role prose.
        disallowed = argv[argv.index("--disallowed-tools") + 1].split(",")
        self.assertTrue(set(WRITING_TOOLS).issubset(disallowed))
        self.assertIn("Bash(git push:*)", disallowed)

    def test_codex_read_only_launch_selects_the_read_only_sandbox(self) -> None:
        argv = build_codex_argv(_verifier_request(), executable="codex")
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        writer = build_codex_argv(
            LaunchRequest(role="python-executor", task_id="TC-02", prompt="p",
                          report_path=Path("r.json"), role_grant=("write",)),
            executable="codex",
        )
        self.assertEqual(writer[writer.index("--sandbox") + 1], "workspace-write")

    def test_read_only_request_drops_every_write_capability(self) -> None:
        request = _verifier_request(role_grant=("read", "write", "run_checks", "filesystem_write"))
        self.assertEqual(set(effective_grant(request)) & WRITE_CAPABILITIES, set())
        # A read-only role cannot widen its reach into an external write root either.
        self.assertEqual(scoped_add_dirs(request, (("packages/python", "/abs/pkg"),)), ())


class StrictIsolationFailClosedTests(unittest.TestCase):
    """Adversarial cells: an unsafe launch is rejected, never silently downgraded (AC-3)."""

    def test_verifier_role_without_read_only_is_rejected(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            effective_grant(LaunchRequest(role="test_verifier", task_id="T", prompt="p",
                                          report_path=Path("r.json")))
        self.assertEqual(raised.exception.code, "verifier-not-read-only")

    def test_read_only_request_naming_a_writing_tool_is_rejected(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            build_claude_argv(_verifier_request(tools=("Read", "Write")), executable="claude")
        self.assertEqual(raised.exception.code, "read-only-write-denied")

    def test_a_request_that_pre_approves_git_push_is_rejected(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            build_claude_argv(_verifier_request(allowed_tools=("Bash(git push:*)",)), executable="claude")
        self.assertEqual(raised.exception.code, "push-denied")

    def test_fresh_session_role_may_not_carry_a_resume_id(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            build_claude_argv(
                _verifier_request(resume_session_id="prev", fresh_session=True),
                executable="claude",
            )
        self.assertEqual(raised.exception.code, "resume-forbidden")

    def test_a_shell_metacharacter_in_the_argv_is_rejected(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            build_claude_argv(_verifier_request(resume_session_id="a;rm -rf b"), executable="claude")
        self.assertEqual(raised.exception.code, "shell-metacharacter")


class StrictIsolationUnsupportedTests(unittest.TestCase):
    """Cells that stay ``stack-isolation-unsupported`` / prompt-only at this revision (AC-3)."""

    def test_launch_request_has_no_stack_bundle_or_required_read_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(LaunchRequest)}
        # No validated per-stack bundle, no enforced transitive skill-read list, no nested
        # delegate policy is representable at the launch boundary today.
        self.assertEqual(names & {"stack", "skill_bundle", "bundle_digest", "required_reads",
                                  "nested_tools"}, set())
        self.assertTrue({"model", "effort"} <= names)
        self.assertEqual({f.name for f in dataclasses.fields(LaunchResult)} & {"stack"}, set())

    def test_skill_read_enforcement_is_only_one_line_of_prompt_prose(self) -> None:
        # The inline agent definition carries a one-line charter string; there is no
        # machine-checked "these skills were actually read" gate. A read-only launch appends
        # a sentence to that prose, it does not add an enforcement flag.
        executor = LaunchRequest(role="python-executor", task_id="T", prompt="p",
                                 report_path=Path("r.json"))
        strict = inline_agents_json(executor, read_only=True)
        loose = inline_agents_json(executor, read_only=False)
        self.assertIn("read-only", strict.lower())
        self.assertNotIn("read-only", loose.lower())
        self.assertNotIn("--require-read", build_claude_argv(_verifier_request(), executable="claude"))

    def test_no_tools_launch_is_a_claude_flag_with_no_codex_equivalent(self) -> None:
        claude = build_claude_argv(_verifier_request(no_tools=True), executable="claude")
        self.assertEqual(claude[claude.index("--tools") + 1], "")
        codex = build_codex_argv(_verifier_request(no_tools=True), executable="codex")
        # Codex exposes only the read-only sandbox; there is no dedicated tool-free switch.
        self.assertNotIn("--tools", codex)
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")


class PersistenceOwnershipTests(unittest.TestCase):
    """Which writer production uses, and the migration/version boundaries a cutover keeps."""

    def test_production_execute_still_writes_through_the_legacy_run_lifecycle(self) -> None:
        source = inspect.getsource(execution_module)
        self.assertIn("RunLifecycle", source)
        self.assertIn("life.run.save()", source)
        # The transactional replacement is not wired into the pipeline_core execute path yet.
        self.assertNotIn("RunPersistence", source)
        self.assertEqual(legacy_state_module.SCHEMA_VERSION, 2)
        self.assertTrue(hasattr(legacy_state_module.Run, "save"))

    def test_the_two_schema_integers_and_read_range_are_explicit(self) -> None:
        self.assertEqual(STATE_SCHEMA_VERSION, 3)
        self.assertEqual(SUPPORTED_READ_SCHEMA_VERSIONS, frozenset({2, 3}))
        # ``schema_version`` (file shape) and ``contract_version`` (task-spec contract) are
        # distinct fields, plus a reserved ``revision`` compare-and-set token.
        fields = {f.name for f in dataclasses.fields(RunStateV3)}
        self.assertTrue({"schema_version", "contract_version", "revision"}.issubset(fields))

    def test_v2_to_v3_migration_is_in_memory_and_picks_no_new_version_number(self) -> None:
        v2 = _blank_state().to_mapping()
        v2["schema_version"] = 2
        del v2["revision"]
        del v2["contract_version"]
        migrated = migrate_v2_to_v3(v2)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["revision"], 0)
        self.assertEqual(migrated["contract_version"], CONTRACT_SCHEMA_VERSION)
        self.assertNotIn("revision", v2)  # argument left untouched
        with self.assertRaises(SchemaVersionError):
            migrate_v2_to_v3({"schema_version": 3})

    def test_load_state_fails_closed_on_v1_and_on_a_future_schema(self) -> None:
        for version in (1, 4, 99):
            with self.subTest(version=version), self.assertRaises(SchemaVersionError):
                load_state({"schema_version": version})

    def test_state_repository_is_single_writer_compare_and_set(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            repo = StateRepository()
            repo.initialise(_blank_state(), path)
            loaded = repo.load_with_revision(path)
            self.assertEqual(loaded.revision, 0)
            committed = repo.commit(_blank_state(), path, expected_revision=0)
            self.assertEqual(committed.revision, 1)
            with self.assertRaises(StateRevisionConflict):
                repo.commit(_blank_state(), path, expected_revision=0)
            with self.assertRaises(StateRevisionConflict):
                repo.initialise(_blank_state(), path)


if __name__ == "__main__":
    unittest.main()
