"""Characterization tests for portable persistence and execution primitives."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.artifacts import ArtifactReadError, write_json_atomic
from pipeline_core.commands import run_command
from pipeline_core.lease import LeaseHeldError, PipelineLease
from pipeline_core.redaction import build_rules, redact_text
from pipeline_core.state import (
    ACTOR_EXECUTOR,
    EXIT_LAUNCH_FAILED,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    ResumeError,
    Run,
    StateError,
    TransitionError,
    repo_relative,
)


class ArtifactCharacterizationTests(unittest.TestCase):
    def test_redaction_preserves_the_original_repository_path_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            spelling = root / "nested" / ".."

            self.assertEqual(
                redact_text(str(spelling / "secret"), build_rules(spelling)),
                "<repo>\\secret",
            )

    def test_atomic_write_redacts_nested_repository_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "reports" / "result.json"
            write_json_atomic(target, {"path": str(root / "secret"), "items": [str(root)]}, repo_root=root)
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(stored, {"path": "<repo>\\secret", "items": ["<repo>"]})

    def test_non_serializable_payload_preserves_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.json"
            target.write_text('{"old": true}\n', encoding="utf-8")
            with self.assertRaises(TypeError):
                write_json_atomic(target, {"bad": object()})
            self.assertEqual(target.read_text(encoding="utf-8"), '{"old": true}\n')

    def test_invalid_json_has_a_stable_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad.json"
            target.write_text("not json", encoding="utf-8")
            from pipeline_core.artifacts import read_json
            with self.assertRaises(ArtifactReadError) as caught:
                read_json(target)
            self.assertEqual(caught.exception.code, "invalid-json")


class StateCharacterizationTests(unittest.TestCase):
    def _run(self, root: Path) -> Run:
        prompt = root / "prompts" / "feature.md"
        prompt.parent.mkdir()
        prompt.write_text("feature", encoding="utf-8")
        return Run.create("portable-feature", prompt, None, root / "runs" / "portable-feature", root)

    def test_relative_path_rejects_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StateError) as caught:
                repo_relative("../secret", directory)
            self.assertEqual(caught.exception.code, "path-escape")

    def test_transitions_persist_and_resume_identity_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._run(root)
            run.add_task("NP-01")
            run.transition_task("NP-01", "ready")
            run.transition_task("NP-01", "running")
            run.transition_task("NP-01", "implemented", ACTOR_EXECUTOR)
            run.save()
            loaded = Run.load(run.run_dir, root)
            self.assertEqual(loaded.task("NP-01").status, "implemented")
            with self.assertRaises(ResumeError):
                loaded.resume(feature="other", prompt_path="prompts/feature.md", plan_path=None)

    def test_executor_cannot_mark_a_task_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            run.add_task("NP-01")
            for status in ("ready", "running"):
                run.transition_task("NP-01", status)
            run.transition_task("NP-01", "implemented", ACTOR_EXECUTOR)
            with self.assertRaises(TransitionError) as caught:
                run.transition_task("NP-01", "verified", ACTOR_EXECUTOR)
            self.assertEqual(caught.exception.code, "unauthorized-transition")


class LeaseCharacterizationTests(unittest.TestCase):
    def test_live_lease_excludes_another_writer_and_releases_its_own_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lease.json"
            first = PipelineLease(path, "first", pid=os.getpid())
            first.acquire("NP-01")
            with self.assertRaises(LeaseHeldError):
                PipelineLease(path, "second", pid=os.getpid()).acquire()
            first.release()
            self.assertFalse(path.exists())


class CommandCharacterizationTests(unittest.TestCase):
    def _run(self, root: Path) -> Run:
        prompt = root / "prompt.md"
        prompt.write_text("prompt", encoding="utf-8")
        return Run.create("commands", prompt, None, root / "runs" / "commands", root)

    def test_argv_execution_records_output_and_caps_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            record = run_command(run, "test", ".", [sys.executable, "-c", "print('x' * 100)"], output_limit=10)
            self.assertEqual(record["exit_code"], 0)
            self.assertEqual(record["argv"][0], sys.executable)
            self.assertEqual(len(record["stdout"]), 10)

    def test_missing_cwd_and_program_have_distinct_safe_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            self.assertEqual(run_command(run, "test", "missing", [sys.executable])["exit_code"], EXIT_LAUNCH_FAILED)
            self.assertEqual(run_command(run, "test", ".", ["definitely-not-a-program"])["exit_code"], EXIT_NOT_FOUND)

    def test_timeout_is_recorded_as_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            record = run_command(run, "test", ".", [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
            self.assertEqual(record["exit_code"], EXIT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
