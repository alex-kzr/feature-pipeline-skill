"""Claude adapter argv/grant rules, session capture, and deterministic adapter resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from pipeline_core.adapters import (
    EXIT_TIMEOUT,
    AdapterError,
    ClaudeAdapter,
    CLAUDE_ISOLATION_CAPABILITIES,
    CodexAdapter,
    DockerCodexAdapter,
    CODEX_ISOLATION_CAPABILITIES,
    CompletedProcess,
    ContextEntry,
    ExecutorContextBundle,
    LaunchRequest,
    LiveProbeRequest,
    LaunchComposition,
    STRICT_ISOLATION_CAPABILITIES,
    build_codex_argv,
    build_claude_argv,
    check_command_allowances,
    effective_grant,
    on_disk_agent_name,
    parse_result_text,
    parse_session_id,
    parse_codex_final_result,
    require_strict_isolation,
)
from pipeline_core.adapter_resolution import (
    AdapterResolution,
    AdapterResolutionError,
    ensure_pinned_adapter,
    pin_adapter,
    pinned_adapter,
    resolve_adapter,
)
from pipeline_core.roles import compose_role
from pipeline_core.state import Run
from feature_pipeline.contracts import Profile
from feature_pipeline.ports.adapters import IsolationCapabilityProof
from tests.support.isolation import proven_isolation_capabilities


# --- fixtures --------------------------------------------------------------------------------

_FAKE_CLAUDE = """\
import json, sys
sys.stdin.read()
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "implemented\\n\\nreport body", "session_id": "sess-CLAUDE-1",
}))
"""

_SLOW_CLAUDE = "import time\ntime.sleep(30)\n"

_FAKE_CODEX = """\
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thread-CODEX-1"}))
print(json.dumps({"type": "item.completed", "item": {
    "type": "agent_message", "text": "implemented\\n\\nreport body",
}}))
"""

#: The literal oxidium-forge PCC-02 reproduction evidence (RDS-07): the raw
#: ``--output-format json`` wrapper the CLI actually returned, its own ``result`` field
#: carrying the clean, correctly-shaped status envelope the agent sent.
_OXIDIUM_FORGE_WRAPPER = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": '{"role": "executor", "status": "blocked", "task_id": "PCC-01", "attempt": 1}',
    "session_id": "sess-pcc01", "uuid": "u-1", "duration_ms": 100, "duration_api_ms": 90,
    "total_cost_usd": 0.01, "usage": {"input_tokens": 1}, "modelUsage": {}, "num_turns": 1,
    "is_error_status": None, "api_error_status": None, "stop_reason": "end_turn",
    "subtype_reason": None, "permission_denials": [], "queued_turn_count": 0,
    "fast_mode_state": None, "fast_mode_disabled_reason": None, "subagent_stats": {},
    "terminal_reason": None, "time_to_request_ms": 0, "ttft_ms": 0, "ttft_stream_ms": 0,
})

_WRAPPER_CLAUDE = f"""\
import sys
sys.stdin.read()
print({_OXIDIUM_FORGE_WRAPPER!r})
"""


def _fake_executable(directory: Path, body: str, name: str) -> list[str]:
    script = directory / name
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


def _profile() -> Profile:
    return Profile.from_data({
        "version": 1,
        "name": "portable-example",
        "logical_paths": {"project": "workspace", "agents": "shared", "core": "core"},
        "role_grants": {
            "executor": ["read", "write", "run_checks"],
            "task_verifier": ["read"],
            "test_verifier": ["read", "run_checks"],
        },
        "stages": [{"name": "implement", "subagents": ["executor"], "argv": ["run"]}],
    })


#: An explicit, all-tokens-True capability grant standing in for a separately budgeted
#: R03 live-probe measurement, used only where a test's own purpose is unrelated to the
#: isolation-capability gate itself (e.g. Codex JSONL parsing).
_PROVEN_ISOLATION_CAPABILITIES = proven_isolation_capabilities("codex")

#: The same stand-in for Claude, used where a test's own purpose (argv shape, timeout
#: handling, prompt enrichment, role/bundle-substitution denial, ...) is unrelated to the
#: isolation-capability gate itself.
_PROVEN_CLAUDE_ISOLATION_CAPABILITIES = proven_isolation_capabilities("claude")


def _request(role: str, **overrides: object) -> LaunchRequest:
    base: dict[str, object] = dict(
        role=role, task_id="RDS-03", prompt="do the work", report_path=Path("report.md"),
    )
    base.update(overrides)
    return LaunchRequest(**base)  # type: ignore[arg-type]


def _run(root: Path) -> Run:
    prompt = root / "prompts" / "feature.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("feature", encoding="utf-8")
    run = Run.create("adapters", prompt, None, root / "storage" / "adapters", root)
    run.add_task("RDS-03", depends_on=[])
    run.add_task("RDS-04", depends_on=["RDS-03"])
    return run


# --- argv + effective grant ---------------------------------------------------------------------


class ClaudeArgvTests(unittest.TestCase):
    def test_characterized_executor_argv_is_non_interactive_and_shell_free(self) -> None:
        argv = build_claude_argv(
            _request("executor", role_grant=("read", "run_checks", "write"),
                     tools=("Read", "Edit", "Bash")),
            executable="claude",
            settings_path="/repo/.claude/settings.json",
        )
        self.assertEqual(argv[:4], ["claude", "-p", "--output-format", "json"])
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "executor")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Bash")
        self.assertIn("Bash(git push:*)", argv[argv.index("--disallowed-tools") + 1])
        for token in argv:
            self.assertFalse(any(ch in token for ch in "|;<>`\n"), token)

    def test_verifier_agent_flag_is_the_hyphenated_on_disk_name(self) -> None:  # RDS-14
        for normalized, on_disk in (
            ("task_verifier", "task-verifier"),
            ("test_verifier", "test-verifier"),
        ):
            argv = build_claude_argv(
                _request(normalized, read_only=True, no_tools=True), executable="claude")
            self.assertEqual(argv[argv.index("--agent") + 1], on_disk)

    def test_on_disk_agent_name_denormalizes_verifiers_and_passes_executors_through(self) -> None:
        self.assertEqual(on_disk_agent_name("task_verifier"), "task-verifier")
        self.assertEqual(on_disk_agent_name("test-verifier"), "test-verifier")
        self.assertEqual(on_disk_agent_name("python-executor"), "python-executor")
        self.assertEqual(on_disk_agent_name("rust-executor"), "rust-executor")

    def test_resume_appends_the_session_id(self) -> None:
        argv = build_claude_argv(_request("executor", resume_session_id="sess-9"))
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-9")

    def test_effective_grant_drops_write_for_a_read_only_request(self) -> None:
        profile = _profile()
        executor_grant = compose_role(profile, "executor", ["read", "write", "run_checks"], [])
        request = _request("executor", read_only=True, role_grant=executor_grant)
        self.assertEqual(effective_grant(request), ("read", "run_checks"))
        argv = build_claude_argv(request)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        disallowed = argv[argv.index("--disallowed-tools") + 1]
        for writer in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(writer, disallowed)

    def test_verifier_grant_cannot_write_even_when_the_request_asks_for_more(self) -> None:
        profile = _profile()
        verifier_grant = compose_role(profile, "task_verifier", ["read"], [])
        request = _request(
            "task-verifier", task_id="RDS-03", read_only=True, role_grant=verifier_grant,
            tools=("Read",), disallowed_tools=(),
        )
        argv = build_claude_argv(request)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        self.assertNotIn("Edit", argv[argv.index("--tools") + 1])
        self.assertIn("Write", argv[argv.index("--disallowed-tools") + 1])

    def test_verifier_request_that_is_not_read_only_is_rejected(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            build_claude_argv(_request("test_verifier", role_grant=("read",)))
        self.assertEqual(ctx.exception.code, "verifier-not-read-only")

    def test_read_only_request_naming_a_writing_tool_is_denied(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            build_claude_argv(_request("executor", read_only=True, tools=("Read", "Edit")))
        self.assertEqual(ctx.exception.code, "read-only-write-denied")

    def test_push_escalation_in_allowed_tools_is_denied(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            build_claude_argv(_request("executor", allowed_tools=("Bash(git push origin main)",)))
        self.assertEqual(ctx.exception.code, "push-denied")

    def test_a_fresh_session_role_may_not_be_resumed(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            build_claude_argv(_request("executor", fresh_session=True, resume_session_id="s-1"))
        self.assertEqual(ctx.exception.code, "resume-forbidden")

    def test_shell_metacharacters_in_a_tool_policy_are_refused(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            build_claude_argv(_request("executor", tools=("Bash", "Foo;rm -rf x")))
        self.assertEqual(ctx.exception.code, "shell-metacharacter")

    def test_no_tools_launch_grants_nothing(self) -> None:
        argv = build_claude_argv(_request("executor", no_tools=True, tools=("Read",)))
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--allowed-tools", argv)

    def test_inline_agents_definition_is_emitted_and_matches_the_selected_agent(self) -> None:
        # RDS-06: the role is defined inline so a launch needs no on-disk .claude/agents file
        # in the target project. --agents carries exactly the name --agent then selects.
        for role, on_disk in (
            ("executor", "executor"),
            ("python-executor", "python-executor"),
            ("task_verifier", "task-verifier"),
            ("test-verifier", "test-verifier"),
        ):
            with self.subTest(role=role):
                argv = build_claude_argv(
                    _request(role, read_only="verifier" in role, no_tools="verifier" in role,
                             role_grant=("read",)))
                self.assertIn("--agents", argv)
                self.assertLess(argv.index("--agents"), argv.index("--agent"))
                definition = json.loads(argv[argv.index("--agents") + 1])
                self.assertEqual(list(definition), [on_disk])
                self.assertEqual(argv[argv.index("--agent") + 1], on_disk)
                self.assertIn("description", definition[on_disk])
                self.assertTrue(definition[on_disk]["prompt"].strip())

    def test_inline_agents_json_is_shell_free_and_ascii(self) -> None:
        argv = build_claude_argv(_request("task_verifier", read_only=True, no_tools=True,
                                          role_grant=("read",)))
        payload = argv[argv.index("--agents") + 1]
        self.assertTrue(payload.isascii())
        for bad in ("|", "&", ";", "<", ">", "`", "$(", "\n", "\r"):
            self.assertNotIn(bad, payload)
        # a verifier's inline charter states the read-only posture
        definition = json.loads(payload)
        self.assertIn("read-only", definition["task-verifier"]["prompt"].lower())

    def test_inline_agent_charter_covers_unknown_executor_roles(self) -> None:
        argv = build_claude_argv(_request("frontend-executor", role_grant=("read", "write"),
                                          tools=("Read", "Edit")))
        definition = json.loads(argv[argv.index("--agents") + 1])
        self.assertIn("executor", definition["frontend-executor"]["prompt"].lower())


class CheckCommandAllowanceTests(unittest.TestCase):
    """RLC-01 AC-1: a Claude executor with ``run_checks`` is granted only exact, task-declared,
    shell-free ``Bash(<argv>)`` allowances whose CWD is its working root."""

    def test_qualifying_command_yields_one_exact_bash_allowance(self) -> None:
        allowed = check_command_allowances(
            [(".", ("uv", "run", "python", "-m", "unittest"))],
            role_grant=("read", "run_checks"), working_root=".",
        )
        self.assertEqual(allowed, ("Bash(uv run python -m unittest)",))

    def test_command_from_a_different_cwd_than_the_working_root_is_excluded(self) -> None:
        allowed = check_command_allowances(
            [("other-package", ("uv", "run", "pytest"))],
            role_grant=("read", "run_checks"), working_root="feature-pipeline-skill",
        )
        self.assertEqual(allowed, ())

    def test_without_run_checks_the_role_gets_no_allowance_at_all(self) -> None:
        allowed = check_command_allowances(
            [(".", ("uv", "run", "python", "-m", "unittest"))],
            role_grant=("read", "write"), working_root=".",
        )
        self.assertEqual(allowed, ())

    def test_a_push_command_is_never_approved_even_if_declared(self) -> None:
        allowed = check_command_allowances(
            [(".", ("git", "push", "origin", "main"))],
            role_grant=("read", "run_checks"), working_root=".",
        )
        self.assertEqual(allowed, ())

    def test_a_command_carrying_a_shell_metacharacter_is_never_approved(self) -> None:
        allowed = check_command_allowances(
            [(".", ("bash", "-c", "rm -rf / && echo pwned"))],
            role_grant=("read", "run_checks"), working_root=".",
        )
        self.assertEqual(allowed, ())

    def test_multiple_qualifying_commands_each_get_their_own_exact_allowance(self) -> None:
        allowed = check_command_allowances(
            [
                (".", ("uv", "run", "python", "-m", "unittest")),
                (".", ("uv", "run", "ruff", "check")),
                ("other", ("uv", "run", "pytest")),
            ],
            role_grant=("read", "run_checks"), working_root=".",
        )
        self.assertEqual(
            allowed,
            ("Bash(uv run python -m unittest)", "Bash(uv run ruff check)"),
        )

    def test_windows_backslash_cwd_normalizes_the_same_as_the_working_root(self) -> None:
        allowed = check_command_allowances(
            [("feature-pipeline-skill", ("uv", "run", "python", "-m", "unittest"))],
            role_grant=("read", "run_checks"), working_root="feature-pipeline-skill\\",
        )
        self.assertEqual(allowed, ("Bash(uv run python -m unittest)",))

    def test_derived_allowances_flow_through_build_claude_argv_and_still_deny_push(self) -> None:
        request = _request(
            "executor", role_grant=("read", "run_checks"), tools=("Read", "Bash"),
            allowed_tools=("Bash(uv run python -m unittest)",),
        )
        argv = build_claude_argv(request)
        self.assertIn("Bash(uv run python -m unittest)", argv[argv.index("--allowed-tools") + 1])
        self.assertIn("Bash(git push:*)", argv[argv.index("--disallowed-tools") + 1])


class ClaudeExecutorResolutionTests(unittest.TestCase):
    """REC-07: availability resolution must honor the inline ``--agents`` launch contract, so a
    concrete ``*-executor`` role the adapter already defines inline resolves without an ambient
    ``.claude/agents/<role>.md`` file — while a custom non-executor role still fails closed."""

    def test_custom_concrete_executor_resolves_without_a_project_local_agent_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with unittest.mock.patch("pathlib.Path.home", return_value=root):
                adapter = ClaudeAdapter(executable="claude", working_root=root, env={})
                self.assertTrue(adapter.can_resolve_executor("tooling-executor"))
                self.assertTrue(adapter.can_resolve_executor("python-executor"))

    def test_resolved_inline_executor_argv_keeps_the_exact_role_and_stays_shell_free(self) -> None:
        argv = build_claude_argv(
            _request("tooling-executor", role_grant=("read", "run_checks", "write"),
                     tools=("Read", "Bash", "Edit")),
            executable="claude",
        )
        self.assertEqual(argv[argv.index("--agent") + 1], "tooling-executor")
        self.assertLess(argv.index("--agents"), argv.index("--agent"))
        definition = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(list(definition), ["tooling-executor"])
        self.assertIn("executor", definition["tooling-executor"]["prompt"].lower())
        for token in argv:
            for bad in ("|", "&", ";", "<", ">", "`", "$(", "\n", "\r"):
                self.assertNotIn(bad, token)

    def test_custom_non_executor_role_without_an_agent_file_stays_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with unittest.mock.patch("pathlib.Path.home", return_value=root):
                adapter = ClaudeAdapter(executable="claude", working_root=root, env={})
                self.assertFalse(adapter.can_resolve_executor("tooling-maintainer"))
                self.assertFalse(adapter.can_resolve_executor("release-manager"))

    def test_inline_executor_resolution_ignores_builtin_disable_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with unittest.mock.patch("pathlib.Path.home", return_value=root):
                adapter = ClaudeAdapter(
                    executable="claude", working_root=root,
                    env={"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1"})
                self.assertTrue(adapter.can_resolve_executor("tooling-executor"))

    def test_a_verifier_role_is_not_treated_as_a_concrete_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with unittest.mock.patch("pathlib.Path.home", return_value=root):
                adapter = ClaudeAdapter(executable="claude", working_root=root, env={})
                self.assertFalse(adapter.can_resolve_executor("task-verifier"))
                self.assertFalse(adapter.can_resolve_executor("test_verifier"))


class CodexArgvTests(unittest.TestCase):
    def test_write_launch_grants_only_the_external_root_named_by_allowed_scope(self) -> None:
        adapter = CodexAdapter(
            executable="codex",
            working_root="C:/repo",
            scope_roots=((".agents", "C:/agents"), ("shared-core", "C:/core")),
        )

        argv = adapter.plan(
            _request(
                "executor",
                role_grant=("read", "write"),
                allowed_scope=(".agents/skills/example/SKILL.md",),
            )
        )

        self.assertEqual(
            [argv[index + 1] for index, value in enumerate(argv) if value == "--add-dir"],
            [str(Path("C:/repo")), str(Path("C:/agents/skills/example"))],
        )

    def test_writing_launch_grants_the_workspace_and_its_parent_for_windows_traversal(self) -> None:
        """A Codex executor grants its disposable workspace and traversal parent.

        Windows Codex sandbox enforces reachable roots when changing directory: granting
        only `--cd` leaves its parent inaccessible and causes `Set-Location` to fail.
        """
        adapter = CodexAdapter(
            executable="codex",
            working_root="C:/Temp/feature-pipeline-executor-example/workspace/feature-pipeline-skill",
        )

        argv = adapter.plan(_request("executor", role_grant=("read", "write")))

        self.assertEqual(argv[argv.index("--sandbox") + 1], "danger-full-access")
        self.assertEqual(
            [argv[index + 1] for index, value in enumerate(argv) if value == "--add-dir"],
            [
                str(Path("C:/Temp/feature-pipeline-executor-example/workspace")),
                str(Path("C:/Temp/feature-pipeline-executor-example/workspace/feature-pipeline-skill")),
            ],
        )

    def test_read_only_launch_does_not_grant_external_roots(self) -> None:
        adapter = CodexAdapter(
            executable="codex",
            working_root="C:/repo",
            scope_roots=((".agents", "C:/agents"),),
        )

        argv = adapter.plan(
            _request(
                "task_verifier",
                read_only=True,
                allowed_scope=(".agents/skills/example/SKILL.md",),
            )
        )

        self.assertNotIn("--add-dir", argv)

    def test_executor_argv_uses_exec_json_workspace_write_and_resolved_grants(self) -> None:
        argv = build_codex_argv(
            _request("executor", role_grant=("read", "run_checks", "write")),
            executable="codex",
            working_root="C:/repo",
            add_dirs=("C:/agents", "C:/core"),
        )
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("--cd") + 1], "C:/repo")
        self.assertEqual(
            [argv[index + 1] for index, value in enumerate(argv) if value == "--add-dir"],
            ["C:/agents", "C:/core"],
        )

    def test_tool_free_status_continuation_is_rejected_without_a_codex_enforcement_flag(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            build_codex_argv(
            _request(
                "task_verifier",
                read_only=True,
                no_tools=True,
                resume_session_id="thread-9",
            ),
            executable="codex",
            working_root="C:/repo",
            add_dirs=("C:/agents", "C:/core"),
            )
        self.assertEqual(raised.exception.code, "no-tools-unsupported")


# --- process capture --------------------------------------------------------------------------


class ClaudeLaunchTests(unittest.TestCase):
    def test_executor_launch_passes_exact_granted_check_argv_to_the_worker(self) -> None:
        """The actual production launcher argv, not only ``plan()``, carries the narrow
        command grant needed for a runner-declared executor check."""
        observed: list[list[str]] = []

        def runner(argv, **_kwargs):
            observed.append(list(argv))
            return CompletedProcess(
                0,
                json.dumps({"result": "implemented", "session_id": "session-1"}),
                "",
            )

        adapter = ClaudeAdapter(
            executable="claude", runner=runner,
            isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
        )
        adapter.launch(_request(
            "executor",
            role_grant=("read", "write", "run_checks"),
            tools=("Read", "Edit", "Bash"),
            allowed_tools=("Bash(uv run python -m unittest)",),
        ))

        self.assertEqual(len(observed), 1)
        argv = observed[0]
        self.assertEqual(argv[argv.index("--allowed-tools") + 1], "Bash(uv run python -m unittest)")
        self.assertEqual(argv[argv.index("--disallowed-tools") + 1], "Bash(git push:*)")

    def test_characterized_launch_returns_exit_output_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = _fake_executable(Path(directory), _FAKE_CLAUDE, "fake_claude.py")
            adapter = ClaudeAdapter(
                executable=executable,
                isolation_capabilities=proven_isolation_capabilities(
                    "claude", runtime="\0".join(executable)
                ),
            )
            self.assertTrue(adapter.available())
            plan = adapter.plan(_request("executor"))
            self.assertEqual(plan[:1], [sys.executable])
            self.assertIn("json", plan)
            result = adapter.launch(_request("executor", role_grant=("read", "write")))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.session_id, "sess-CLAUDE-1")
        self.assertIn("implemented", result.stdout)

    def test_launch_extracts_result_text_not_the_raw_wrapper(self) -> None:
        """RDS-07: ``LaunchResult.stdout`` must be the wrapper's own ``result`` text — the
        oxidium-forge PCC-02 reproduction evidence, literally, as the regression fixture — not
        the raw ``--output-format json`` wrapper it travelled inside."""
        with tempfile.TemporaryDirectory() as directory:
            executable = _fake_executable(Path(directory), _WRAPPER_CLAUDE, "wrapper_claude.py")
            adapter = ClaudeAdapter(
                executable=executable,
                isolation_capabilities=proven_isolation_capabilities(
                    "claude", runtime="\0".join(executable)
                ),
            )
            result = adapter.launch(_request("task_verifier", read_only=True))

        self.assertEqual(
            result.stdout,
            '{"role": "executor", "status": "blocked", "task_id": "PCC-01", "attempt": 1}',
        )
        self.assertEqual(result.session_id, "sess-pcc01")
        # The raw wrapper is preserved separately, for a human debugging a failed launch.
        self.assertIn("total_cost_usd", result.raw_stdout)
        self.assertNotIn("total_cost_usd", result.stdout)

    def test_timeout_terminates_the_process_and_reports_the_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = _fake_executable(Path(directory), _SLOW_CLAUDE, "slow_claude.py")
            adapter = ClaudeAdapter(
                executable=executable, timeout=1.0,
                isolation_capabilities=proven_isolation_capabilities(
                    "claude", runtime="\0".join(executable)
                ),
            )
            started = time.monotonic()
            result = adapter.launch(_request("executor"))
            elapsed = time.monotonic() - started
        self.assertEqual(result.exit_code, EXIT_TIMEOUT)
        self.assertLess(elapsed, 20.0)

    def test_launch_without_an_executable_fails_closed(self) -> None:
        adapter = ClaudeAdapter(
            resolver=lambda: None,
            isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
        )
        self.assertFalse(adapter.available())
        with self.assertRaises(AdapterError) as ctx:
            adapter.launch(_request("executor"))
        self.assertEqual(ctx.exception.code, "adapter-unavailable")


class CodexLaunchTests(unittest.TestCase):
    def test_codex_final_result_accepts_intermediate_messages_before_one_final_event(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps({
                "role": "executor", "task_id": "VRC-04", "attempt": 1,
                "status": "implemented",
            }),
        }})
        intermediate = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "I am still working.",
        }})
        completed = json.dumps({"type": "turn.completed"})
        result = parse_codex_final_result(
            "\n".join((intermediate, event, completed)), task_id="VRC-04", attempt=1)
        self.assertEqual(result.status, "implemented")
        with self.assertRaises(AdapterError) as ctx:
            parse_codex_final_result(
                "\n".join((event, event, completed)), task_id="VRC-04", attempt=1)
        self.assertEqual(ctx.exception.code, "result-protocol-invalid")

    def test_codex_final_result_rejects_a_message_after_the_final_event(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps({
                "role": "executor", "task_id": "VRC-04", "attempt": 1,
                "status": "implemented",
            }),
        }})
        later = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "One last note.",
        }})
        with self.assertRaises(AdapterError) as ctx:
            parse_codex_final_result(
                "\n".join((event, later, json.dumps({"type": "turn.completed"}))),
                task_id="VRC-04", attempt=1)
        self.assertEqual(ctx.exception.code, "result-protocol-invalid")

    def test_codex_final_result_rejects_a_malformed_jsonl_event(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps({
                "role": "executor", "task_id": "VRC-04", "attempt": 1,
                "status": "implemented",
            }),
        }})

        with self.assertRaises(AdapterError) as ctx:
            parse_codex_final_result(
                event + "\n{malformed event", task_id="VRC-04", attempt=1)

        self.assertEqual(ctx.exception.code, "result-protocol-invalid")
    def test_launch_parses_jsonl_agent_message_and_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = _fake_executable(Path(directory), _FAKE_CODEX, "fake_codex.py")
            # JSONL parsing is unrelated to R03 isolation policy; supply an explicit
            # live-probe-equivalent capability grant so this launch reaches the parser.
            result = CodexAdapter(
                executable=executable,
                isolation_capabilities=proven_isolation_capabilities(
                    "codex", runtime="\0".join(executable)
                ),
            ).launch(_request("executor", role_grant=("read", "write")))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "implemented\n\nreport body")
        self.assertEqual(result.session_id, "thread-CODEX-1")

    def test_parse_session_id_never_invents_a_value(self) -> None:
        self.assertIsNone(parse_session_id(""))
        self.assertIsNone(parse_session_id('{"type": "result", "result": "x"}'))
        self.assertEqual(parse_session_id('{"session_id": "abc"}'), "abc")

    def test_parse_result_text_never_invents_a_value(self) -> None:
        self.assertIsNone(parse_result_text(""))
        self.assertIsNone(parse_result_text("not json at all"))
        self.assertIsNone(parse_result_text('{"session_id": "abc"}'))
        self.assertEqual(parse_result_text('{"result": "hello"}'), "hello")
        self.assertEqual(parse_result_text(_OXIDIUM_FORGE_WRAPPER), (
            '{"role": "executor", "status": "blocked", "task_id": "PCC-01", "attempt": 1}'
        ))

    def test_parse_result_text_takes_the_last_object_on_a_resumed_continuation(self) -> None:
        first = json.dumps({"result": "first turn"})
        second = json.dumps({"result": "second turn"})
        self.assertEqual(parse_result_text(f"{first}\n{second}"), "second turn")

    @unittest.skipUnless(
        os.environ.get("PIPELINE_CORE_CLAUDE_SMOKE"), "opt-in installed-CLI characterization"
    )
    def test_installed_cli_read_only_smoke(self) -> None:  # pragma: no cover - opt-in
        import shutil

        if shutil.which("claude") is None:
            self.skipTest("claude CLI not on PATH")
        with tempfile.TemporaryDirectory() as directory:
            adapter = ClaudeAdapter(working_root=directory)
            result = adapter.launch(_request(
                "task-verifier", read_only=True, role_grant=("read",), no_tools=True,
                prompt="Reply with the single word: ready", timeout=120.0,
            ))
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(result.session_id)

    @unittest.skipUnless(
        os.environ.get("PIPELINE_CORE_CLAUDE_SMOKE"), "opt-in installed-CLI characterization"
    )
    def test_installed_cli_accepts_every_shipped_executor_role(self) -> None:  # pragma: no cover
        """RDS-06: each concrete ``Executor`` a task can declare must be a real ``--agent`` the
        installed CLI recognizes, so ``dispatch_executor`` handing it ``spec.executor`` verbatim
        never regresses back into the fixed-literal ``"executor"`` bug this task fixed."""
        import shutil

        if shutil.which("claude") is None:
            self.skipTest("claude CLI not on PATH")
        for role in ("python-executor", "rust-executor", "frontend-executor",
                     "docs-maintainer", "task-verifier", "test-verifier"):
            with self.subTest(role=role):
                with tempfile.TemporaryDirectory() as directory:
                    adapter = ClaudeAdapter(working_root=directory)
                    result = adapter.launch(_request(
                        role, read_only=True, role_grant=("read",), no_tools=True,
                        prompt="Reply with the single word: ready", timeout=120.0,
                    ))
                self.assertEqual(result.exit_code, 0, msg=result.stderr)
                self.assertNotIn("not found", (result.stderr or "").lower())


# --- encoding (RDS-11) ------------------------------------------------------------------------


#: Reads the prompt as raw bytes and dumps them back as ASCII hex — a write-side check that
#: never itself depends on any text encoding, so only the parent's ``Popen`` encoding of the
#: prompt string can be under test.
_HEX_ECHO_CLAUDE = """\
import sys
sys.stdout.write(sys.stdin.buffer.read().hex())
"""

#: Writes a real, hardcoded UTF-8-encoded non-ASCII reply — exactly how the installed CLI (a
#: Node.js process) behaves on a redirected stdout pipe regardless of the host's locale. A
#: read-side check: only the parent's decoding of these bytes is under test.
_UTF8_REPLY_CLAUDE = """\
import sys
sys.stdin.buffer.read()
sys.stdout.buffer.write("caf\\u00e9 \\u2014 na\\u00efve".encode("utf-8"))
"""


class SubprocessEncodingTests(unittest.TestCase):
    """RDS-11: ``run_subprocess`` must not depend on ``locale.getpreferredencoding()`` — Windows
    non-English locales (verified: ``cp1251``) are not UTF-8, and silently mis-encode/decode
    every non-ASCII character crossing the pipe in either direction. A patched "wrong" locale
    is used so this is deterministic across hosts, rather than depending on the CI machine's own
    locale already happening to be UTF-8 (which would mask the bug)."""

    def test_prompt_is_written_as_utf8_regardless_of_host_locale(self) -> None:
        from pipeline_core.adapters import run_subprocess

        prompt = "café — naïve"
        with tempfile.TemporaryDirectory() as directory:
            argv = _fake_executable(Path(directory), _HEX_ECHO_CLAUDE, "hex_echo.py")
            with unittest.mock.patch("locale.getpreferredencoding", return_value="cp1251"):
                completed = run_subprocess(argv, prompt=prompt, timeout=5)
        self.assertEqual(completed.stdout, prompt.encode("utf-8").hex())

    def test_reply_is_decoded_as_utf8_regardless_of_host_locale(self) -> None:
        from pipeline_core.adapters import run_subprocess

        with tempfile.TemporaryDirectory() as directory:
            argv = _fake_executable(Path(directory), _UTF8_REPLY_CLAUDE, "utf8_reply.py")
            with unittest.mock.patch("locale.getpreferredencoding", return_value="cp1251"):
                completed = run_subprocess(argv, prompt="hi", timeout=5)
        self.assertEqual(completed.stdout, "café — naïve")


# --- resolution -----------------------------------------------------------------------------


class AdapterResolutionTests(unittest.TestCase):
    def test_explicit_available_adapter_resolves_to_itself(self) -> None:
        resolution = resolve_adapter("claude", {"claude": True})
        self.assertEqual((resolution.requested, resolution.resolved, resolution.sourced),
                         ("claude", "claude", "explicit"))

    def test_auto_picks_the_first_available_known_adapter(self) -> None:
        resolution = resolve_adapter("auto", {"claude": True, "codex": True})
        self.assertEqual((resolution.resolved, resolution.sourced), ("claude", "default"))

    def test_auto_with_nothing_available_fails_closed(self) -> None:
        with self.assertRaises(AdapterResolutionError) as ctx:
            resolve_adapter("auto", {"claude": False})
        self.assertEqual(ctx.exception.code, "adapter-unavailable")

    def test_explicit_codex_resolves_without_falling_back_to_claude(self) -> None:
        resolution = resolve_adapter("codex", {"claude": True, "codex": True})
        self.assertEqual((resolution.requested, resolution.resolved, resolution.sourced),
                         ("codex", "codex", "explicit"))

    def test_unknown_adapter_is_rejected(self) -> None:
        with self.assertRaises(AdapterResolutionError) as ctx:
            resolve_adapter("nonsense", {})
        self.assertEqual(ctx.exception.code, "adapter-unavailable")

    def test_resolution_is_driven_by_the_probed_claude_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = _fake_executable(Path(directory), _FAKE_CLAUDE, "fake_claude.py")
            available = ClaudeAdapter(executable=executable).available()
            resolution = resolve_adapter("auto", {"claude": available})
        self.assertEqual(resolution.resolved, "claude")


class AdapterPinningTests(unittest.TestCase):
    def test_pin_records_the_adapter_globally_and_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            pin_adapter(run, resolve_adapter("claude", {"claude": True}))
            self.assertEqual(run.controls["adapter_resolved"],
                             {"value": "claude", "sourced": "explicit"})
            self.assertEqual(run.controls["adapter_requested"]["value"], "claude")
            self.assertEqual(pinned_adapter(run), "claude")
            self.assertEqual([t.adapter for t in run.tasks.values()], ["claude", "claude"])

    def test_auto_pin_is_sourced_as_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            pin_adapter(run, resolve_adapter("auto", {"claude": True}))
            self.assertEqual(run.controls["adapter_resolved"]["sourced"], "default")

    def test_resume_with_the_same_adapter_passes_and_a_switch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            pin_adapter(run, resolve_adapter("claude", {"claude": True}))
            self.assertEqual(
                ensure_pinned_adapter(run, AdapterResolution("claude", "claude", "explicit")),
                "claude",
            )
            with self.assertRaises(AdapterResolutionError) as ctx:
                ensure_pinned_adapter(run, AdapterResolution("codex", "codex", "explicit"))
            self.assertEqual(ctx.exception.code, "adapter-switch")

    def test_a_fresh_run_has_no_pin_and_accepts_any_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            self.assertIsNone(pinned_adapter(run))
            self.assertEqual(
                ensure_pinned_adapter(run, AdapterResolution("auto", "claude", "default")),
                "claude",
            )


class StrictWorkerIsolationTests(unittest.TestCase):
    """TC-11: a strict-role launch fails closed before any process starts unless the adapter
    declares every isolation token, and a request cannot substitute its bound role/bundle."""

    def test_claude_default_capabilities_are_unproved_without_live_evidence(self) -> None:
        for token in ("bundle_validated", "discovery_isolated", "skill_reads_enforced"):
            self.assertFalse(
                CLAUDE_ISOLATION_CAPABILITIES.has(token),
                f"Claude has no runtime/version/probe evidence for {token!r}",
            )

    def test_claude_default_capabilities_leave_nested_and_subprocess_isolation_unproven(
        self,
    ) -> None:
        """AC-4: TC-02-capabilities.md marks nested-delegate/subprocess isolation UNKNOWN and
        live-unrun for both runtimes; UNKNOWN never counts as PASS, so neither token may be
        declared held from argv construction alone."""
        for token in ("subprocess_isolated", "nested_delegation_isolated"):
            self.assertFalse(
                CLAUDE_ISOLATION_CAPABILITIES.has(token),
                f"Claude has no live proof yet for {token!r}",
            )

    def test_codex_default_capabilities_declare_no_strict_isolation_token(self) -> None:
        for token in STRICT_ISOLATION_CAPABILITIES:
            self.assertFalse(
                CODEX_ISOLATION_CAPABILITIES.has(token),
                f"codex exec has no flag that backs {token!r}",
            )

    def test_require_strict_isolation_passes_for_a_fully_capable_adapter(self) -> None:
        require_strict_isolation(
            _PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            role="python-executor",
            executable="claude",
            cli_surface="claude -p",
        )

    def test_a_declared_token_needs_complete_evidence_bound_to_its_adapter(self) -> None:
        token = "subprocess_isolated"
        invalid = replace(
            _PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            isolation_proofs=(IsolationCapabilityProof(
                capability=token,
                adapter="codex",
                runtime="claude",
                cli_surface="claude -p",
                observed_version="test-version",
                evidence="probe record",
            ),),
        )
        self.assertFalse(invalid.has(token))
        with self.assertRaises(AdapterError) as raised:
            require_strict_isolation(
                invalid,
                role="python-executor",
                executable="claude",
                cli_surface="claude -p",
            )
        self.assertEqual(raised.exception.code, "stack-isolation-unsupported")

    def test_claude_rejects_a_proof_for_a_different_selected_executable(self) -> None:
        calls: list[object] = []

        def unreachable_runner(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise AssertionError("a mismatched runtime proof must not start a process")

        with self.assertRaises(AdapterError) as raised:
            ClaudeAdapter(
                executable="different-claude",
                runner=unreachable_runner,
                isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            ).launch(_request("executor", role_grant=("read", "write")))

        self.assertEqual(raised.exception.code, "stack-isolation-unsupported")
        self.assertEqual(calls, [])

    def test_claude_rejects_a_proof_for_a_different_observed_runtime_version(self) -> None:
        capabilities = replace(
            _PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            observed_version="different-version",
        )

        with self.assertRaises(AdapterError) as raised:
            require_strict_isolation(
                capabilities,
                role="executor",
                executable="claude",
                cli_surface="claude -p",
            )

        self.assertEqual(raised.exception.code, "stack-isolation-unsupported")

    def test_require_strict_isolation_rejects_claude_default_for_unproven_tokens(self) -> None:
        """The Claude default is not fully capable until a live probe proves the remaining
        two tokens — declaring them true from argv construction alone is exactly what AC-4
        forbids."""
        with self.assertRaises(AdapterError) as ctx:
            require_strict_isolation(
                CLAUDE_ISOLATION_CAPABILITIES,
                role="python-executor",
                executable="claude",
                cli_surface="claude -p",
            )
        self.assertEqual(ctx.exception.code, "stack-isolation-unsupported")

    def test_require_strict_isolation_rejects_a_missing_token(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            require_strict_isolation(
                CODEX_ISOLATION_CAPABILITIES,
                role="executor",
                executable="codex",
                cli_surface="codex exec",
            )
        self.assertEqual(ctx.exception.code, "stack-isolation-unsupported")

    def test_codex_launch_rejects_every_strict_role_by_default(self) -> None:
        for role in ("python-executor", "task_verifier", "test_verifier"):
            with self.subTest(role=role):
                with self.assertRaises(AdapterError) as ctx:
                    CodexAdapter(executable="codex").launch(
                        _request(role, role_grant=("read", "write"))
                    )
                self.assertEqual(ctx.exception.code, "stack-isolation-unsupported")

    def test_codex_launch_never_starts_a_process_when_isolation_is_unsupported(self) -> None:
        calls: list[object] = []

        def unreachable_runner(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise AssertionError("no process may start for an unsupported strict launch")

        with self.assertRaises(AdapterError) as ctx:
            CodexAdapter(executable="codex", runner=unreachable_runner).launch(
                _request("executor", role_grant=("read", "write"))
            )
        self.assertEqual(ctx.exception.code, "stack-isolation-unsupported")
        self.assertEqual(calls, [])

    def test_runner_owned_probe_can_reach_codex_without_relaxing_ordinary_strict_launches(self) -> None:
        calls: list[object] = []

        def runner(*args: object, **kwargs: object) -> CompletedProcess:
            calls.append((args, kwargs))
            return CompletedProcess(0, "", "")

        adapter = CodexAdapter(
            executable="codex", runner=runner, working_root="project-root",
        )
        with self.assertRaises(AdapterError) as typed:
            adapter.launch_live_probe(_request("executor"))  # type: ignore[arg-type]
        self.assertEqual(typed.exception.code, "live-probe-invalid")
        with self.assertRaises(AdapterError) as raised:
            adapter.launch(_request("executor", role_grant=("read", "write")))
        self.assertEqual(raised.exception.code, "stack-isolation-unsupported")
        self.assertEqual(calls, [])

        result = adapter.launch_live_probe(LiveProbeRequest(
            task_id="TC-11", prompt="runner-owned probe", report_path=Path("probe.json"),
            allowed_scope=("pipeline_core",), timeout=90.0,
        ))

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(calls), 1)
        argv = calls[0][0][0]
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--cd", argv)
        self.assertEqual(calls[0][1]["cwd"], Path(argv[argv.index("--cd") + 1]))
        self.assertNotEqual(calls[0][1]["cwd"], Path("project-root"))
        self.assertTrue(str(calls[0][1]["cwd"]).startswith(tempfile.gettempdir()))
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(calls[0][1]["prompt"], "runner-owned probe")
        self.assertEqual(calls[0][1]["timeout"], 90.0)

    def test_codex_ordinary_argv_does_not_bypass_the_git_repository_check(self) -> None:
        argv = build_codex_argv(
            _request("executor", role_grant=("read", "write")),
            executable="codex", working_root="C:/repo",
        )

        self.assertNotIn("--skip-git-repo-check", argv)


class DockerCodexIsolationTests(unittest.TestCase):
    """The opt-in Docker path has a materially narrower host surface than Codex on Windows."""

    def test_container_context_has_the_minimal_runtime_import_closure(self) -> None:
        """A scoped ``pipeline_core.adapters`` import resolves without mounting the project."""
        from pipeline_core.adapters import _materialize_container_context

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "pipeline_core").mkdir(parents=True)
            source_root = Path(__file__).resolve().parents[1]
            (workspace / "pipeline_core" / "adapters.py").write_bytes(
                (source_root / "pipeline_core" / "adapters.py").read_bytes()
            )
            context = root / "context"
            _materialize_container_context(
                ExecutorContextBundle("TC-11", (
                    ContextEntry.of("task", "docs/plans/tasks/TC-11.md", "TASK BODY"),
                )),
                context,
                runtime_root=source_root,
            )

            env = {"PYTHONPATH": os.pathsep.join((
                str(workspace / "src"),
                str(context / "project" / "feature-pipeline-skill" / "src"),
            ))}
            result = subprocess.run(
                [sys.executable, "-c", "import pipeline_core.adapters; print('imported')"],
                cwd=workspace, env=env, capture_output=True, text=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "imported")
            self.assertTrue((context / "project" / "feature-pipeline-skill" / "src"
                             / "feature_pipeline" / "infrastructure" / "adapters"
                             / "codex_launcher.py").is_file())
            self.assertFalse((context / "project" / "feature-pipeline-skill" / "src"
                              / "feature_pipeline" / "bootstrap.py").exists())

    def test_container_materializes_only_bound_context_on_a_read_only_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            (worktree / "pipeline_core").mkdir(parents=True)
            (worktree / "pipeline_core" / "target.py").write_text("before\n", encoding="utf-8")
            (worktree / "docs").mkdir()
            (worktree / "docs" / "secret.md").write_text("not mounted\n", encoding="utf-8")
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            image = "example.invalid/codex@sha256:" + "a" * 64
            bundle = ExecutorContextBundle("TC-11", (
                ContextEntry.of("task", "docs/plans/tasks/TC-11.md", "TASK BODY"),
                ContextEntry.of("input", ".prompts/proposal.md", "PROPOSAL BODY"),
                ContextEntry.of("input", ".prompts/research.md", "RESEARCH BODY"),
                ContextEntry.of("input", "docs/validation/routing/TC-09-review.md", "REVIEW BODY"),
                ContextEntry.of("skill", ".agents/skills/python/SKILL.md", "SKILL BODY"),
            ))
            observed: dict[str, object] = {}

            def docker_runner(argv: list[str]) -> CompletedProcess:
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(0, json.dumps([argv[-1]]), "")
                return CompletedProcess(0, "", "")

            def runner(argv: list[str], *, prompt: str, **_: object) -> CompletedProcess:
                if argv[-1].endswith("@openai/codex@0.1.0 codex --version"):
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                observed["argv"] = argv
                mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
                observed["workspace"] = next(mount for mount in mounts if "dst=/workspace" in mount)
                context = next(mount for mount in mounts if "dst=/context" in mount)
                observed["context"] = context
                observed["prompt"] = prompt
                context_root = Path(context.split(",dst=", 1)[0].removeprefix("type=bind,src="))
                self.assertEqual((context_root / "project/docs/plans/tasks/TC-11.md").read_text(encoding="utf-8"), "TASK BODY")
                self.assertEqual((context_root / "project/.prompts/proposal.md").read_text(encoding="utf-8"), "PROPOSAL BODY")
                self.assertEqual((context_root / "project/.prompts/research.md").read_text(encoding="utf-8"), "RESEARCH BODY")
                self.assertEqual((context_root / "project/docs/validation/routing/TC-09-review.md").read_text(encoding="utf-8"), "REVIEW BODY")
                self.assertEqual((context_root / "agents/skills/python/SKILL.md").read_text(encoding="utf-8"), "SKILL BODY")
                self.assertFalse((context_root / "project/docs/secret.md").exists())
                return CompletedProcess(1, "", "fake Codex failure")

            adapter = DockerCodexAdapter(
                image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth, docker_runner=docker_runner,
                runner=runner, image_validator=lambda *_: True,
                isolation_capabilities=proven_isolation_capabilities("codex", runtime=image, observed_version="codex 0.1.0"),
                executor_contexts={"TC-11": bundle},
                runtime_root=Path(__file__).resolve().parents[1],
            )
            adapter.launch(LaunchRequest(
                role="python-executor", task_id="TC-11", prompt="read docs/plans/tasks/TC-11.md",
                report_path=root / "report", working_root=str(worktree), role_grant=("read", "write"),
                allowed_scope=("pipeline_core/**",), recipient_role="executor", bundle_digest="a" * 64,
                composition=LaunchComposition("executor", "a" * 64, ("pipeline_core/**",), ("read", "write")),
            ))

        self.assertNotIn(",readonly", str(observed["workspace"]))
        self.assertIn(",readonly", str(observed["context"]))
        self.assertIn("/context/project/docs/plans/tasks/TC-11.md", str(observed["prompt"]))
        self.assertIn("/context/agents/skills/python/SKILL.md", str(observed["prompt"]))
        self.assertIn("minimal read-only context and runtime import closure", str(observed["prompt"]))
        self.assertIn("standalone temporary Python assertion or script", str(observed["prompt"]))
        self.assertIn("available target/runtime imports", str(observed["prompt"]))
        self.assertIn("permanent allowed test", str(observed["prompt"]))
        self.assertIn("runner remains the owner of full repository verification", str(observed["prompt"]))
        self.assertIn("/context is read-only; edit only /workspace", str(observed["prompt"]))
        self.assertIn("do not edit outside the allowed scope", str(observed["prompt"]))
        self.assertIn("PYTHONPATH=/workspace/src:/context/project/feature-pipeline-skill/src",
                      observed["argv"])

    def test_workspace_write_probe_is_unprivileged_and_exposes_no_runtime_inputs(self) -> None:
        """The preflight proves only the disposable bind mount is writable."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            adapter = DockerCodexAdapter(
                image="example.invalid/codex@sha256:" + "a" * 64,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth,
            )

            argv = adapter._workspace_write_probe_argv(
                workspace, Path("probe/allowed-write.txt"), "preflight-token",
            )

            self.assertEqual(argv[:4], ["docker", "run", "--rm", "--network"])
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertEqual(argv[argv.index("--user") + 1], "1000:1000")
            self.assertIn("--read-only", argv)
            self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
            self.assertIn("no-new-privileges", argv)
            mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
            self.assertEqual(mounts, [f"type=bind,src={workspace},dst=/workspace"])
            self.assertNotIn(str(auth.resolve()), argv)
            self.assertNotIn("HTTP_PROXY=http://codex-egress-proxy:8080", argv)
            self.assertNotIn("codex exec", argv[-1])
            self.assertIn("/workspace/probe/allowed-write.txt", argv[-1])
            self.assertIn("preflight-token", argv[-1])

    def test_version_observation_uses_the_same_read_only_auth_mount_as_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            adapter = DockerCodexAdapter(
                image="example.invalid/codex@sha256:" + "a" * 64,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth,
            )

            argv = adapter._version_argv("runner-owned-network")

            mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
            self.assertEqual(mounts, [
                f"type=bind,src={auth.resolve()},dst=/run/codex-auth/auth.json,readonly",
            ])
            self.assertIn("CODEX_HOME=/codex-home", argv)
            self.assertIn("/npm-cache:rw,exec,nosuid,nodev,size=768m", argv)
            self.assertIn("NPM_CONFIG_CACHE=/npm-cache", argv)
            self.assertIn("TMPDIR=/npm-cache", argv)
            self.assertEqual(argv[-3:], ["sh", "-ceu", argv[-1]])
            self.assertIn('cp /run/codex-auth/auth.json "$CODEX_HOME/auth.json"', argv[-1])
            self.assertIn("exec npx --yes --package @openai/codex@0.1.0 codex --version", argv[-1])

    def test_container_gives_codex_a_bounded_worker_thread_budget(self) -> None:
        """Codex code-mode workers need more than the 64-task bootstrap ceiling."""
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            adapter = DockerCodexAdapter(
                image="example.invalid/codex@sha256:" + "a" * 64,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth,
            )

            request = LaunchRequest(
                role="runner-live-isolation-probe", task_id="TC-11", prompt="probe",
                report_path=Path("probe.json"), allowed_scope=("probe/**",),
            )

            for argv in (
                adapter._version_argv("runner-owned-network"),
                adapter._docker_argv(
                    Path("/runner-owned-workspace"), request, "runner-owned-network",
                ),
            ):
                self.assertEqual(argv[argv.index("--pids-limit") + 1], "256")
                self.assertIn("--read-only", argv)
                self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
                self.assertIn("no-new-privileges", argv)

    def test_version_validation_accepts_the_published_codex_cli_banner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            adapter = DockerCodexAdapter(
                image="example.invalid/codex@sha256:" + "a" * 64,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.155.1", auth_file=auth,
                runner=lambda *_args, **_kwargs: CompletedProcess(0, "codex-cli 0.155.1\n", ""),
            )

            self.assertTrue(adapter._validate_codex_version("runner-owned-network"))

    def test_connect_proxy_allows_only_documented_hostname_suffixes_and_public_dns(self) -> None:
        from pipeline_core.adapters import _CONNECT_PROXY_SOURCE
        namespace: dict[str, object] = {}
        exec(_CONNECT_PROXY_SOURCE.split("class Proxy")[0], namespace)
        allowed = namespace["allowed"]
        public_address = namespace["public_address"]
        self.assertTrue(allowed("api.openai.com"))  # type: ignore[operator]
        self.assertTrue(allowed("packages.chatgpt.com"))  # type: ignore[operator]
        self.assertFalse(allowed("api.openai.com.evil.example"))  # type: ignore[operator]
        self.assertFalse(allowed("127.0.0.1"))  # type: ignore[operator]
        with unittest.mock.patch.object(namespace["socket"], "getaddrinfo", return_value=[
            (None, None, None, None, ("127.0.0.1", 443)),
        ]):
            self.assertIsNone(public_address("api.openai.com"))  # type: ignore[operator]

    def test_container_launch_uses_only_scoped_workspace_auth_and_strict_codex_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            allowed = worktree / "pipeline_core"
            allowed.mkdir(parents=True)
            (allowed / "target.py").write_text("before\n", encoding="utf-8")
            (worktree / "sibling.txt").write_text("private\n", encoding="utf-8")
            auth = root / "auth.json"
            auth.write_text("not inspected by this test\n", encoding="utf-8")
            calls: list[tuple[list[str], dict[str, object]]] = []

            def runner(argv: list[str], **kwargs: object) -> CompletedProcess:
                calls.append((argv, kwargs))
                if argv[-1].endswith("@openai/codex@0.1.0 codex --version"):
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                return CompletedProcess(1, "", "fake Codex failure")

            docker_calls: list[list[str]] = []

            def docker_runner(argv: list[str]) -> CompletedProcess:
                docker_calls.append(argv)
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(0, json.dumps([argv[-1]]), "")
                if argv[-2:] == ["codex", "--version"]:
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                return CompletedProcess(0, "proxy-id\n", "")

            image = "example.invalid/codex@sha256:" + "a" * 64
            adapter = DockerCodexAdapter(
                image=image,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0",
                auth_file=auth,
                docker_executable="docker",
                runner=runner,
                docker_runner=docker_runner,
                isolation_capabilities=proven_isolation_capabilities("codex", runtime=image, observed_version="codex 0.1.0"),
                image_validator=lambda *_: True,
            )
            result = adapter.launch(LaunchRequest(
                role="python-executor", task_id="TC-11", prompt="work", report_path=root / "report",
                working_root=str(worktree), role_grant=("read", "write"),
                allowed_scope=("pipeline_core/**",), recipient_role="executor",
                bundle_digest="a" * 64,
                composition=LaunchComposition("executor", "a" * 64, ("pipeline_core/**",), ("read", "write")),
            ))

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(len(calls), 2)
            version_argv, _ = calls[0]
            self.assertIn("npx", version_argv[-1])
            self.assertIn("exec npx --yes --package @openai/codex@0.1.0 codex --version", version_argv[-1])
            argv, kwargs = calls[1]
            self.assertEqual(argv[:4], ["docker", "run", "--rm", "-i"])
            self.assertIn("-i", argv, "Docker must attach stdin so Codex receives the probe prompt")
            self.assertIn("--network", argv)
            internal_network = argv[argv.index("--network") + 1]
            self.assertTrue(internal_network.startswith("feature-pipeline-codex-"))
            self.assertIn("HTTP_PROXY=http://codex-egress-proxy:8080", argv)
            self.assertIn("HTTPS_PROXY=http://codex-egress-proxy:8080", argv)
            self.assertIn("NO_PROXY=", argv)
            self.assertIn("--read-only", argv)
            self.assertIn("--cap-drop", argv)
            self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
            self.assertIn("no-new-privileges", argv)
            self.assertNotIn("--privileged", argv)
            mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
            self.assertEqual(len(mounts), 2)
            self.assertTrue(any("dst=/workspace" in mount and ",ro" not in mount for mount in mounts))
            self.assertTrue(any("dst=/run/codex-auth/auth.json" in mount and ",readonly" in mount for mount in mounts))
            self.assertFalse(any(str(worktree) in mount for mount in mounts))
            self.assertFalse(any(str(worktree) in mount and "sibling.txt" in mount for mount in mounts))
            command = argv[-1]
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertIn("--sandbox danger-full-access", command)
            self.assertIn("/codex-home:rw,noexec,nosuid,nodev,size=8m", argv)
            self.assertIn("npx", command)
            self.assertIn("exec npx --yes --package @openai/codex@0.1.0 codex exec --json", command)
            self.assertNotIn(" exec codex ", command)
            self.assertEqual(kwargs["cwd"], None)
            self.assertEqual(kwargs["prompt"], "work")
            self.assertEqual(docker_calls[0][:4], ["docker", "image", "inspect", "--format"])
            self.assertIn(["docker", "network", "create", "--internal", internal_network], docker_calls)
            preflight = next(call for call in docker_calls if call[:6] == [
                "docker", "run", "--rm", "--network", "none", "--read-only",
            ])
            self.assertIn("/workspace/pipeline_core/target.py", preflight[-1])
            proxy_start = next(call for call in docker_calls if call[:3] == ["docker", "run", "-d"])
            self.assertIn("--network", proxy_start)
            self.assertEqual(proxy_start[proxy_start.index("--network") + 1], internal_network)
            self.assertTrue(any(call[:4] == ["docker", "network", "connect", "bridge"]
                                and call[4].startswith("codex-egress-proxy-") for call in docker_calls))
            self.assertIn(["docker", "network", "rm", internal_network], docker_calls)

    def test_container_rejects_unpinned_proxy_or_package_version_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            with self.assertRaises(AdapterError) as raised:
                DockerCodexAdapter(
                    image="example.invalid/codex@sha256:" + "a" * 64,
                    proxy_image="python:latest", codex_version="0.1.0", auth_file=auth,
                )
            self.assertEqual(raised.exception.code, "container-proxy-image-unpinned")

    def test_container_live_probe_uses_the_executor_network_and_scoped_workspace_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            image = "example.invalid/codex@sha256:" + "a" * 64
            calls: list[list[str]] = []
            def docker_runner(argv: list[str]) -> CompletedProcess:
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(0, json.dumps([argv[-1]]), "")
                if argv[-2:] == ["codex", "--version"]:
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                return CompletedProcess(0, "", "")
            adapter = DockerCodexAdapter(
                image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth, docker_runner=docker_runner,
                runner=lambda argv, **_: calls.append(argv) or CompletedProcess(
                    0, "codex 0.1.0\n" if argv[-1].endswith("@openai/codex@0.1.0 codex --version") else "{}", ""
                ),
                image_validator=lambda *_: True,
            )
            result = adapter.launch_live_probe(LiveProbeRequest(
                task_id="TC-11", prompt="perform the containment probe", report_path=root / "probe.json",
                allowed_scope=("ignored/**",), timeout=60.0,
            ))
            self.assertEqual(result.exit_code, 1)
            argv = calls[1]
            self.assertNotEqual(argv[argv.index("--network") + 1], "none")
            self.assertIn("HTTP_PROXY=http://codex-egress-proxy:8080", argv)
            mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
            self.assertTrue(any("dst=/workspace" in mount and ",readonly" not in mount for mount in mounts))
            self.assertFalse(any("sibling.txt" in mount for mount in mounts))

    def test_container_live_probe_accepts_an_observed_tool_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            image = "example.invalid/codex@sha256:" + "a" * 64

            def docker_runner(argv: list[str]) -> CompletedProcess:
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(0, json.dumps([argv[-1]]), "")
                return CompletedProcess(0, "", "")

            def runner(argv: list[str], *, prompt: str, **_: object) -> CompletedProcess:
                if argv[-1].endswith("@openai/codex@0.1.0 codex --version"):
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                self.assertIn("shell or file tool", prompt)
                token = prompt.split("the exact token ", 1)[1].split(". Do this", 1)[0]
                self.assertIn("--output-schema /workspace/probe/final-response.schema.json", argv[-1])
                workspace_mount = next(argv[index + 1] for index, value in enumerate(argv)
                                       if value == "--mount" and "dst=/workspace" in argv[index + 1])
                workspace = Path(workspace_mount.split(",dst=", 1)[0].removeprefix("type=bind,src="))
                schema = json.loads((workspace / "probe" / "final-response.schema.json").read_text(
                    encoding="utf-8"
                ))
                self.assertFalse(schema["additionalProperties"])
                (workspace / "probe" / "allowed-write.txt").write_text(token, encoding="utf-8")
                for name in ("parent-sibling.out", "parent-outside.out", "child-sibling.out", "child-outside.out"):
                    (workspace / "probe" / name).write_text("denied", encoding="utf-8")
                result = "{}"
                return CompletedProcess(0, "\n".join((
                    json.dumps({"type": "item.completed", "item": {
                        "type": "agent_message", "text": result,
                    }}),
                    json.dumps({"type": "turn.completed"}),
                )), "")

            adapter = DockerCodexAdapter(
                image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth, docker_runner=docker_runner,
                runner=runner, image_validator=lambda *_: True,
            )

            result = adapter.launch_live_probe(LiveProbeRequest(
                task_id="TC-11", prompt="runner-owned probe", report_path=root / "probe.json",
                allowed_scope=("ignored/**",), timeout=60.0,
            ))

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                result.probe_binding,
                DockerCodexAdapter.probe_binding(
                    image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                    codex_version="0.1.0", auth_file=auth,
                ),
            )

    def test_container_live_probe_requires_runner_observed_parent_and_child_containment(self) -> None:
        """A JSON denial is not proof: only runner-observed canary outcomes can pass."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            image = "example.invalid/codex@sha256:" + "a" * 64

            def docker_runner(argv: list[str]) -> CompletedProcess:
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(0, json.dumps([argv[-1]]), "")
                return CompletedProcess(0, "", "")

            def runner(argv: list[str], *, prompt: str, **_: object) -> CompletedProcess:
                if argv[-1].endswith("@openai/codex@0.1.0 codex --version"):
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                workspace_mount = next(argv[index + 1] for index, value in enumerate(argv)
                                       if value == "--mount" and "dst=/workspace" in argv[index + 1])
                workspace = Path(workspace_mount.split(",dst=", 1)[0].removeprefix("type=bind,src="))
                token = prompt.split("the exact token ", 1)[1].split(".", 1)[0]
                (workspace / "probe" / "allowed-write.txt").write_text(token, encoding="utf-8")
                # A model saying the reads failed must not substitute for runner-visible proof.
                result = json.dumps({"allowed_write": token, "sibling_access": False})
                return CompletedProcess(0, json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": result,
                }}), "")

            adapter = DockerCodexAdapter(
                image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth, docker_runner=docker_runner,
                runner=runner, image_validator=lambda *_: True,
            )
            result = adapter.launch_live_probe(LiveProbeRequest(
                task_id="TC-11", prompt="runner-owned probe", report_path=root / "probe.json",
                allowed_scope=("ignored/**",), timeout=60.0,
            ))

            self.assertEqual(result.exit_code, 1)
            self.assertIsNotNone(result.probe_observations)
            self.assertFalse(result.probe_observations["parent_read_attempted"])
            self.assertFalse(result.probe_observations["child_read_attempted"])

    def test_container_live_probe_rejects_valid_final_json_without_an_observed_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            image = "example.invalid/codex@sha256:" + "a" * 64

            def docker_runner(argv: list[str]) -> CompletedProcess:
                if argv[1:3] == ["image", "inspect"]:
                    return CompletedProcess(0, json.dumps([argv[-1]]), "")
                return CompletedProcess(0, "", "")

            def runner(argv: list[str], *, prompt: str, **_: object) -> CompletedProcess:
                if argv[-1].endswith("@openai/codex@0.1.0 codex --version"):
                    return CompletedProcess(0, "codex 0.1.0\n", "")
                token = prompt.split("the exact token ", 1)[1].split(". Do this", 1)[0]
                workspace_mount = next(argv[index + 1] for index, value in enumerate(argv)
                                       if value == "--mount" and "dst=/workspace" in argv[index + 1])
                workspace = Path(workspace_mount.split(",dst=", 1)[0].removeprefix("type=bind,src="))
                seeded = (workspace / "probe" / "allowed-write.txt").read_text(encoding="utf-8")
                self.assertTrue(seeded.startswith("runner-owned-seed-"))
                self.assertNotEqual(seeded, token)
                result = json.dumps({"allowed_write": token, "sibling_access": False})
                return CompletedProcess(0, "\n".join((
                    json.dumps({"type": "item.completed", "item": {
                        "type": "agent_message", "text": result,
                    }}),
                    json.dumps({"type": "turn.completed"}),
                )), "")

            adapter = DockerCodexAdapter(
                image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=auth, docker_runner=docker_runner,
                runner=runner, image_validator=lambda *_: True,
            )

            result = adapter.launch_live_probe(LiveProbeRequest(
                task_id="TC-11", prompt="runner-owned probe", report_path=root / "probe.json",
                allowed_scope=("ignored/**",), timeout=60.0,
            ))

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.probe_parse_status, "not-used")
            self.assertIs(result.probe_allowed_write, False)
            self.assertEqual(result.probe_failure_class, "incomplete-runner-observation")

    def test_runner_owned_probe_can_reach_claude_without_relaxing_ordinary_strict_launches(self) -> None:
        calls: list[object] = []

        def runner(*args: object, **kwargs: object) -> CompletedProcess:
            calls.append((args, kwargs))
            return CompletedProcess(0, json.dumps({"result": "", "session_id": "probe"}), "")

        adapter = ClaudeAdapter(executable="claude", runner=runner)
        with self.assertRaises(AdapterError) as raised:
            adapter.launch(_request("task_verifier", role_grant=("read",)))
        self.assertEqual(raised.exception.code, "stack-isolation-unsupported")
        self.assertEqual(calls, [])

        result = adapter.launch_live_probe(LiveProbeRequest(
            task_id="TC-11", prompt="runner-owned probe", report_path=Path("probe.json"),
            allowed_scope=("pipeline_core",), timeout=1.0,
        ))

        self.assertEqual(result.exit_code, 0)
        argv = calls[0][0][0]
        self.assertIn("runner-live-isolation-probe", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_codex_launch_succeeds_once_an_explicit_probe_grant_proves_isolation(self) -> None:
        result = CodexAdapter(
            executable="codex", runner=lambda *a, **k: CompletedProcess(0, "{}", ""),
            isolation_capabilities=_PROVEN_ISOLATION_CAPABILITIES,
        ).launch(_request("executor", role_grant=("read", "write")))
        self.assertEqual(result.exit_code, 0)

    def test_claude_launch_rejects_every_strict_role_by_default(self) -> None:
        """Claude is not exempt: the same unproven-nested/subprocess gap rejects its default
        launch too, mirroring Codex's fail-closed behaviour rather than trusting argv shape."""
        for role in ("python-executor", "task_verifier", "test_verifier"):
            with self.subTest(role=role):
                with self.assertRaises(AdapterError) as ctx:
                    ClaudeAdapter(executable="claude").launch(
                        _request(role, role_grant=("read", "write"))
                    )
                self.assertEqual(ctx.exception.code, "stack-isolation-unsupported")

    def test_claude_launch_never_starts_a_process_when_isolation_is_unsupported(self) -> None:
        calls: list[object] = []

        def unreachable_runner(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise AssertionError("no process may start for an unsupported strict launch")

        with self.assertRaises(AdapterError) as ctx:
            ClaudeAdapter(executable="claude", runner=unreachable_runner).launch(
                _request("executor", role_grant=("read", "write"))
            )
        self.assertEqual(ctx.exception.code, "stack-isolation-unsupported")
        self.assertEqual(calls, [])

    def test_claude_launch_succeeds_once_an_explicit_probe_grant_proves_isolation(self) -> None:
        result = ClaudeAdapter(
            executable="claude",
            runner=lambda *a, **k: CompletedProcess(
                0, json.dumps({"result": "implemented", "session_id": "s-1"}), ""
            ),
            isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
        ).launch(_request("executor", role_grant=("read", "write")))
        self.assertEqual(result.exit_code, 0)

    def test_recipient_role_matching_launched_role_is_accepted(self) -> None:
        request = _request(
            "task_verifier", read_only=True, recipient_role="task_verifier",
            bundle_digest="a" * 64,
        )
        # Should not raise: identity is consistent.
        from pipeline_core.adapters import _assert_bundle_identity
        _assert_bundle_identity(request)

    def test_generic_executor_recipient_role_matches_a_concrete_stack_executor(self) -> None:
        request = _request("python-executor", recipient_role="executor")
        from pipeline_core.adapters import _assert_bundle_identity
        _assert_bundle_identity(request)

    def test_mismatched_recipient_role_is_denied_before_launch(self) -> None:
        request = _request(
            "python-executor", role_grant=("read", "write"), recipient_role="task_verifier",
        )
        with self.assertRaises(AdapterError) as ctx:
            ClaudeAdapter(
                executable="claude",
                isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            ).launch(request)
        self.assertEqual(ctx.exception.code, "role-bundle-substitution")

    def test_malformed_bundle_digest_is_denied_before_launch(self) -> None:
        request = _request(
            "python-executor", role_grant=("read", "write"), bundle_digest="not-a-digest",
        )
        with self.assertRaises(AdapterError) as ctx:
            ClaudeAdapter(
                executable="claude",
                isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            ).launch(request)
        self.assertEqual(ctx.exception.code, "role-bundle-substitution")

    def test_valid_but_wrong_composed_digest_is_denied_before_process_launch(self) -> None:
        calls: list[object] = []

        def unreachable_runner(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise AssertionError("a substituted bundle must not start a process")

        request = _request(
            "python-executor", role_grant=("read", "write"),
            recipient_role="executor", bundle_digest="b" * 64,
            composition=LaunchComposition(
                recipient_role="executor", bundle_digest="a" * 64,
                allowed_scope=("pipeline_core/adapters.py",), role_grant=("read", "write"),
            ),
        )
        with self.assertRaises(AdapterError) as ctx:
            ClaudeAdapter(
                executable="claude", runner=unreachable_runner,
                isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
            ).launch(request)
        self.assertEqual(ctx.exception.code, "role-bundle-substitution")
        self.assertEqual(calls, [])

    def test_composed_request_cannot_widen_scope_or_grants(self) -> None:
        composition = LaunchComposition(
            recipient_role="executor", bundle_digest="a" * 64,
            allowed_scope=("pipeline_core/adapters.py",), role_grant=("read",),
        )
        for request in (
            _request("python-executor", recipient_role="executor", bundle_digest="a" * 64,
                     allowed_scope=("pipeline_core/adapters.py", "pipeline_core/dispatch.py"),
                     composition=composition),
            _request("python-executor", recipient_role="executor", bundle_digest="a" * 64,
                     role_grant=("read", "write"), composition=composition),
        ):
            with self.subTest(request=request):
                with self.assertRaises(AdapterError) as ctx:
                    ClaudeAdapter(
                        executable="claude", isolation_capabilities=_PROVEN_CLAUDE_ISOLATION_CAPABILITIES,
                    ).launch(request)
                self.assertEqual(ctx.exception.code, "role-bundle-substitution")


if __name__ == "__main__":
    unittest.main()
