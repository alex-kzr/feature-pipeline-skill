"""Pure text rendering for the CLI boundary.

Every function here turns already-computed data into the exact printed text; none of them
resolve a path, read a profile, touch a lease, or otherwise decide anything a use case in
:mod:`feature_pipeline.cli.use_cases` has not already decided. ``--dry-run``'s C1-C8 plan
itself is rendered by :func:`feature_pipeline.application.render_plan.render_dry_run`
(the application-layer renderer BL-01 introduced); this module covers the remaining
CLI-only text: the recorded-run ``--status`` report, the post-task lifecycle preview lines,
and the non-dry-run gate summary.

Standard library only.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.post_task import POST_TASK_STAGES
from pipeline_core.redaction import build_rules, redact_text
from pipeline_core.release import ReleasePolicy
from pipeline_core.state import Run, StateError

from .commands import RunCommand
from .parser import EXIT_BLOCKED


def status_text(run_dir: Path, project_dir: Path, feature: str) -> str:
    """Render the recorded run state for ``--status``; redacted, host-path-free."""
    try:
        run = Run.load(run_dir, project_dir)
    except StateError:
        return redact_text(f"feature: {feature}\nno recorded run\n", build_rules())
    lines = [f"feature: {run.feature}", f"status: {run.status}", "tasks:"]
    for record in run.tasks.values():
        lines.append(f"  {record.id}: {record.status}")
    return redact_text("\n".join(lines) + "\n", build_rules())


def post_task_gate_lines(
    *,
    release_policy: ReleasePolicy | None,
    wrapper_dir: str | None,
    output_count: int,
    approve_plan: bool,
    approve_final_diff: bool,
    commit_requested: bool,
) -> list[str]:
    """Render the stage 10-16 post-task lifecycle for the C4 plan.

    Every work stage is ``planned`` (a plan can never run it); the human final-diff gate is
    ``satisfied`` only when the plan gate is also approved; and the release stage is *always*
    a dry run here — a plan can never assert a passing final verification, so ``would_commit``
    can never be true. This mirrors :func:`pipeline_core.release.release_plan_result` without
    any commit or push code path.
    """
    fd_ok = approve_plan and approve_final_diff
    out: list[str] = ["  post-task lifecycle (stages 10-16, in order):"]
    for number, name in POST_TASK_STAGES:
        if number == 12:
            suffix = (f" (wrapper: bash {wrapper_dir}/build.sh)" if wrapper_dir else "")
            out.append(f"  {number}. {name} - planned{suffix}")
        elif number == 13:
            out.append(
                f"  {number}. {name} - planned ({output_count} configured output(s) re-checked)")
        elif number == 14:
            checks = "; ".join(
                " ".join(command.argv) for command in
                (release_policy.final_verification if release_policy else ()))
            out.append(f"  {number}. {name} - planned (checks: {checks})")
        elif number == 15:
            out.append(f"  {number}. {name} - {'satisfied' if fd_ok else 'pending'}")
        elif number == 16:
            blockers: list[str] = []
            if not commit_requested:
                blockers.append("no explicit commit control was given")
            if not fd_ok:
                blockers.append("the final diff is not approved")
            blockers.append("final verification has not run in a plan")
            out.append(f"  {number}. {name} - dry-run")
            if release_policy is not None:
                out.append(f"     stage paths: {', '.join(release_policy.stage_paths)}")
                if release_policy.submodule_pointer:
                    out.append(f"     submodule pointer: {release_policy.submodule_pointer}")
                out.append(
                    f"     proposed commit message: {release_policy.commit_message_template}")
            out.append(f"     reasons release stays a dry run: {'; '.join(blockers)}")
            out.append(
                "     commit: the runner has no commit code path - nothing is executed even "
                "when authorized")
            out.append("     push: never - the runner has no push code path")
        else:
            out.append(f"  {number}. {name} - planned")
    return out


def non_dry_run_text(
    command: RunCommand, exit_code: int, dep_blocked: dict, lease_blocked: bool,
    unresolved: bool,
) -> str:
    if dep_blocked:
        return "blocked: a selected task has an unsatisfied dependency (dependency-not-satisfied)\n"
    if lease_blocked:
        return "blocked: a live run lease is held for this feature (lease-held)\n"
    if unresolved:
        return "error: a selected task has no registry route (unresolved-executor)\n"
    if command.resume and exit_code == EXIT_BLOCKED:
        return "blocked: no matching recorded run to resume\n"
    if command.approve_plan:
        return ("plan gate satisfied; commit gate pending - this runner has no commit or push "
                "code path, so the run stops here.\n")
    return ("plan gate pending - approve with --approve-plan. No delivery gate is auto-passed; "
            "--commit does not bypass this gate.\n")


__all__ = ["status_text", "post_task_gate_lines", "non_dry_run_text"]
