"""Build the Standard Subagent Prompt Envelope for one executor launch.

Every field is filled verbatim from the normalized :class:`~schemas.contracts.TaskSpec` plus
the explicit repository anchors, the execution mode, the plan/task/kanban paths, the composed
role grant, and the report path the runner will read back. A fresh executor has no memory of
the orchestrating session, so nothing needed to execute the task is left implicit.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from schemas.contracts import TaskSpec

EXECUTOR_ROLE = "executor"


@dataclass(frozen=True)
class EnvelopeAnchors:
    """The repository anchors a subagent needs to resolve paths and load skills."""

    project_root: str
    agents_root: str
    kanban_path: str = "docs/kanban.md"


def _commands_block(spec: TaskSpec) -> str:
    if not spec.verification_commands:
        return "  none declared"
    return "\n".join(
        f"  - {command.cwd} -> {' '.join(command.argv)}"
        for command in spec.verification_commands
    )


def build_executor_envelope(
    spec: TaskSpec,
    *,
    anchors: EnvelopeAnchors,
    role_grant: Sequence[str],
    execution_mode: str,
    report_path: str,
    plan_path: str | None = None,
    repair_report_path: str | None = None,
) -> str:
    """Render the filled envelope. ``report_path`` is where the runner will read the report."""
    lines = [
        "Context:",
        f"- Project root: {anchors.project_root}",
        f"- Agents root: {anchors.agents_root}",
        f"- Required skills: {', '.join(spec.required_skills) or 'none declared'}",
        f"- Plan file: {plan_path or 'none'}",
        f"- Task file: {spec.path or 'none'}",
        f"- Kanban file: {anchors.kanban_path}",
        "",
        "Execution mode:",
        f"- {execution_mode or 'separate'}",
        "",
        "Task:",
        f"- Task ID: {spec.id}",
        f"- Task title: {spec.title or spec.id}",
        f"- Task type: {spec.task_type}",
        f"- Depends on: {', '.join(spec.depends_on) or 'none'}",
        f"- Allowed scope: {', '.join(spec.allowed_scope) or 'none'}",
        f"- Out of scope: {', '.join(spec.out_of_scope) or 'none'}",
        "- Verification commands:",
        _commands_block(spec),
        f"- Maximum repair attempts: {spec.max_repair_attempts}",
        "",
        "Role:",
        f"- {EXECUTOR_ROLE}",
        f"- Role grant: {', '.join(sorted(role_grant)) or 'none'}",
        f"- Report path: {report_path}",
        "",
        "Rules:",
        "- Read required skills first.",
        "- Read the task file before editing.",
        "- Write only inside Allowed scope; never inside Out of scope.",
        "- Run every verification command from its declared working directory, one at a time.",
        "- Report each command with its working directory and exit code. Never report a "
        "command you did not run.",
        "- Do not verify your own work and do not tick acceptance-criteria checkboxes.",
        "- Terminal state is `implemented`: stop after reporting. The orchestrator dispatches "
        "verification and moves the board.",
    ]
    if repair_report_path:
        lines += [
            "",
            f"Repair of: {repair_report_path}",
            "- Fix only the findings listed there. Preserve the original scope above.",
        ]
    lines += [
        "",
        "Final report:",
        "- Status: implemented | blocked",
        "- Files changed:",
        "- Validation:",
        "- Acceptance criteria evidence:",
        "- Out-of-scope discoveries:",
        "- Blockers:",
    ]
    return "\n".join(lines) + "\n"
