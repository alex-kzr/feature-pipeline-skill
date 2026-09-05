"""KLC-02 — the deterministic Markdown task lifecycle projection.

Plan: ``docs/plans/2026-09-05-kanban-lifecycle-results.md`` (Phase 2),
``docs/plans/tasks/KLC-02_markdown-task-projection.md``.

Three things are pinned here:

* **AC-1** — starting a task moves exactly one card from ``To Do`` to ``In Progress`` and
  marks only ``In Progress`` in the task's ``## Status`` block.
* **AC-2** — projecting ``verified`` removes the card from the active board, marks the task
  ``Done``, and writes exactly one complete ``## Result`` section with repository-relative
  evidence.
* **AC-3** — replaying any supported durable state is idempotent, and malformed board/task
  input fails without losing unrelated content.

Standard library only.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from feature_pipeline.infrastructure.board_projection import (
    CommandEvidence,
    CompletionEvidence,
    DuplicateCardError,
    InvalidEvidenceError,
    MalformedBoardError,
    MalformedTaskError,
    MissingCardError,
    project_task_state,
)

from tests.support.fixtures import temp_root

BOARD = """# Kanban Board

## To Do

- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)
- [ABC-02: Do another thing](plans/tasks/ABC-02_do-another-thing.md)

## In Progress
"""

TASK = """# ABC-01 - Do the thing

Plan — [example.md](../example.md)

## Status
- [ ] To Do
- [ ] In Progress
- [ ] Done

## Execution Metadata
- Type: python
"""


def _write_bytes_exact(path: Path, text: str) -> Path:
    """Write ``text`` verbatim as UTF-8 bytes, bypassing universal-newline translation.

    ``Path.write_text`` (used by ``tests.support.fixtures.write_file``) translates every
    ``\\n`` to ``os.linesep`` on write, which would silently turn a plain-LF fixture into
    CRLF on Windows and double a fixture that already uses CRLF. This module's tests care
    about exact line endings, so fixtures are written byte-for-byte instead.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _evidence(**overrides: object) -> CompletionEvidence:
    fields: dict[str, object] = dict(
        completed_at="2026-09-05T00:00:00Z",
        run_id="run-123",
        outcome="verified",
        repair_count=0,
        gate_count=0,
        task_verdict="PASS",
        test_verdict="PASS",
        commands=(CommandEvidence(cwd=".", command="pytest", exit_code=0),),
        evidence_paths=("docs/acceptance/artifacts/run-123.md",),
    )
    fields.update(overrides)
    return CompletionEvidence(**fields)  # type: ignore[arg-type]


class StartTransitionTests(unittest.TestCase):
    """AC-1."""

    def test_start_moves_card_and_checks_only_in_progress(self) -> None:
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", BOARD)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="running",
            )

            board_text = board.read_text(encoding="utf-8")
            task_text = task.read_text(encoding="utf-8")

            self.assertNotIn(
                "[ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)",
                board_text.split("## In Progress")[0],
            )
            self.assertIn(
                "[ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)",
                board_text.split("## In Progress")[1],
            )
            # The unrelated card is untouched and stays in To Do.
            self.assertIn(
                "[ABC-02: Do another thing](plans/tasks/ABC-02_do-another-thing.md)",
                board_text.split("## In Progress")[0],
            )

            self.assertIn("- [ ] To Do\n", task_text)
            self.assertIn("- [x] In Progress\n", task_text)
            self.assertIn("- [ ] Done\n", task_text)

    def test_blocked_returns_card_to_to_do_and_preserves_blockers(self) -> None:
        task_with_blockers = TASK.replace(
            "## Execution Metadata",
            "## Blockers\n\nRecorded 2026-09-05: waiting on ABC-00.\n\n## Execution Metadata",
        )
        board_in_progress = BOARD.replace(
            "- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)\n", ""
        ).replace(
            "## In Progress\n",
            "## In Progress\n\n- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)\n",
        )
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", board_in_progress)
            task = _write_bytes_exact(
                root / "docs/plans/tasks/ABC-01_do-the-thing.md", task_with_blockers
            )

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="blocked",
            )

            board_text = board.read_text(encoding="utf-8")
            task_text = task.read_text(encoding="utf-8")

            self.assertIn(
                "[ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)",
                board_text.split("## In Progress")[0],
            )
            self.assertNotIn(
                "[ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)",
                board_text.split("## In Progress")[1],
            )
            self.assertIn("- [x] To Do\n", task_text)
            self.assertIn("Recorded 2026-09-05: waiting on ABC-00.", task_text)


