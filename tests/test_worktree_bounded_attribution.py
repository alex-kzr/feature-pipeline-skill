"""WT-01 — the bounded worktree inspector: candidate matrix, parity, and denial paths.

The bounded inspector (:mod:`feature_pipeline.infrastructure.worktree.inspector`) reads Git
and index metadata for the whole repository but file bytes only for the *candidate* set —
the paths dirty or untracked when the window opened, plus anything Git reports changed when
it closed. This module builds a real Git repository per case, opens a window with
:func:`pipeline_core.worktree.capture_snapshot`, applies a change, closes it with
:func:`pipeline_core.worktree.attribute_executor_window`, and:

* checks the bounded attribution against the retained legacy full-snapshot attribution of
  the identical window — same state, same changed set, same classification, same diff text
  (AC-1, "characterization fixtures produce equivalent attribution");
* exercises deletions, renames, symlinks, a nested repository, and a submodule-declared
  boundary (Requirements);
* proves the denial paths: a missing boundary, a failed Git query, and an unreadable
  candidate each produce ``unavailable`` with a reason and never an empty successful
  manifest (AC-2).

Standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.infrastructure.git.inspector import GitInspection
from feature_pipeline.infrastructure.worktree.inspector import GitWorktreeInspector
from feature_pipeline.ports.worktree import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_UNAVAILABLE,
    WorktreeInspectionError,
)
from pipeline_core.reports import launch_artifacts
from pipeline_core.worktree import (
    STATE_KNOWN,
    STATE_UNAVAILABLE,
    WorktreeError,
    _legacy_capture_snapshot,
    attribute_executor_window,
    attribute_window,
    capture_snapshot,
)


def _git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    )


def _init(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")


def _commit(root: Path, message: str = "base") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


class _WindowHarness(unittest.TestCase):
    """Run one window through both inspectors and assert their attributions agree."""

    def _run_both(
        self, root: Path, mutate, *, allowed_scope
    ):
        run_dir = root / ".pipeline" / "runs" / "r1"
        exclude_roots = (run_dir,)
        legacy_before = _legacy_capture_snapshot(root, exclude_roots=exclude_roots)
        bounded_before = capture_snapshot(root, exclude_roots=exclude_roots)

        mutate()

        # The legacy attribution is computed first, from an after-snapshot taken before any
        # evidence file exists — so the two inspectors see the identical window.
        legacy_after = _legacy_capture_snapshot(root, exclude_roots=exclude_roots)
        legacy = attribute_window(
            legacy_before, legacy_after, allowed_scope=allowed_scope
        )

        artifacts = launch_artifacts(run_dir, "WT-01", 1)
        artifacts.directory.mkdir(parents=True, exist_ok=True)
        artifacts.executor_report.write_text("# r\n", encoding="utf-8")
        bounded = attribute_executor_window(
            bounded_before, root, artifacts=artifacts, task_id="WT-01",
            allowed_scope=allowed_scope, attempt=1, generation=1,
            executor_report=artifacts.executor_report, exclude_roots=exclude_roots,
        )
        return bounded, legacy, artifacts

    def _assert_parity(self, bounded, legacy) -> None:
        self.assertEqual(bounded.state, legacy.state)
        self.assertEqual(
            {(r["path"], r["status"], r["classification"]) for r in bounded.changed_files},
            {
                (r.path, r.status, r.classification)
                for r in legacy.changed_files
            },
        )
        self.assertEqual(
            {r["path"]: r["digest"] for r in bounded.changed_files},
            {r.path: r.digest for r in legacy.changed_files},
        )


class CandidateMatrixParityTests(_WindowHarness):
    def test_in_window_edit_of_a_clean_file_matches_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "src").mkdir()
            (root / "src" / "keep.py").write_text("clean\n", encoding="utf-8")
            (root / "src" / "edit.py").write_text("before\n", encoding="utf-8")
            _commit(root)

            bounded, legacy, _ = self._run_both(
                root,
                lambda: (root / "src" / "edit.py").write_text(
                    "after\n", encoding="utf-8"
                ),
                allowed_scope=("src/**",),
            )
            self._assert_parity(bounded, legacy)
            self.assertEqual([r["path"] for r in bounded.changed_files], ["src/edit.py"])
            self.assertEqual(bounded.state, STATE_KNOWN)

    def test_preexisting_dirt_is_excluded_but_a_further_edit_survives(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "a.py").write_text("v1\n", encoding="utf-8")
            (root / "b.py").write_text("v1\n", encoding="utf-8")
            _commit(root)
            (root / "a.py").write_text("v1\npre-existing\n", encoding="utf-8")  # dirt
            (root / "b.py").write_text("v1\npre-existing\n", encoding="utf-8")  # dirt

            bounded, legacy, _ = self._run_both(
                root,
                lambda: (root / "b.py").write_text(
                    "v1\npre-existing\nwindow\n", encoding="utf-8"
                ),
                allowed_scope=("**",),
            )
            self._assert_parity(bounded, legacy)
            self.assertEqual([r["path"] for r in bounded.changed_files], ["b.py"])

    def test_deletion_matches_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "gone.py").write_text("bye\n", encoding="utf-8")
            (root / "stay.py").write_text("stay\n", encoding="utf-8")
            _commit(root)

            bounded, legacy, _ = self._run_both(
                root, lambda: (root / "gone.py").unlink(), allowed_scope=("**",)
            )
            self._assert_parity(bounded, legacy)
            rows = {r["path"]: r["status"] for r in bounded.changed_files}
            self.assertEqual(rows, {"gone.py": "deleted"})

    def test_rename_matches_legacy_as_delete_plus_add(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "old_name.py").write_text("body of the file\n", encoding="utf-8")
            _commit(root)

            bounded, legacy, _ = self._run_both(
                root,
                lambda: os.rename(root / "old_name.py", root / "new_name.py"),
                allowed_scope=("**",),
            )
            self._assert_parity(bounded, legacy)
            self.assertEqual(
                {(r["path"], r["status"]) for r in bounded.changed_files},
                {("old_name.py", "deleted"), ("new_name.py", "added")},
            )

    def test_symlink_creation_is_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "target.txt").write_text("target\n", encoding="utf-8")
            _commit(root)

            def _mk_symlink() -> None:
                try:
                    os.symlink("target.txt", root / "link.txt")
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks not permitted on this platform")

            bounded, legacy, _ = self._run_both(
                root, _mk_symlink, allowed_scope=("**",)
            )
            self._assert_parity(bounded, legacy)
            rows = {r["path"]: r["status"] for r in bounded.changed_files}
            self.assertEqual(rows.get("link.txt"), "added")

    def test_nested_repository_boundary_is_crossed_and_matches_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "top.py").write_text("top\n", encoding="utf-8")
            nested = root / "vendor" / "lib"
            nested.mkdir(parents=True)
            _init(nested)
            (nested / "inner.py").write_text("inner v1\n", encoding="utf-8")
            _commit(nested)
            (root / ".gitmodules").write_text(
                '[submodule "lib"]\n\tpath = vendor/lib\n\turl = ./x\n', encoding="utf-8"
            )
            _commit(root)

            bounded, legacy, _ = self._run_both(
                root,
                lambda: (nested / "inner.py").write_text(
                    "inner v2\n", encoding="utf-8"
                ),
                allowed_scope=("vendor/**",),
            )
            self._assert_parity(bounded, legacy)
            self.assertEqual(
                [r["path"] for r in bounded.changed_files], ["vendor/lib/inner.py"]
            )

    def test_empty_window_is_known_empty_like_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "a.py").write_text("a\n", encoding="utf-8")
            _commit(root)

            bounded, legacy, artifacts = self._run_both(
                root, lambda: None, allowed_scope=("**",)
            )
            self.assertEqual(bounded.state, legacy.state)
            self.assertEqual(bounded.changed_files, [])
            manifest = json.loads(
                artifacts.implementation_manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["attribution_state"], "known-empty")


class DiffTextParityTests(_WindowHarness):
    def test_unified_diff_text_is_byte_identical_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "m.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            _commit(root)

            legacy_before = _legacy_capture_snapshot(root)
            bounded_before = capture_snapshot(root)
            (root / "m.py").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
            legacy = attribute_window(
                legacy_before,
                _legacy_capture_snapshot(root),
                allowed_scope=("**",),
            )
            inspection = GitWorktreeInspector().attribute(
                bounded_before.marker, str(root), allowed_scope=("**",)
            )

            # The bounded inspector renders the unified diff byte-for-byte as the legacy
            # full-snapshot inspector — only the read cost changes.
            self.assertEqual(inspection.diff_text, legacy.diff_text)
            self.assertIn("+TWO", inspection.diff_text)
            self.assertIn("+four", inspection.diff_text)


class DenialPathTests(_WindowHarness):
    """AC-2 — a partial or unavailable attribution is never an empty successful manifest."""

    def test_missing_boundary_is_unavailable_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)  # never `git init`-ed
            before = capture_snapshot(root)
            self.assertFalse(before.available)
            artifacts = launch_artifacts(root / "run", "WT-01", 1)
            result = attribute_executor_window(
                before, root, artifacts=artifacts, task_id="WT-01",
                allowed_scope=("**",), attempt=1, generation=1,
                executor_report=artifacts.executor_report,
            )
            self.assertEqual(result.state, STATE_UNAVAILABLE)
            self.assertIn("no Git repository boundary", result.reason or "")
            self.assertEqual(result.changed_files, [])
            manifest = json.loads(
                artifacts.implementation_manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["attribution_state"], STATE_UNAVAILABLE)
            self.assertTrue(manifest["reason"])

    def test_failed_status_at_close_is_unavailable_not_empty(self) -> None:
        calls = {"n": 0}
        real = GitWorktreeInspector()

        def flaky_runner(repo_dir: str, argv):
            calls["n"] += 1
            # Let the marker capture succeed; fail every status query afterwards.
            if list(argv[:2]) == ["status", "--porcelain"] and calls["n"] > 3:
                return GitInspection(available=False, stdout="", reason="disk gone")
            return real._run(repo_dir, argv)

        inspector = GitWorktreeInspector(runner=flaky_runner)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "a.py").write_text("a\n", encoding="utf-8")
            _commit(root)
            marker = inspector.capture_marker(str(root))
            self.assertTrue(marker.available)
            (root / "a.py").write_text("a2\n", encoding="utf-8")
            inspection = inspector.attribute(marker, str(root), allowed_scope=("**",))
            self.assertFalse(inspection.available)
            self.assertEqual(inspection.confidence, CONFIDENCE_UNAVAILABLE)
            self.assertIn("disk gone", inspection.reason or "")
            self.assertEqual(inspection.candidates, ())

    def test_unreadable_candidate_is_unavailable_but_listed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "a.py").write_text("readable\n", encoding="utf-8")
            _commit(root)
            before = capture_snapshot(root)

            real = GitWorktreeInspector()
            marker = before.marker
            assert marker is not None
            (root / "a.py").write_text("changed\n", encoding="utf-8")

            # Force the after-body read to look unreadable.
            orig = GitWorktreeInspector._after_body

            def unreadable_after(root_path, key):
                from feature_pipeline.infrastructure.worktree.inspector import _Body

                return _Body(None, unreadable=True)

            GitWorktreeInspector._after_body = staticmethod(unreadable_after)
            try:
                inspection = real.attribute(marker, str(root), allowed_scope=("**",))
            finally:
                GitWorktreeInspector._after_body = staticmethod(orig)

            self.assertFalse(inspection.available)
            self.assertEqual(inspection.confidence, CONFIDENCE_UNAVAILABLE)
            self.assertIn("a.py", inspection.reason or "")
            self.assertEqual([c.path for c in inspection.candidates], ["a.py"])

    def test_absolute_allowed_scope_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "a.py").write_text("a\n", encoding="utf-8")
            _commit(root)
            before = capture_snapshot(root)
            (root / "a.py").write_text("a2\n", encoding="utf-8")
            artifacts = launch_artifacts(root / ".pipeline" / "runs" / "r1", "WT-01", 1)
            absolute_entry = str((Path(tempfile.gettempdir()).resolve() / "elsewhere"))
            with self.assertRaises(WorktreeError) as ctx:
                attribute_executor_window(
                    before, root, artifacts=artifacts, task_id="WT-01",
                    allowed_scope=(absolute_entry,), attempt=1, generation=1,
                    executor_report=artifacts.executor_report,
                )
            self.assertEqual(ctx.exception.code, "invalid-allowed-scope")


class PortResultShapeTests(unittest.TestCase):
    def test_inspection_rejects_an_unknown_confidence(self) -> None:
        from feature_pipeline.ports.worktree import WorktreeInspection

        with self.assertRaises(WorktreeInspectionError):
            WorktreeInspection(
                available=True, confidence="maybe", boundaries=(), candidates=(),
                diff_text="",
            )

    def test_boundaries_and_candidates_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init(root)
            (root / "a.py").write_text("a\n", encoding="utf-8")
            _commit(root)
            inspector = GitWorktreeInspector()
            marker = inspector.capture_marker(str(root))
            (root / "a.py").write_text("a2\n", encoding="utf-8")
            (root / "b.py").write_text("new\n", encoding="utf-8")
            inspection = inspector.attribute(marker, str(root), allowed_scope=("a.py",))
            self.assertEqual(inspection.confidence, CONFIDENCE_KNOWN)
            self.assertTrue(inspection.boundaries)
            self.assertEqual(inspection.boundaries[0].kind.value, "working-root")
            classes = {c.path: c.classification for c in inspection.candidates}
            self.assertEqual(classes["a.py"], "in_allowed_scope")
            self.assertEqual(classes["b.py"], "out_of_scope")


if __name__ == "__main__":
    unittest.main()
