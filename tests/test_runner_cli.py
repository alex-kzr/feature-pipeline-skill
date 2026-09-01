"""Tests for the portable core runner CLI (`scripts/run_pipeline.py`).

The CLI logic lives in :mod:`pipeline_core.runner_cli`; ``scripts/run_pipeline.py`` is a
thin shim over :func:`pipeline_core.runner_cli.main`. These tests drive ``main`` directly
with an explicit ``argv`` so nothing depends on ``sys.argv`` or process exit.
"""

from __future__ import annotations

import io
import json
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pipeline_core import runner_cli
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.verification import VerifierLaunchers

from tests.test_repair import ScriptedExecutor, StubVerifier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# Identifiers that must never leak into the portable core or its --help output.
PROJECT_IDENTITY_LITERALS = ("graphify", "cargo", "npm", "oxidium")

MINIMAL_REGISTRY = {
    "task_types": {
        "docs": {
            "stack": "text",
            "subagents": ["executor"],
            "root": "content",
            "checks": ["lint"],
            "storage": "runs",
        }
    },
    "stacks": {"text": {"runtime": "text"}},
    "subagents": {"executor": {"grant": "executor"}},
    "roots": {"content": "content"},
    "checks": {"lint": ["lint", "run"]},
    "storage": {"runs": ".pipeline/runs"},
}


def _run(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(args)
    return code, out.getvalue(), err.getvalue()


def _seed(fixture: str, dest: Path, *, tasks: list[dict], with_registry: bool = True) -> dict:
    """Copy a portable fixture into ``dest`` and add a plan; return anchor + arg data."""
    shutil.copytree(FIXTURES / fixture, dest, dirs_exist_ok=True)
    if fixture == "library-guide":
        profile_rel, project_subdir = "profile.json", "."
    else:
        profile_rel, project_subdir = "config/pipeline.json", "workspace"
    profile_path = dest / profile_rel
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if with_registry:
        profile["registry"] = json.loads(json.dumps(MINIMAL_REGISTRY))
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

    project_dir = dest if project_subdir == "." else dest / project_subdir
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "plan.json").write_text(
        json.dumps({"feature": "sample-feature", "tasks": tasks}, indent=2) + "\n",
        encoding="utf-8",
    )
    agents_root = dest / "_agents"
    (agents_root / "skills").mkdir(parents=True, exist_ok=True)
    return {
        "anchors": [
            "--project-root", str(dest),
            "--agents-root", str(agents_root),
            "--core-root", str(dest),
        ],
        "profile_rel": profile_rel,
        "project_dir": project_dir,
        "dest": dest,
    }