class VerifiedTransitionTests(unittest.TestCase):
    """AC-2."""

    def test_verified_removes_card_marks_done_and_writes_result(self) -> None:
        board_in_progress = BOARD.replace(
            "- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)\n", ""
        ).replace(
            "## In Progress\n",
            "## In Progress\n\n- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)\n",
        )
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", board_in_progress)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="verified",
                evidence=_evidence(),
            )

            board_text = board.read_text(encoding="utf-8")
            task_text = task.read_text(encoding="utf-8")

            self.assertNotIn("ABC-01", board_text)
            self.assertIn(
                "[ABC-02: Do another thing](plans/tasks/ABC-02_do-another-thing.md)",
                board_text,
            )

            self.assertIn("- [x] Done\n", task_text)
            self.assertEqual(task_text.count("## Result"), 1)
            self.assertIn("run-123", task_text)
            self.assertIn("outcome: **verified**", task_text)
            self.assertIn("Repairs: 0", task_text)
            self.assertIn("Task verifier verdict: PASS", task_text)
            self.assertIn("`. -> pytest` — exit 0", task_text)
            self.assertIn("docs/acceptance/artifacts/run-123.md", task_text)
            self.assertIn("## Execution Metadata", task_text)

    def test_verified_requires_evidence(self) -> None:
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", BOARD)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)
            with self.assertRaises(InvalidEvidenceError):
                project_task_state(
                    board_path=board,
                    task_path=task,
                    task_id="ABC-01",
                    task_title="Do the thing",
                    state="verified",
                )

    def test_evidence_rejects_absolute_paths(self) -> None:
        with self.assertRaises(InvalidEvidenceError):
            _evidence(evidence_paths=("C:/Users/me/report.md",))
        with self.assertRaises(InvalidEvidenceError):
            _evidence(evidence_paths=("/etc/passwd",))


class IdempotentReplayTests(unittest.TestCase):
    """AC-3."""

    def test_replaying_running_is_byte_identical(self) -> None:
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", BOARD)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="running",
            )
            first_board = board.read_bytes()
            first_task = task.read_bytes()

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="running",
            )

            self.assertEqual(board.read_bytes(), first_board)
            self.assertEqual(task.read_bytes(), first_task)

    def test_replaying_verified_is_byte_identical(self) -> None:
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", BOARD)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="verified",
                evidence=_evidence(),
            )
            first_board = board.read_bytes()
            first_task = task.read_bytes()

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="verified",
                evidence=_evidence(),
            )

            self.assertEqual(board.read_bytes(), first_board)
            self.assertEqual(task.read_bytes(), first_task)

    def test_missing_board_heading_fails_without_writing(self) -> None:
        malformed_board = BOARD.replace("## In Progress\n", "## Later\n")
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", malformed_board)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            with self.assertRaises(MalformedBoardError):
                project_task_state(
                    board_path=board,
                    task_path=task,
                    task_id="ABC-01",
                    task_title="Do the thing",
                    state="running",
                )

            self.assertEqual(board.read_text(encoding="utf-8"), malformed_board)
            self.assertEqual(task.read_text(encoding="utf-8"), TASK)

    def test_missing_task_status_block_fails_without_writing(self) -> None:
        malformed_task = TASK.replace("## Status\n", "## Standing\n")
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", BOARD)
            task = _write_bytes_exact(
                root / "docs/plans/tasks/ABC-01_do-the-thing.md", malformed_task
            )

            with self.assertRaises(MalformedTaskError):
                project_task_state(
                    board_path=board,
                    task_path=task,
                    task_id="ABC-01",
                    task_title="Do the thing",
                    state="running",
                )

            self.assertEqual(board.read_text(encoding="utf-8"), BOARD)
            self.assertEqual(task.read_text(encoding="utf-8"), malformed_task)

    def test_missing_card_fails_when_transition_requires_one(self) -> None:
        board_without_card = BOARD.replace(
            "- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)\n", ""
        )
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", board_without_card)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            with self.assertRaises(MissingCardError):
                project_task_state(
                    board_path=board,
                    task_path=task,
                    task_id="ABC-01",
                    task_title="Do the thing",
                    state="running",
                )

            self.assertEqual(board.read_text(encoding="utf-8"), board_without_card)
            self.assertEqual(task.read_text(encoding="utf-8"), TASK)

    def test_duplicate_card_fails_without_writing(self) -> None:
        duplicated_board = BOARD.replace(
            "## In Progress\n",
            "## In Progress\n\n- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)\n",
        )
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", duplicated_board)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            with self.assertRaises(DuplicateCardError):
                project_task_state(
                    board_path=board,
                    task_path=task,
                    task_id="ABC-01",
                    task_title="Do the thing",
                    state="running",
                )

            self.assertEqual(board.read_text(encoding="utf-8"), duplicated_board)
            self.assertEqual(task.read_text(encoding="utf-8"), TASK)

    def test_verified_replay_when_already_removed_is_a_no_op(self) -> None:
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", BOARD)
            task = _write_bytes_exact(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="verified",
                evidence=_evidence(),
            )
            first_board = board.read_bytes()

            # Replaying verified again with the card already absent must not fail or
            # reintroduce it.
            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="verified",
                evidence=_evidence(),
            )

            self.assertEqual(board.read_bytes(), first_board)


class CrlfPreservationTests(unittest.TestCase):
    def test_crlf_board_and_task_stay_crlf(self) -> None:
        crlf_board = BOARD.replace("\n", "\r\n")
        crlf_task = TASK.replace("\n", "\r\n")
        with temp_root() as root:
            board = _write_bytes_exact(root / "docs/kanban.md", crlf_board)
            task = _write_bytes_exact(
                root / "docs/plans/tasks/ABC-01_do-the-thing.md", crlf_task
            )

            project_task_state(
                board_path=board,
                task_path=task,
                task_id="ABC-01",
                task_title="Do the thing",
                state="running",
            )

            board_bytes = board.read_bytes()
            task_bytes = task.read_bytes()
            self.assertNotIn(b"\r\r\n", board_bytes)
            self.assertIn(b"\r\n", board_bytes)
            self.assertNotIn(b"\r\r\n", task_bytes)
            self.assertIn(b"\r\n", task_bytes)


if __name__ == "__main__":
    unittest.main()
