"""Runner-owned executor-window attribution: content snapshots across repository
boundaries, before/after subtraction that ignores pre-existing dirt and runner churn,
allowed-scope classification, and the distinct ``known`` / ``known-empty`` /
``unavailable`` persisted states."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline_core.reports import launch_artifacts
from pipeline_core.state import Run
from pipeline_core.worktree import (
    STATE_KNOWN,
    STATE_KNOWN_EMPTY,
    STATE_UNAVAILABLE,
    SnapshotFile,
    WorktreeError,
    WorktreeSnapshot,
    attribute_executor_window,
    attribute_window,
    capture_snapshot,
    persist_attribution,
)


# --- helpers -------------------------------------------------------------------------------


def _git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.py").write_text("alpha\n", encoding="utf-8")
    (root / "b.py").write_text("beta\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


def _snap(files: dict[str, bytes | None]) -> WorktreeSnapshot:
    return WorktreeSnapshot(
        {path: SnapshotFile(data) for path, data in files.items()}, available=True
    )


# --- capture_snapshot over a real repository --------------------------------------------------


class CaptureSnapshotTests(unittest.TestCase):
    def test_tracked_untracked_captured_and_run_dir_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "new.py").write_text("fresh\n", encoding="utf-8")  # untracked
            run_dir = root / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text("{}", encoding="utf-8")  # runner-owned

            snapshot = capture_snapshot(root, exclude_roots=(run_dir,))

            self.assertTrue(snapshot.available)
            self.assertEqual(
                set(snapshot.files), {"a.py", "b.py", "new.py"})
            self.assertNotIn("runs/run.json", snapshot.files)
            self.assertIn(b"alpha", snapshot.files["a.py"].data or b"")

    def test_missing_repository_boundary_is_unavailable_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = capture_snapshot(Path(directory))
            self.assertFalse(snapshot.available)
            self.assertIn("no Git repository boundary", snapshot.reason or "")

    def test_nested_declared_repository_is_a_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            nested = root / "vendor" / "lib"
            nested.mkdir(parents=True)
            _git(nested, "init", "-q")
            (nested / "inner.py").write_text("inner\n", encoding="utf-8")
            (root / ".gitmodules").write_text(
                '[submodule "lib"]\n\tpath = vendor/lib\n\turl = ./x\n', encoding="utf-8"
            )

            snapshot = capture_snapshot(root)

            self.assertIn("vendor/lib/inner.py", snapshot.files)


# --- attribute_window unit matrix ----------------------------------------------------------


class AttributeWindowTests(unittest.TestCase):
    def test_added_modified_deleted_are_classified_and_digested(self) -> None:
        before = _snap({"a.py": b"one\n", "gone.py": b"remove me\n"})
        after = _snap({"a.py": b"two\n", "new.py": b"brand new\n", "gone.py": None})

        result = attribute_window(
            before, after, allowed_scope=("a.py", "new.py", "gone.py"))

        self.assertEqual(result.state, STATE_KNOWN)
        rows = {row.path: row for row in result.changed_files}
        self.assertEqual(rows["a.py"].status, "modified")
        self.assertEqual(rows["new.py"].status, "added")
        self.assertEqual(rows["gone.py"].status, "deleted")
        self.assertIsNone(rows["gone.py"].digest)
        self.assertTrue(rows["a.py"].digest.startswith("sha256:"))
        self.assertIn("+two", result.diff_text)

    def test_preexisting_change_drops_out_but_a_further_edit_survives(self) -> None:
        # 'a.py' carried a pre-existing edit the executor did not touch — identical bytes on
        # both sides. 'b.py' was pre-dirty AND edited again inside the window.
        before = _snap({"a.py": b"pre-existing\n", "b.py": b"pre-existing\n"})
        after = _snap({"a.py": b"pre-existing\n", "b.py": b"executor went further\n"})

        result = attribute_window(before, after, allowed_scope=("a.py", "b.py"))

        self.assertEqual([row.path for row in result.changed_files], ["b.py"])

    def test_rename_shows_as_a_delete_and_an_add(self) -> None:
        before = _snap({"old.py": b"body\n"})
        after = _snap({"new.py": b"body\n", "old.py": None})

        result = attribute_window(before, after, allowed_scope=("**",))

        self.assertEqual(
            {(r.path, r.status) for r in result.changed_files},
            {("old.py", "deleted"), ("new.py", "added")},
        )

    def test_empty_window_is_known_empty(self) -> None:
        snapshot = _snap({"a.py": b"same\n"})
        result = attribute_window(snapshot, snapshot, allowed_scope=("a.py",))
        self.assertEqual(result.state, STATE_KNOWN_EMPTY)
        self.assertEqual(result.changed_files, ())
        self.assertIn("known-empty", result.diff_text)

    def test_out_of_scope_paths_are_classified_not_dropped(self) -> None:
        before = _snap({"inscope.py": b"x\n", "outside.py": b"y\n"})
        after = _snap({"inscope.py": b"x2\n", "outside.py": b"y2\n"})

        result = attribute_window(
            before, after, allowed_scope=("inscope.py",))

        rows = {row.path: row.classification for row in result.changed_files}
        self.assertEqual(rows["inscope.py"], "in_allowed_scope")
        self.assertEqual(rows["outside.py"], "out_of_scope")

    def test_glob_scope_supports_single_and_double_star(self) -> None:
        before = _snap(
            {"pkg/mod.py": b"a\n", "pkg/sub/deep.py": b"a\n", "top.txt": b"a\n"})
        after = _snap(
            {"pkg/mod.py": b"b\n", "pkg/sub/deep.py": b"b\n", "top.txt": b"b\n"})

        result = attribute_window(
            before, after, allowed_scope=("pkg/*.py", "pkg/**"))

        rows = {row.path: row.classification for row in result.changed_files}
        self.assertEqual(rows["pkg/mod.py"], "in_allowed_scope")  # pkg/*.py
        self.assertEqual(rows["pkg/sub/deep.py"], "in_allowed_scope")  # pkg/**
        self.assertEqual(rows["top.txt"], "out_of_scope")

    def test_unreadable_changed_file_is_unavailable_but_still_listed(self) -> None:
        before = _snap({"a.py": b"readable\n"})
        after = WorktreeSnapshot(
            {"a.py": SnapshotFile(None, unreadable=True)}, available=True)

        result = attribute_window(before, after, allowed_scope=("a.py",))

        self.assertEqual(result.state, STATE_UNAVAILABLE)
        self.assertIn("a.py", result.reason or "")
        self.assertEqual([row.path for row in result.changed_files], ["a.py"])

    def test_unavailable_before_snapshot_never_reports_empty(self) -> None:
        before = WorktreeSnapshot({}, available=False, reason="git exploded")
        after = _snap({"a.py": b"x\n"})

        result = attribute_window(before, after, allowed_scope=("a.py",))

        self.assertEqual(result.state, STATE_UNAVAILABLE)
        self.assertEqual(result.reason, "git exploded")

    def test_binary_change_is_summarised_not_diffed(self) -> None:
        before = _snap({"blob.bin": b"\x00\x01old"})
        after = _snap({"blob.bin": b"\x00\x01new"})

        result = attribute_window(before, after, allowed_scope=("**",))

        self.assertEqual(result.state, STATE_KNOWN)
        self.assertIn("binary file: blob.bin", result.diff_text)


# --- persistence -------------------------------------------------------------------------------


class PersistAttributionTests(unittest.TestCase):
    def test_manifest_and_diff_shape_and_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = launch_artifacts(root / "run", "RDS-05", 1)
            before = _snap({"a.py": b"one\n"})
            after = _snap({"a.py": b"two\n"})
            attribution = attribute_window(before, after, allowed_scope=("a.py",))

            result = persist_attribution(
                artifacts, attribution, task_id="RDS-05", attempt=1, generation=1,
                executor_report=artifacts.executor_report, repo_root=root,
            )

            self.assertTrue(result.manifest.endswith("implementation-manifest-1.json"))
            manifest = json.loads(
                artifacts.implementation_manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["task_id"], "RDS-05")
            self.assertEqual(manifest["attribution_state"], STATE_KNOWN)
            self.assertEqual(manifest["changed_files"][0]["path"], "a.py")
            self.assertTrue(artifacts.implementation_diff.is_file())

            with self.assertRaises(WorktreeError) as ctx:
                persist_attribution(
                    artifacts, attribution, task_id="RDS-05", attempt=1, generation=1,
                    executor_report=artifacts.executor_report, repo_root=root,
                )
            self.assertEqual(ctx.exception.code, "implementation-evidence-overwrite")


# --- end to end over a real repository, plus the execution-evidence link --------------------


class AttributeExecutorWindowTests(unittest.TestCase):
    def test_window_close_persists_evidence_and_records_it_on_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            run_dir = root / "runs" / "r1"
            run_dir.mkdir(parents=True)
            run = Run.create("feat", root / "p.md", None, run_dir, root)
            run.add_task("RDS-05")

            before = capture_snapshot(root, exclude_roots=(run_dir,))
            (root / "a.py").write_text("mutated by executor\n", encoding="utf-8")
            artifacts = launch_artifacts(run_dir, "RDS-05", 1)
            artifacts.directory.mkdir(parents=True)
            artifacts.executor_report.write_text("# report\n", encoding="utf-8")

            result = attribute_executor_window(
                before, root, artifacts=artifacts, task_id="RDS-05",
                allowed_scope=("a.py",), attempt=1, generation=1,
                executor_report=artifacts.executor_report, exclude_roots=(run_dir,),
            )

            self.assertEqual(result.state, STATE_KNOWN)
            self.assertEqual(result.changed_files[0]["path"], "a.py")

            run.record_executor_evidence(
                "RDS-05", attempt=1, generation=1,
                report_path=artifacts.executor_report,
            )
            run.record_implementation_attribution(
                "RDS-05", generation=1, attempt=1, attribution_state=result.state,
                manifest=result.manifest, diff=result.diff,
                changed_files=result.changed_files, reason=result.reason,
            )

            implementation = run.task("RDS-05").execution_evidence["implementation"]
            self.assertEqual(implementation["state"], STATE_KNOWN)
            self.assertTrue(
                implementation["manifest"].endswith("implementation-manifest-1.json"))
            self.assertEqual(run.task("RDS-05").changed_files, ["a.py"])


if __name__ == "__main__":
    unittest.main()
