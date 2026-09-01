"""Claude adapter argv/grant rules, session capture, and deterministic adapter resolution."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pipeline_core.adapters import (
    EXIT_TIMEOUT,
    AdapterError,
    ClaudeAdapter,
    LaunchRequest,
    build_claude_argv,
    effective_grant,
    parse_result_text,
    parse_session_id,
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
from schemas import Profile


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


# --- process capture --------------------------------------------------------------------------


class ClaudeLaunchTests(unittest.TestCase):
    def test_characterized_launch_returns_exit_output_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = _fake_executable(Path(directory), _FAKE_CLAUDE, "fake_claude.py")
            adapter = ClaudeAdapter(executable=executable)
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
            adapter = ClaudeAdapter(executable=executable)
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
            adapter = ClaudeAdapter(executable=executable, timeout=1.0)
            started = time.monotonic()
            result = adapter.launch(_request("executor"))
            elapsed = time.monotonic() - started
        self.assertEqual(result.exit_code, EXIT_TIMEOUT)
        self.assertLess(elapsed, 20.0)

    def test_launch_without_an_executable_fails_closed(self) -> None:
        adapter = ClaudeAdapter(resolver=lambda: None)
        self.assertFalse(adapter.available())
        with self.assertRaises(AdapterError) as ctx:
            adapter.launch(_request("executor"))
        self.assertEqual(ctx.exception.code, "adapter-unavailable")

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

    def test_codex_is_recognized_but_never_available(self) -> None:
        with self.assertRaises(AdapterResolutionError) as ctx:
            resolve_adapter("codex", {"codex": True})
        self.assertEqual(ctx.exception.code, "adapter-unavailable")
        # 'auto' must not fall back to another adapter when only codex is "present".
        with self.assertRaises(AdapterResolutionError):
            resolve_adapter("auto", {"codex": True})

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


if __name__ == "__main__":
    unittest.main()
