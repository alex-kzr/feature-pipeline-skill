"""Write an actionable diagnostic before any failure transition changes task state.

A ``diagnostic-report.md`` carries the five things needed to act on a failure without
re-running anything: the current command output, the recent commands, ``git diff``,
``git status``, and the current task state. It is assembled from the least-controlled inputs
the runner holds (captured output, ``git`` porcelain), so the finished document is redacted
as a whole and the two truncated inputs are additionally redacted before their byte budget.

:func:`write_diagnostic_report` is the *renderer*. Every production failure path now reaches
it through the one boundary that first collects ``git diff`` / ``git status`` at the moment
of failure —
:class:`feature_pipeline.application.diagnostic_service.DiagnosticService` — so an
uncollected section is never rendered as an empty successful one (BL-02 finding F-05). A
direct call with no ``git_diff`` / ``git_status`` is still supported for tests and golden
capture.

Standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import write_text_atomic
from .commands import DIAGNOSTIC_OUTPUT_BUDGET, redact_then_truncate
from .reporting import Section, fenced, render_report
from .state import Run

DIAGNOSTIC_REPORT_NAME = "diagnostic-report.md"

#: Section order is a contract: current output first, task state last.
SECTION_ORDER = (
    "Current output",
    "Recent commands",
    "git diff",
    "git status",
    "Current task state",
)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recent_commands(run: Run, task_id: str, limit: int = 5) -> list[dict[str, Any]]:
    scoped = [c for c in run.commands if task_id and task_id in str(c.get("stage", ""))]
    return (scoped or list(run.commands))[-limit:]


def _stream_text(run: Run, reference: Any) -> str:
    if not isinstance(reference, str):
        return ""
    path = Path(run.run_dir) / reference
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_diagnostic_report(
    run: Run,
    *,
    task_id: str,
    attempt: int,
    git_diff: str = "",
    git_status: str = "",
    note: str | None = None,
    reports_dir: str | Path | None = None,
) -> Path:
    """Assemble and atomically persist the diagnostic; return its path."""
    base = Path(reports_dir) if reports_dir is not None else (
        Path(run.run_dir) / "reports" / str(task_id) / f"attempt-{attempt}")
    path = base / DIAGNOSTIC_REPORT_NAME

    recent = _recent_commands(run, task_id)
    task_state = run.tasks[task_id].status if task_id in run.tasks else run.status

    if recent:
        last = recent[-1]
        captured = (_stream_text(run, last.get("stdout_log"))
                    + _stream_text(run, last.get("stderr_log")))
        if not captured:
            captured = f"{last.get('stdout', '')}{last.get('stderr', '')}"
        current_output = redact_then_truncate(captured, DIAGNOSTIC_OUTPUT_BUDGET, run.repo_root)
    else:
        current_output = ""

    if recent:
        command_lines = "\n".join(
            f"- `{' '.join(c.get('argv', []))}` in `{c.get('cwd', '.')}` -> exit "
            f"`{c.get('exit_code')}` ({round(float(c.get('duration', 0.0)), 3)}s)"
            for c in recent)
    else:
        command_lines = "none recorded"

    header = "\n".join([
        f"- Run: `{run.run_id}`",
        f"- Task: `{task_id}`",
        f"- Task state: `{task_state}`",
        f"- Run state: `{run.status}`",
        f"- Attempt: `{attempt}`",
        f"- Written: {_now()}",
    ])
    if note:
        header += f"\n- Note: {note}"

    sections = [
        Section("Summary", header),
        Section("Current output", fenced(current_output)),
        Section("Recent commands", command_lines),
        Section("git diff", fenced(
            redact_then_truncate(git_diff, DIAGNOSTIC_OUTPUT_BUDGET, run.repo_root), "diff")),
        Section("git status", fenced(git_status)),
        Section("Current task state", f"`{task_id}` is `{task_state}` (run `{run.status}`)."),
    ]
    document = render_report(f"Diagnostic — {task_id} (attempt {attempt})", sections)
    written = write_text_atomic(path, document, repo_root=run.repo_root)

    try:
        run.artifacts[f"diagnostic:{task_id}:{attempt}"] = (
            written.resolve().relative_to(Path(run.run_dir).resolve()).as_posix())
    except ValueError:
        run.artifacts[f"diagnostic:{task_id}:{attempt}"] = written.as_posix()
    return written
