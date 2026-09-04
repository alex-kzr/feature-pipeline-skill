"""BL-02 — executable characterization of the eight critical ambiguous behaviors.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 0). Prompt findings
``F-01`` .. ``F-08`` (``.prompts/feature-pipeline-refactor.md`` §4.1).

Each test below turns one finding from an observation into reproducible evidence: it asserts
the behavior **as it is today**, not the behavior a fix would produce. Every finding is a
correction target — the disposition table in
``docs/validation/refactor-compatibility-baseline.md`` records, per finding, that it is
corrected through an approved ADR (BL-03) and names the later task that does the correction.
When that task lands, the paired test here is expected to fail; that failure is the signal
the contract moved deliberately, and the test is then updated alongside the change.

This module changes **no production module** (BL-02 AC-2): it only reads public APIs, the
persisted ``run.json``, and — where the finding is a missing wiring rather than an observable
output — module source through :mod:`inspect`.

Standard library only.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from schemas import SchemaError
from schemas.contracts import TaskSpec

from feature_pipeline.application import task_engine as task_engine_mod
from feature_pipeline.application import verification_service as verification_service_mod
from feature_pipeline.application.diagnostic_service import (
    DIAGNOSTIC_COLLECTION_FAILED,
    DiagnosticService,
)
from pipeline_core import dispatch as dispatch_mod
from pipeline_core import execution as execution_mod
from pipeline_core import runner_cli
from pipeline_core import verification as verification_mod
from pipeline_core.adapters import LaunchResult
from pipeline_core.commands import (
    DIAGNOSTIC_OUTPUT_BUDGET,
    run_command,
)
from pipeline_core.dispatch import DispatchRequest, dispatch_executor
from pipeline_core.execution import ExecuteControls, ExecuteRequest, TaskExecution, _apply_repair_bound
from pipeline_core.git_port import GitPort, GitSafetyError
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.worktree import IN_SCOPE, OUT_OF_SCOPE, STATE_KNOWN, attribute_window
from pipeline_core import state as state_mod
from pipeline_core import worktree as worktree_mod


# --- shared fixtures -----------------------------------------------------------------------


def _spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = dict(
        id="BL-02",
        title="Characterize critical ambiguous behavior",
        path="docs/plans/tasks/BL-02_critical-behavior-characterization.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("in_scope.py",),
        out_of_scope=("secret.py",),
        required_skills=(
            ".agents/skills/software-development/test-driven-development/SKILL.md",),
        verification_commands=(),
        max_repair_attempts=2,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def _running_life(root: Path, spec: TaskSpec) -> RunLifecycle:
    prompt = root / "prompts" / "feature.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("feature", encoding="utf-8")
    run = Run.create("charcz", prompt, None, root / "storage" / "charcz", root)
    life = RunLifecycle.initialize(run, tasks=[(spec.id, [])])
    life.transition(spec.id, "running", actor=ACTOR_RUNNER)
    return life


class _ScriptedAdapter:
    """The two-call dispatch shape (executor launch, then same-session status envelope).

    ``on_launch`` runs once during the executor launch so a test can mutate the worktree
    exactly inside the attributed window.
    """

    def __init__(self, *, on_launch=None, status: str = "implemented") -> None:
        self.on_launch = on_launch
        self.status = status

    def launch(self, request):  # noqa: ANN001 - test double
        if request.no_tools or request.resume_session_id:
            body = json.dumps(
                {"role": "executor", "status": self.status,
                 "task_id": request.task_id, "attempt": 1})
            Path(request.report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.report_path).write_text(body, encoding="utf-8")
            return LaunchResult(exit_code=0, stdout="", session_id="sess-1")
        if self.on_launch is not None:
            self.on_launch()
        prose = f"# Executor report\n\n- Status: {self.status}\n"
        Path(request.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(request.report_path).write_text(prose, encoding="utf-8")
        return LaunchResult(exit_code=0, stdout="", session_id="sess-1")


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "in_scope.py").write_text("start\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


# --- F-01 --------------------------------------------------------------------------------------


class F01DryRunAndExecuteCompileDifferentPipelines(unittest.TestCase):
    """F-01 — historically the dry-run plan resolved a per-task route (stack, checks,
    subagents, root, storage) while ``execute`` resolved routes only far enough to pick the
    *first* storage directory, so previewed routing was not the routing that ran.

    Disposition: corrected through an approved ADR (BL-03) — CP-01 built the one immutable
    ``CompiledRunPlan`` + its pure compiler, and **CP-02 (this landing)** cut the production
    dry-run / execute / resume paths over to it. The tests below now assert the *unified*
    path: one ``compile_run_plan`` call per invocation, one plan object, rendered by the
    preview and consumed by execution.
    """

    def test_execute_now_carries_the_resolved_route_as_one_immutable_plan(self) -> None:
        request_fields = {f.name for f in fields(ExecuteRequest)}
        # Route/selection/control resolution reaches execution as ONE object, not as
        # scattered ad-hoc fields.
        self.assertIn("compiled_plan", request_fields)
        for scattered in ("route", "resolved_route", "stack", "checks", "subagents",
                          "storage", "root"):
            self.assertNotIn(scattered, request_fields)
        # ``execute_run`` reads the selection, per-task repair bound and the resume guard
        # off that plan instead of re-deriving them.
        exec_src = inspect.getsource(execution_mod.execute_run)
        self.assertIn("plan = request.compiled_plan", exec_src)
        self.assertIn("plan.selection", exec_src)
        self.assertIn("plan.task(tid).repair_bound", exec_src)

    def test_default_role_grant_constant_is_still_the_documented_fallback(self) -> None:
        # The compiled plan now carries a per-task ``role_grant``; the module constant
        # remains the value it resolves to for every synthesised route.
        self.assertEqual(
            execution_mod.DEFAULT_ROLE_GRANT, ("read", "run_checks", "write"))

    def test_execute_now_builds_and_consumes_the_one_compiled_plan(self) -> None:
        # CP-02 landed: ``_execute`` compiles one ``CompiledRunPlan`` (the object the
        # dry-run preview also renders), rejects an unroutable / mixed-storage selection
        # before any run state, and hands the plan to ``execute_run``.
        src = inspect.getsource(runner_cli._execute)
        self.assertIn("compile_run_plan(", src)
        self.assertIn("compiled_plan=compiled_plan", src)
        self.assertIn("route_reasons(", src)
        self.assertNotIn("storage_dir = resolved.storage", src)
        # ``execute_run`` takes its selection, repair bound and resume guard from the plan.
        exec_src = inspect.getsource(execution_mod.execute_run)
        self.assertIn("request.compiled_plan", exec_src)
        self.assertIn("_ensure_plan_compatible", exec_src)

    def test_dry_run_now_renders_from_the_one_compiled_plan(self) -> None:
        # CP-02 landed: the dry-run preview no longer hand-resolves a route. ``_build``
        # compiles one ``CompiledRunPlan`` (the object execute also consumes) and renders
        # C1–C8 from it via ``feature_pipeline.application.render_plan.render_dry_run``.
        build_src = inspect.getsource(runner_cli._build)
        self.assertIn("compile_run_plan(", build_src)
        self.assertIn("render_dry_run(", build_src)
        self.assertFalse(hasattr(runner_cli, "_plan_text"))
        self.assertFalse(hasattr(runner_cli, "_select"))
        for gone in ("resolved.route.stack", "resolved.route.subagents", "resolved.storage"):
            self.assertNotIn(gone, build_src)


# --- F-02 --------------------------------------------------------------------------------------


class F02DurabilityInvariantIsNotMet(unittest.TestCase):
    """F-02 — ``commands.run_command`` records a completed command straight onto the
    in-memory ``Run`` and never flushes ``run.json``. A crash between the command finishing
    and the surrounding flow's next save loses that command and its evidence from persisted
    state, even though it really ran. ``RunLifecycle.record_command`` (the owning path) does
    flush, but production command execution does not go through it.

    Disposition: correct through an approved ADR (BL-03) — every durable change goes through
    a repository transaction / lifecycle operation with a documented crash-consistency
    boundary. INTENDED FOLLOW-UP: DS-01 (separate typed state and pure transitions) and
    DS-03 (transactional persistence and run identity).
    """

    def test_run_command_mutates_memory_but_leaves_run_json_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            run = life.run
            run.save()  # the last durable checkpoint before the command runs

            run_command(run, f"task:{spec.id}", ".",
                        [sys.executable, "-c", "print('did real work')"])

            # In memory the command is recorded...
            self.assertEqual(len(run.commands), 1)
            self.assertEqual(run.commands[0]["stage"], f"task:{spec.id}")
            # ...but a reader of run.json (a resume after a crash here) sees nothing.
            persisted = json.loads(
                (run.run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["commands"], [])

    def test_the_owning_lifecycle_path_would_have_flushed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            life.run.save()

            life.record_command(
                f"task:{spec.id}", ".", ["echo", "hi"], 0, 0.0, "hi", "")

            persisted = json.loads(
                (life.run.run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(len(persisted["commands"]), 1)


# --- F-03 --------------------------------------------------------------------------------------


class F03FreshRunIdentityAndOverwriteSemanticsAreUnsafe(unittest.TestCase):
    """F-03 — run storage is ``<storage>/<feature>`` with no per-run directory, and the run
    id has only timestamp-level uniqueness. A fresh invocation reuses the feature directory,
    overwrites ``run.json`` in place, and leaves earlier evidence files beside the new state.

    Disposition: correct through an approved ADR (BL-03) — an explicit run-identity policy
    (one immutable directory per run instance plus a ``latest`` index, or a single-active-run
    model that refuses a fresh start over existing state). INTENDED FOLLOW-UP: DS-02 (strict
    schema v3 and explicit migration) and DS-03 (transactional persistence and run identity).
    """

    def test_two_fresh_runs_in_the_same_second_get_the_same_run_id(self) -> None:
        original = state_mod._now
        state_mod._now = lambda: "2026-09-03T12:00:00Z"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                a = Run.create("feat", root / "p.md", None, root / "runs" / "feat", root)
                b = Run.create("feat", root / "p.md", None, root / "runs" / "feat", root)
            self.assertEqual(a.run_id, b.run_id)
            self.assertEqual(a.run_dir, b.run_dir)  # no run-id component in the path
        finally:
            state_mod._now = original

    def test_a_second_fresh_run_overwrites_run_json_and_orphans_old_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "feat"

            first = Run.create("feat", root / "p.md", None, run_dir, root)
            first.add_task("T-1")
            first.add_task("T-2")
            first.save()
            stale = run_dir / "reports" / "T-1" / "attempt-1" / "diagnostic-report.md"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("evidence from the first run\n", encoding="utf-8")

            second = Run.create("feat", root / "p.md", None, run_dir, root)
            second.add_task("T-9")
            second.save()

            persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual([t["id"] for t in persisted["tasks"]], ["T-9"])
            # The first run's evidence is still on disk, now unreferenced by run.json.
            self.assertTrue(stale.is_file())


# --- F-04 --------------------------------------------------------------------------------------


class F04OutOfScopeChangesAreNotEnforcedStructurally(unittest.TestCase):
    """F-04 — worktree attribution *labels* a changed file ``out_of_scope`` but nothing
    stops the task: ``dispatch_executor`` still transitions ``running -> implemented`` on a
    trusted report and hands the decision to the LLM verifier. The CLI documents that
    out-of-scope changes fail; the core has no deterministic gate for it.

    Disposition: correct through an approved ADR (BL-03) — the pipeline blocks or fails
    immediately after attribution when executor-owned out-of-scope changes exist; verifiers
    explain but do not decide a mechanical invariant; the runner never auto-reverts.
    INTENDED FOLLOW-UP: WT-02 (enforce the deterministic scope gate).
    """

    def test_attribution_labels_out_of_scope_without_raising_or_downgrading_trust(self) -> None:
        before = worktree_mod.WorktreeSnapshot({"in_scope.py": _sf(b"a\n")}, available=True)
        after = worktree_mod.WorktreeSnapshot(
            {"in_scope.py": _sf(b"b\n"), "secret.py": _sf(b"leaked\n")}, available=True)

        result = attribute_window(before, after, allowed_scope=("in_scope.py",))

        # A trusted, non-error state even though a file landed outside the allowed scope.
        self.assertEqual(result.state, STATE_KNOWN)
        by_path = {c.path: c.classification for c in result.changed_files}
        self.assertEqual(by_path["in_scope.py"], IN_SCOPE)
        self.assertEqual(by_path["secret.py"], OUT_OF_SCOPE)

    def test_dispatch_reaches_implemented_with_an_out_of_scope_change_in_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec()
            life = _running_life(root, spec)

            def _leak() -> None:
                (root / "in_scope.py").write_text("touched\n", encoding="utf-8")
                (root / "secret.py").write_text("written outside scope\n", encoding="utf-8")

            outcome = dispatch_executor(
                life,
                DispatchRequest(
                    spec=spec, role_grant=("read", "run_checks", "write"),
                    anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
                    plan_path="docs/plans/2026-09-03-feature-pipeline-refactor.md"),
                _ScriptedAdapter(on_launch=_leak),
            )

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(life.run.task(spec.id).status, "implemented")
            manifest = json.loads(
                (root / outcome.attribution.manifest).read_text(encoding="utf-8"))
            classes = {row["path"]: row["classification"]
                       for row in manifest["changed_files"]}
            self.assertEqual(classes.get("secret.py"), OUT_OF_SCOPE)


def _sf(data: bytes) -> worktree_mod.SnapshotFile:
    return worktree_mod.SnapshotFile(data)


# --- F-05 --------------------------------------------------------------------------------------


class F05DiagnosticsDoNotCollectAllRequiredEvidence(unittest.TestCase):
    """F-05 — CORRECTED by VP-01 (extract task, verification, and diagnostic services).

    History: the diagnostic writer took ``git_diff`` / ``git_status`` parameters that
    defaulted to empty and the production call sites passed neither, so an uncollected
    section rendered as an empty *successful* one.

    Now every production failure path assembles its diagnostic through the one boundary,
    :class:`feature_pipeline.application.diagnostic_service.DiagnosticService`, which queries
    a read-only Git inspection at the moment of failure. Real evidence is embedded; a
    collection failure is stated explicitly (``diagnostic-collection-failed`` in the Note
    plus an ``evidence unavailable`` section body) and never presented as an empty success
    (ADR 007 §-diagnostics).
    """

    def test_production_failure_paths_route_through_the_one_service_boundary(self) -> None:
        # The renderer is no longer called directly from the orchestration modules; the
        # engine and the verifier orchestration both go through DiagnosticService.collect.
        self.assertNotIn("write_diagnostic_report(", inspect.getsource(execution_mod))
        self.assertNotIn("write_diagnostic_report(", inspect.getsource(verification_mod))
        engine_src = inspect.getsource(task_engine_mod.TaskEngine)
        self.assertIn("_diagnostics.collect(", engine_src)
        self.assertIn("_DIAGNOSTICS.collect(", inspect.getsource(verification_mod))

    def test_service_embeds_real_git_evidence_collected_at_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "in_scope.py").write_text("changed by the executor\n", encoding="utf-8")
            spec = _spec()
            run = _running_life(root, spec).run

            path = DiagnosticService().collect(
                run, task_id=spec.id, attempt=1,
                note="maximum repair attempts (2) reached")

            text = path.read_text(encoding="utf-8")
            diff_body = text.split("## git diff", 1)[1].split("## ", 1)[0]
            status_body = text.split("## git status", 1)[1].split("## ", 1)[0]
            self.assertIn("diff --git", diff_body)
            self.assertIn("changed by the executor", diff_body)
            self.assertIn("in_scope.py", status_body)
            self.assertNotIn(DIAGNOSTIC_COLLECTION_FAILED, text)

    def test_service_states_collection_failure_when_git_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)  # deliberately not a git repository
            spec = _spec()
            run = _running_life(root, spec).run

            path = DiagnosticService().collect(
                run, task_id=spec.id, attempt=1, note="blocked at the limit")

            text = path.read_text(encoding="utf-8")
            self.assertIn(DIAGNOSTIC_COLLECTION_FAILED, text)
            diff_body = text.split("## git diff", 1)[1].split("## ", 1)[0]
            self.assertIn("evidence unavailable", diff_body.lower())
            # the note is preserved alongside the explicit collection-failure marker.
            self.assertIn("blocked at the limit", text)


# --- F-06 --------------------------------------------------------------------------------------


class F06GitSafetyUsesABypassableDenylist(unittest.TestCase):
    """F-06 — RS-03 replaced the bypassable denylist with a parsed read-only allowlist
    (ADR 007 §7). ``GitPort`` now recognises the subcommand as ``argv[0]`` itself and one of
    a fixed set of read-only operations; a global option before the subcommand, an attached
    ``--git-dir=`` redirect, and an alias/``-c`` override are each refused because there is no
    position for them to hide in. A plain mutating subcommand is still denied, now because it
    is simply not on the allowlist.

    Landed by: RS-03 (enforce argv policy, Git safety, and launch controls). This test was
    flipped from "the bypass sails through" to "the bypass is denied" when RS-03 landed.
    """

    def setUp(self) -> None:
        self._real_run = subprocess.run
        self._calls: list[list[str]] = []

        def _fake_run(argv, **_kwargs):  # noqa: ANN001, ANN003
            self._calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        subprocess.run = _fake_run  # type: ignore[assignment]

    def tearDown(self) -> None:
        subprocess.run = self._real_run  # type: ignore[assignment]

    def test_plain_mutating_subcommand_is_denied(self) -> None:
        port = GitPort(Path("."))
        for argv in (["push"], ["reset", "--hard"], ["checkout", "main"]):
            with self.assertRaises(GitSafetyError):
                port.run(argv)
        self.assertEqual(self._calls, [])  # nothing reached the subprocess

    def test_global_option_or_alias_before_a_mutation_is_now_denied(self) -> None:
        port = GitPort(Path("."))
        bypasses = (
            ["-c", "alias.sync=push", "sync"],       # -c alias indirection
            ["-C", "/somewhere/else", "push"],       # global option before the subcommand
            ["--git-dir=../other/.git", "push"],     # global option, attached form
        )
        for argv in bypasses:
            with self.subTest(argv=argv):
                with self.assertRaises(GitSafetyError):
                    port.run(argv)
        # Not one bypass argv reached the subprocess.
        self.assertEqual(self._calls, [])


# --- F-07 --------------------------------------------------------------------------------------


class F07ConfigurationValuesAreAcceptedButNotConsistentlyApplied(unittest.TestCase):
    """F-07 — the output byte budgets and serialized-program mutex are accepted (CLI flags,
    recorded controls) but never threaded into the command / adapter / report paths that run
    a task's verification. ``run_command`` falls back to the hard-coded module budgets, and
    execute passes no serialized-program set or comprehensive timeout policy.

    Disposition: correct through an approved ADR (BL-03), with a deprecation window for any
    control that becomes unsupported — every accepted control is validated, resolved,
    persisted, and used, or explicitly rejected; no operational flag is a silent no-op.
    INTENDED FOLLOW-UP: DF-02 (resolve controls and clocks at the boundary) and RS-01
    (consolidate local process execution).
    """

    def test_budgets_are_controls_but_not_execution_inputs(self) -> None:
        control_fields = {f.name for f in fields(ExecuteControls)}
        self.assertIn("routine_output_byte_budget", control_fields)
        self.assertIn("diagnostic_output_byte_budget", control_fields)

        exec_fields = {f.name for f in fields(TaskExecution)}
        for dropped in ("routine_output_byte_budget", "diagnostic_output_byte_budget",
                        "serialized_programs"):
            self.assertNotIn(dropped, exec_fields)

    def test_verification_gate_wires_neither_budget_nor_mutex_nor_timeout_policy(self) -> None:
        # VP-01 moved the gate into VerificationService; the F-07 wiring gap is unchanged.
        gate_src = inspect.getsource(verification_service_mod.VerificationService.verify)
        self.assertIn("run_verification_commands(", gate_src)
        self.assertNotIn("output_limit", gate_src)
        self.assertNotIn("byte_budget", gate_src)
        self.assertNotIn("serialized_programs", gate_src)

        run_src = inspect.getsource(task_engine_mod.TaskEngine.run)
        self.assertNotIn("serialized_programs", run_src)

    def test_run_command_falls_back_to_the_hard_coded_module_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            run = _running_life(root, spec).run
            script = "import sys; sys.stderr.write('x' * 200000)"

            record = run_command(
                run, f"task:{spec.id}", ".", [sys.executable, "-c", script])

            err = (run.run_dir / record["stderr_log"]).read_text(encoding="utf-8")
            # Truncated to the fixed diagnostic budget — there is no path for a configured
            # value to change this from inside a verification pass.
            self.assertLessEqual(len(err.encode("utf-8")), DIAGNOSTIC_OUTPUT_BUDGET)
            self.assertIn("bytes truncated", err)


# --- F-08 --------------------------------------------------------------------------------------


class F08ControlValidationCanOccurTooLate(unittest.TestCase):
    """F-08 — the ``--max-repair-attempts`` override is applied with ``dataclasses.replace``,
    which bypasses ``TaskSpec.build`` validation. A negative bound is accepted into the run
    and only rejected later, if and when the repair loop actually starts.

    Disposition: correct through an approved ADR (BL-03) — all CLI, profile, and task
    controls are validated before run state is created, leases are acquired, or any adapter
    is launched. INTENDED FOLLOW-UP: DF-02 (resolve controls and clocks at the boundary)
    and IN-01 (build one normalized task definition).
    """

    def test_taskspec_build_rejects_a_negative_bound(self) -> None:
        with self.assertRaises(SchemaError):
            _spec(max_repair_attempts=-3)

    def test_replace_and_apply_repair_bound_accept_the_negative_bound_unchecked(self) -> None:
        spec = _spec(max_repair_attempts=2)

        bypassed = replace(spec, max_repair_attempts=-3)
        self.assertEqual(bypassed.max_repair_attempts, -3)

        by_id = _apply_repair_bound([spec], -3)
        self.assertEqual(by_id[spec.id].max_repair_attempts, -3)

    def test_the_override_is_not_validated_before_state_or_launch(self) -> None:
        # execute_run applies the bound (step 2) before it initializes the durable lifecycle
        # or acquires the write lease (steps 3-4); the value itself is never validated here.
        src = inspect.getsource(execution_mod.execute_run)
        apply_at = src.index("_apply_repair_bound(")
        lifecycle_at = src.index("RunLifecycle.")
        lease_at = src.index("pipeline_lease(")
        self.assertLess(apply_at, lifecycle_at)
        self.assertLess(apply_at, lease_at)
        apply_src = inspect.getsource(execution_mod._apply_repair_bound)
        self.assertNotIn("SchemaError", apply_src)
        self.assertNotIn("< 0", apply_src)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