class HelpAndFlagSurfaceTests(unittest.TestCase):
    def test_help_lists_every_anchor_and_core_operational_flag(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            _run(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_help_text_carries_no_project_identity_literal(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            runner_cli.main(["--help"])
        help_text = out.getvalue().lower()
        for flag in (
            "--project-root", "--agents-root", "--core-root", "--profile", "--project-skill",
            "--plan", "--task", "--through", "--resume", "--mode", "--approve-plan",
            "--dry-run", "--commit", "--push",
        ):
            self.assertIn(flag, help_text)
        for literal in PROJECT_IDENTITY_LITERALS:
            self.assertNotIn(literal, help_text)

    def test_cli_source_carries_no_project_identity_literal(self) -> None:
        root = Path(runner_cli.__file__).resolve().parents[1]
        sources = [
            root / "pipeline_core" / "runner_cli.py",
            root / "scripts" / "run_pipeline.py",
        ]
        for source in sources:
            text = source.read_text(encoding="utf-8").lower()
            for literal in PROJECT_IDENTITY_LITERALS:
                self.assertNotIn(literal, text, f"{source.name} contains '{literal}'")


class PushDenialTests(unittest.TestCase):
    def test_push_is_refused_before_any_artifact_with_the_baseline_message(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, _, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--dry-run", "--push",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(
                err.strip(),
                "push is denied at this stage and the runner has no push code path.",
            )
            self.assertFalse(list(seed["project_dir"].glob("**/run.json")))

    def test_push_is_refused_even_without_other_arguments(self) -> None:
        code, _, err = _run(["--push"])
        self.assertEqual(code, 1)
        self.assertIn("push is denied at this stage", err)


class RelativeAnchorRejectionTests(unittest.TestCase):
    def _reject(self, profile_value: str) -> tuple[int, str]:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, _, err = _run(seed["anchors"] + [
                "--profile", profile_value, "--plan", "plan.json", "--dry-run",
            ])
        return code, err

    def test_absolute_profile_path_is_rejected(self) -> None:
        code, err = self._reject("/etc/passwd")
        self.assertEqual(code, 30)
        self.assertIn("relative", err.lower())

    def test_home_anchored_profile_path_is_rejected(self) -> None:
        code, err = self._reject("~/profile.json")
        self.assertEqual(code, 30)

    def test_parent_traversal_profile_path_is_rejected(self) -> None:
        code, err = self._reject("../profile.json")
        self.assertEqual(code, 30)
        self.assertIn("traverse", err.lower())

    def test_backslash_separator_profile_path_is_rejected(self) -> None:
        code, err = self._reject("nested\\profile.json")
        self.assertEqual(code, 30)

    def test_rejection_message_does_not_leak_an_absolute_host_path(self) -> None:
        code, err = self._reject("/etc/passwd")
        self.assertEqual(code, 30)
        self.assertNotIn("/etc/passwd", err)


C1_TO_C8 = ("C1.", "C2.", "C3.", "C4.", "C5.", "C6.", "C7.", "C8.")


class DryRunPlanShapeTests(unittest.TestCase):
    def test_dry_run_against_library_guide_prints_full_shape_and_is_byte_identical(self) -> None:
        tasks = [
            {"id": "T-01", "type": "docs"},
            {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
        ]
        outputs = []
        for _ in range(2):
            with TemporaryDirectory() as directory:
                dest = Path(directory) / "project"
                seed = _seed("library-guide", dest, tasks=tasks)
                code, out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--project-skill", "skills",
                    "--plan", "plan.json", "--dry-run",
                ])
                self.assertEqual(code, 10, err)
                self.assertNotIn(str(dest), out)
                self.assertNotIn(directory, out)
                outputs.append(out)
        for marker in C1_TO_C8:
            self.assertIn(marker, outputs[0])
        self.assertIn("T-01", outputs[0])
        self.assertIn("T-02", outputs[0])
        self.assertIn("stack=text", outputs[0])
        self.assertEqual(outputs[0], outputs[1], "dry-run output is not deterministic")

    def test_dry_run_against_incident_notes_also_prints_full_shape(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("incident-notes", dest, tasks=[{"id": "N-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 10, err)
        for marker in C1_TO_C8:
            self.assertIn(marker, out)
        self.assertIn("notes update", out)  # a stage argv from the incident-notes profile

    def test_dry_run_writes_nothing_into_the_project_tree(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            before = {p for p in dest.rglob("*")}
            code, _, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
            after = {p for p in dest.rglob("*")}
        self.assertEqual(code, 10, err)
        self.assertEqual(before, after)


class FailClosedRoutingTests(unittest.TestCase):
    def test_design_task_type_fails_closed_with_unresolved_executor(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "D-01", "type": "design"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("unresolved-executor", out)

    def test_unknown_task_type_fails_closed_with_unknown_task_type(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "X-01", "type": "sculpture"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("unknown-task-type", out)

    def test_profile_without_a_registry_fails_closed_for_every_task(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}],
                         with_registry=False)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("unresolved-executor", out)


class DryRunExitCodeTests(unittest.TestCase):
    """DR-5: a non-blocked dry run reports a pending delivery gate, exit 10."""

    def _dry_run(self, tasks: list[dict], extra: list[str] | None = None,
                 *, with_registry: bool = True) -> tuple[int, str, str]:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=tasks, with_registry=with_registry)
            return _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ] + (extra or []))

    def test_non_blocked_dry_run_returns_gate_pending(self) -> None:
        code, out, err = self._dry_run([{"id": "T-01", "type": "docs"}])
        self.assertEqual(code, 10, err)
        self.assertIn("C5. Exit code: 10", out)

    def test_unmet_dependency_dry_run_still_returns_blocked(self) -> None:
        tasks = [
            {"id": "T-01", "type": "docs"},
            {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
        ]
        code, out, err = self._dry_run(tasks, ["--task", "T-02"])
        self.assertEqual(code, 20, err)

    def test_absolute_path_dry_run_still_returns_error(self) -> None:
        code, _, _ = self._dry_run(
            [{"id": "T-01", "type": "docs"}], ["--prompt", "/etc/pipeline/prompt.md"]
        )
        self.assertEqual(code, 30)

    def test_unknown_type_dry_run_still_returns_error(self) -> None:
        code, out, _ = self._dry_run([{"id": "X-01", "type": "sculpture"}])
        self.assertEqual(code, 30)
        self.assertIn("unknown-task-type", out)


class DeliveryGateFlagTests(unittest.TestCase):
    """DR-6: --approve-final-diff and --commit-approved-manifest advance the C4 plan."""

    def _seeded(self, directory: str):
        dest = Path(directory) / "project"
        return _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])

    def test_approve_final_diff_advances_the_final_diff_gate_in_the_dry_run_plan(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
                "--approve-plan", "--approve-final-diff",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("plan - satisfied", out)
        self.assertIn("final-diff - satisfied", out)
        self.assertIn("commit - pending", out)

    def test_final_diff_gate_never_bypasses_an_unapproved_plan_gate(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
                "--approve-final-diff",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("plan - pending", out)
        self.assertIn("final-diff - pending", out)

    def test_commit_approved_manifest_advances_the_commit_gate_when_earlier_gates_pass(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
                "--approve-plan", "--approve-final-diff", "--commit-approved-manifest",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("commit - satisfied (requested)", out)

    def test_commit_approved_manifest_never_bypasses_earlier_gates(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
                "--commit-approved-manifest",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("plan - pending", out)
        self.assertIn("commit - pending", out)

    def test_new_gate_flags_never_advance_a_real_run_past_the_plan_gate(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--approve-final-diff", "--commit-approved-manifest",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("plan gate pending", out)

    def test_new_gate_flags_create_no_run_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, _, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
                "--approve-plan", "--approve-final-diff", "--commit-approved-manifest",
            ])
            self.assertEqual(code, 10, err)
            self.assertFalse(list(seed["project_dir"].glob("**/run.json")))


class ForwardedLegacyFlagTests(unittest.TestCase):
    """DR-6: every MI-01 core-classified flag the launcher forwards is accepted -
    implemented or a documented no-op - never an argparse exit 2 from inside the core."""

    ACCEPTED_NO_OP = [
        ["--adapter", "auto"],
        ["--max-repair-attempts", "3"],
        ["--routine-output-byte-budget", "4096"],
        ["--diagnostic-output-byte-budget", "4096"],
        ["--verbose"],
        ["--quiet"],
    ]

    def _dry_run(self, extra: list[str], tasks=None):
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest,
                         tasks=tasks or [{"id": "T-01", "type": "docs"}])
            return _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ] + extra)

    def test_each_accepted_no_op_flag_parses_and_does_not_change_the_exit_code(self) -> None:
        for extra in self.ACCEPTED_NO_OP:
            with self.subTest(flag=extra[0]):
                code, out, err = self._dry_run(extra)
                self.assertEqual(code, 10, f"{extra} -> {err}")
                self.assertIn("C1.", out)

    def test_unattended_is_an_alias_of_mode_unattended(self) -> None:
        code, out, err = self._dry_run(["--unattended"])
        self.assertEqual(code, 10, err)
        self.assertIn("mode: unattended", out)

    def test_status_prints_the_recorded_run_state_and_exits_zero(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            run_dir = seed["project_dir"] / ".pipeline" / "runs" / "sample-feature"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(json.dumps({
                "schema_version": 1, "feature": "sample-feature",
                "prompt_path": "plan.json", "plan_path": "plan.json",
                "run_id": "fixed-run-id", "status": "running",
                "tasks": [{"id": "T-01", "status": "implemented", "depends_on": [],
                           "attempts": 1, "blocker": None}],
                "history": [], "commands": [],
            }, indent=2) + "\n", encoding="utf-8")
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--status",
            ])
        self.assertEqual(code, 0, err)
        self.assertIn("status: running", out)
        self.assertIn("T-01", out)
        self.assertNotIn(str(dest), out)

    def test_status_reports_no_recorded_run_and_exits_zero(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--status",
            ])
        self.assertEqual(code, 0, err)
        self.assertIn("no recorded run", out)

    def test_help_lists_every_forwarded_flag_and_no_project_literal(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            runner_cli.main(["--help"])
        help_text = out.getvalue().lower()
        for flag in (
            "--approve-final-diff", "--commit-approved-manifest", "--status",
            "--unattended", "--adapter", "--max-repair-attempts",
            "--routine-output-byte-budget", "--diagnostic-output-byte-budget",
            "--verbose", "--quiet",
        ):
            self.assertIn(flag, help_text)
        for literal in PROJECT_IDENTITY_LITERALS:
            self.assertNotIn(literal, help_text)
        for banned in ("archive", "purge", "retry", "maintenance", "recovery", "unblock"):
            self.assertNotIn(banned, help_text)


class DependencyAndCommitGateTests(unittest.TestCase):
    def test_task_with_an_unmet_dependency_fails_closed(self) -> None:
        tasks = [
            {"id": "T-01", "type": "docs"},
            {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
        ]
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=tasks)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--dry-run",
            ])
        self.assertEqual(code, 20, err)
        self.assertIn("dependency-not-satisfied", out)
        self.assertIn("T-01", out)

    def test_commit_flag_cannot_skip_the_plan_gate_in_a_real_run(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--commit",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("plan gate pending", out)

    def test_commit_gate_stays_pending_in_the_dry_run_plan(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--dry-run", "--commit",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("commit - pending", out)


class DryRunAttestationTests(unittest.TestCase):
    """RDS-09: `--dry-run` resolves `--attest-dependency` the same read-only way `--mode
    execute` does, instead of a separate code path that never consults it."""

    TASKS = [
        {"id": "T-01", "type": "docs"},
        {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
    ]

    def _seeded(self, directory: str):
        dest = Path(directory) / "project"
        return _seed("library-guide", dest, tasks=self.TASKS)

    def _force_verified(self, life: RunLifecycle, task_id: str) -> None:
        life.transition(task_id, "running", actor=ACTOR_RUNNER)
        life.transition(task_id, "implemented", actor=ACTOR_RUNNER)
        life.run.record_verdicts(task_id, "PASS", "PASS")
        life.run.save()

    def _make_source_run(
        self, project_dir: Path, source_feature: str, *,
        verified: bool, dep_id: str = "T-01",
    ) -> None:
        """A hand-built source run under the same `.pipeline/runs` storage root the dry-run's
        own would-be run directory resolves to, tracking `dep_id` in a chosen status."""
        prompt = plan = project_dir / "plan.json"
        run = Run.create(
            source_feature, prompt, plan,
            project_dir / ".pipeline" / "runs" / source_feature, project_dir)
        life = RunLifecycle.initialize(run, tasks=[(dep_id, [])])
        if verified:
            self._force_verified(life, dep_id)

    def test_valid_attestation_stops_reporting_dependency_not_satisfied(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            self._make_source_run(seed["project_dir"], "source-feature", verified=True)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--attest-dependency", "T-01=source-feature", "--dry-run",
            ])
        self.assertEqual(code, 10, err)
        self.assertNotIn("T-02: pending -> blocked", out)
        self.assertIn("T-02: pending -> ready -> running -> implemented -> verified", out)

    def test_valid_attestation_dispatches_no_adapter_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            self._make_source_run(seed["project_dir"], "source-feature", verified=True)
            source_run_json = (
                seed["project_dir"] / ".pipeline" / "runs" / "source-feature" / "run.json")
            before = source_run_json.read_text(encoding="utf-8")
            code, _out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--attest-dependency", "T-01=source-feature", "--dry-run",
            ])
            self.assertEqual(code, 10, err)
            self.assertFalse(
                (seed["project_dir"] / ".pipeline" / "runs" / "sample-feature"
                 / "run.json").exists())
            self.assertEqual(source_run_json.read_text(encoding="utf-8"), before)

    def test_bad_syntax_fails_the_dry_run_the_same_way_a_real_run_would(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, _out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--attest-dependency", "T-01", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("DEP_ID=SOURCE_FEATURE", err)

    def test_through_scope_is_refused_before_printing_a_plan(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--through", "T-02", "--attest-dependency", "T-01=source-feature", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("attestation-requires-task-scope", err)
        self.assertEqual(out, "")

    def test_dep_id_not_a_declared_dependency_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, _out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-01", "--attest-dependency", "T-01=source-feature", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("attestation-not-a-dependency", err)

    def test_duplicate_dep_id_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, _out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--attest-dependency", "T-01=one",
                "--attest-dependency", "T-01=two", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("duplicate-attestation-dependency", err)

    def test_missing_source_run_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            code, _out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--attest-dependency", "T-01=does-not-exist", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("attestation-source-missing", err)

    def test_unverified_source_dependency_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seeded(directory)
            self._make_source_run(seed["project_dir"], "source-feature", verified=False)
            code, _out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--attest-dependency", "T-01=source-feature", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("attestation-source-not-verified", err)


RICH_EXECUTE_TASK = {
    "id": "TSK-01",
    "title": "Rich execute task",
    "path": "tasks/TSK-01.md",
    "task_type": "docs",
    "executor": "docs-executor",
    "depends_on": [],
    "allowed_scope": ["content/tsk-01.txt"],
    "acceptance_criteria": ["The task reaches verified."],
    "verification_commands": [],
    "max_repair_attempts": 2,
}


def _fake_execute_adapters(_project_dir):
    executor = ScriptedExecutor(("implemented",))
    launchers = VerifierLaunchers(task=StubVerifier(("PASS",)), test=StubVerifier(("PASS",)))
    return executor, launchers, {"claude": True, "codex": False}


class ExecuteModeTests(unittest.TestCase):
    """`--mode execute` wiring: the CLI stays thin and delegates to `execution.execute_run`."""

    def _seed_rich(self, directory: str, tasks: list[dict]):
        dest = Path(directory) / "project"
        seed = _seed("library-guide", dest, tasks=[{"id": "TSK-01", "type": "docs"}])
        (seed["project_dir"] / "plan.json").write_text(
            json.dumps({"feature": "sample-feature", "tasks": tasks}, indent=2) + "\n",
            encoding="utf-8")
        return seed

    def test_help_lists_execute_as_a_mode(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(out):
            runner_cli.main(["--help"])
        self.assertIn("execute", out.getvalue())

    def test_execute_without_plan_approval_is_gate_pending(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [RICH_EXECUTE_TASK])
            with patch.object(runner_cli, "make_execute_adapters", _fake_execute_adapters):
                code, out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json", "--mode", "execute",
                ])
            self.assertEqual(code, 10, err)
            self.assertIn("plan gate pending", out)
            self.assertFalse(
                (seed["project_dir"] / ".pipeline" / "runs" / "sample-feature"
                 / "run.json").exists())

    def test_execute_with_plan_approval_runs_a_task_to_verified(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [RICH_EXECUTE_TASK])
            with patch.object(runner_cli, "make_execute_adapters", _fake_execute_adapters):
                code, out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json",
                    "--mode", "execute", "--approve-plan",
                ])
            self.assertEqual(code, 0, err)
            self.assertIn("verified", out)
            run = json.loads(
                (seed["project_dir"] / ".pipeline" / "runs" / "sample-feature" / "run.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(run["tasks"][0]["status"], "verified")

    def test_execute_rejects_an_id_and_type_only_plan(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [{"id": "TSK-01", "type": "docs"}])
            with patch.object(runner_cli, "make_execute_adapters", _fake_execute_adapters):
                code, out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json",
                    "--mode", "execute", "--approve-plan",
                ])
            self.assertEqual(code, 30, out)
            self.assertIn("full task metadata", err)

    def test_execute_unattended_opt_in_satisfies_the_gate(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [RICH_EXECUTE_TASK])
            with patch.object(runner_cli, "make_execute_adapters", _fake_execute_adapters):
                code, out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json",
                    "--mode", "execute", "--unattended",
                ])
            self.assertEqual(code, 0, err)
            self.assertIn("verified", out)

    def test_execute_unknown_adapter_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [RICH_EXECUTE_TASK])

            def _no_adapter(_project_dir):
                executor, launchers, _env = _fake_execute_adapters(_project_dir)
                return executor, launchers, {}

            with patch.object(runner_cli, "make_execute_adapters", _no_adapter):
                code, _out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json",
                    "--mode", "execute", "--approve-plan",
                ])
            self.assertEqual(code, 30)
            self.assertIn("adapter-unavailable", err)

    def test_attest_dependency_needs_an_equals_separator(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [RICH_EXECUTE_TASK])
            with patch.object(runner_cli, "make_execute_adapters", _fake_execute_adapters):
                code, _out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json",
                    "--mode", "execute", "--approve-plan", "--task", "TSK-01",
                    "--attest-dependency", "TSK-00",
                ])
            self.assertEqual(code, 30, err)
            self.assertIn("DEP_ID=SOURCE_FEATURE", err)

    def test_attest_dependency_rejects_an_unsafe_source_feature(self) -> None:
        with TemporaryDirectory() as directory:
            seed = self._seed_rich(directory, [RICH_EXECUTE_TASK])
            with patch.object(runner_cli, "make_execute_adapters", _fake_execute_adapters):
                code, _out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--plan", "plan.json",
                    "--mode", "execute", "--approve-plan", "--task", "TSK-01",
                    "--attest-dependency", "TSK-00=../escape",
                ])
            self.assertEqual(code, 30, err)
            self.assertIn("bare directory name", err)
            self.assertFalse(
                (seed["project_dir"] / ".pipeline" / "runs" / "sample-feature"
                 / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
