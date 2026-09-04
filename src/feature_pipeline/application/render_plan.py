"""Render the deterministic dry-run plan (C1–C8) from one ``CompiledRunPlan`` (CP-02).

The dry-run preview used to resolve each task's route, checks, subagents, root and storage
inside ``pipeline_core.runner_cli._plan_text`` — a second, hand-written copy of the
resolution the execute path did differently (finding F-01, ADR 006). CP-02 makes the preview
read those fields off the single :class:`~feature_pipeline.domain.plan.CompiledRunPlan` the
execute path also consumes; this module is the renderer.

It is pure text assembly: no filesystem, no process, no exit-code policy. The caller supplies
the already-resolved gate booleans, the pre-computed generic stage lines (the profile's
non-executing scaffold argv, which the compiled plan does not model), the chosen exit code,
and — for ``--mode release-dry-run`` — the post-task lifecycle block. Host-path redaction is
also the caller's (it owns ``pipeline_core.redaction``).

Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from feature_pipeline.domain.plan import CompiledRunPlan
from feature_pipeline.inputs.profile import CompiledProfile

#: The fixed delivery-gate order, byte-identical to
#: ``pipeline_core.runner_cli.DELIVERY_GATES``.
DELIVERY_GATES = ("plan", "final-diff", "commit", "verification-verdict")

_PUSH_DENIED_MESSAGE = "push is denied at this stage and the runner has no push code path."
_EXIT_MEANINGS = {
    0: "ok",
    10: "a delivery gate is pending",
    20: "blocked",
    30: "error",
}

__all__ = ["DELIVERY_GATES", "render_dry_run"]


def _tok(rel: str) -> str:
    """A resolved logical path as its redacted ``<project_root>`` token."""
    return "<project_root>" if rel in ("", ".") else f"<project_root>/{rel}"


def render_dry_run(
    *,
    plan: CompiledRunPlan | None,
    profile: CompiledProfile | None,
    selected_ids: Sequence[str],
    task_type_by_id: Mapping[str, str],
    route_reason_by_id: Mapping[str, str],
    mode: str,
    profile_name: str,
    profile_rel: str,
    project_skill_rel: str | None,
    stage_lines: Sequence[str],
    dep_blocked: Mapping[str, Sequence[str]],
    lease_blocked: bool,
    approve_plan: bool,
    approve_final_diff: bool,
    commit_approved_manifest: bool,
    commit_requested: bool,
    exit_code: int,
    post_task: bool = False,
    post_task_lines: Sequence[str] = (),
    post_task_transition_line: str | None = None,
) -> str:
    """Assemble the C1–C8 dry-run plan text (no redaction — the caller applies it)."""
    lines: list[str] = []
    lines.append("feature-pipeline core runner - dry run")
    lines.append(f"mode: {mode}")
    lines.append(f"profile: {profile_name}")
    lines.append("anchors:")
    lines.append("  project-root: <project_root>")
    lines.append("  agents-root: <agents_root>")
    lines.append("  core-root: <core_root>")
    lines.append(f"  profile: <project_root>/{profile_rel}")
    lines.append(
        f"  project-skill: <agents_root>/{project_skill_rel}"
        if project_skill_rel else "  project-skill: (none)"
    )
    lines.append("")

    lines.append("C1. Selected tasks:")
    for index, task_id in enumerate(selected_ids, start=1):
        lines.append(f"  {index}. {task_id}")
    lines.append("")

    def _route_view(task_id: str) -> tuple[str, str, str, tuple[tuple[str, tuple[str, ...]], ...], tuple[str, ...]]:
        """``(stack, working_root, storage_root, ((check_name, argv), …), subagents)`` for a
        routable task — from the compiled plan when it exists, otherwise straight off the
        compiled profile (a selection with an unroutable sibling has no whole-plan object)."""
        task_type = task_type_by_id[task_id]
        assert profile is not None  # a routable task always has a typed profile
        route = profile.route_for(task_type)
        if plan is not None:
            resolved = plan.task(task_id)
            checks = tuple((c.name, tuple(c.argv)) for c in resolved.checks)
            return (route.stack, str(resolved.working_root), str(resolved.storage_root),
                    checks, tuple(route.subagents))
        checks = tuple(
            (name, tuple(profile.check_command(name).argv)) for name in route.check_names
        )
        return (route.stack, str(route.working_root),
                str(profile.storage_root(route.storage_key)), checks,
                tuple(route.subagents))

    lines.append("C2. Routing:")
    for task_id in selected_ids:
        reason = route_reason_by_id.get(task_id)
        if reason is not None:
            lines.append(f"  {task_id}: {reason} (type {task_type_by_id[task_id]})")
            continue
        stack, working_root, storage_root, checks, subagents = _route_view(task_id)
        lines.append(
            f"  {task_id}: stack={stack} "
            f"root={_tok(working_root)} "
            f"storage={_tok(storage_root)} "
            f"checks=[{','.join(name for name, _ in checks)}] "
            f"subagents=[{','.join(subagents)}]"
        )
    lines.append("")

    lines.append("C3. Generated argv (shell-free):")
    for task_id in selected_ids:
        lines.append(f"  {task_id}:")
        for stage_line in stage_lines:
            lines.append(f"    {stage_line}")
        if route_reason_by_id.get(task_id) is None:
            for name, argv in _route_view(task_id)[3]:
                lines.append(f"    check {name}: {' '.join(argv)}")
    lines.append("")

    lines.append("C4. Pending delivery gates (in order):")
    approved = {
        "plan": approve_plan,
        "final-diff": approve_final_diff,
        "commit": commit_approved_manifest,
        "verification-verdict": False,
    }
    earlier_satisfied = True
    for position, gate in enumerate(DELIVERY_GATES, start=1):
        satisfied = earlier_satisfied and approved.get(gate, False)
        earlier_satisfied = satisfied
        state = "satisfied" if satisfied else "pending"
        note = " (requested)" if gate == "commit" and commit_requested else ""
        lines.append(f"  {position}. {gate} - {state}{note}")
    if post_task:
        lines.extend(post_task_lines)
    lines.append("")

    lines.append(
        f"C5. Exit code: {exit_code} "
        f"({_EXIT_MEANINGS.get(exit_code, 'see safety posture')})"
    )
    lines.append("")

    lines.append("C6. Planned state transitions:")
    for task_id in selected_ids:
        reason = route_reason_by_id.get(task_id)
        if task_id in dep_blocked:
            deps = ",".join(dep_blocked[task_id])
            lines.append(
                f"  {task_id}: pending -> blocked (dependency-not-satisfied: {deps})")
        elif lease_blocked:
            lines.append(f"  {task_id}: pending -> blocked (lease-held)")
        elif reason is not None:
            lines.append(f"  {task_id}: pending -> blocked ({reason})")
        else:
            lines.append(
                f"  {task_id}: pending -> ready -> running -> implemented -> verified")
    if post_task and post_task_transition_line is not None:
        lines.append(post_task_transition_line)
    lines.append("")

    lines.append("C7. Redacted evidence manifest:")
    storage = None
    if profile is not None:
        for task_id in selected_ids:
            if route_reason_by_id.get(task_id) is None:
                storage = _tok(_route_view(task_id)[2])
                break
    storage = storage or "<project_root>/.pipeline/runs"
    lines.append(f"  {storage}/<feature>/run.json")
    lines.append("")

    lines.append("C8. Safety posture:")
    lines.append(
        f'  push: denied before any artifact ("{_PUSH_DENIED_MESSAGE}") [exit 1]')
    lines.append(
        "  commit: never bypasses the plan gate; this runner has no commit code path")
    lines.append(
        "  logical paths: absolute, ~, .. and \\ separators are rejected")
    lines.append("  unknown task type: fail closed (unknown-task-type)")
    lines.append("  design / no registry route: fail closed (unresolved-executor)")
    lines.append("  verifier roles: read-only, enforced at profile load")
    lines.append("  unmet dependency: fail closed (dependency-not-satisfied)")
    if post_task:
        lines.append(
            "  release stage: dry-run plan only; a commit needs the commit control, an "
            "approved final diff, and a passing final verification, and even then no commit "
            "or push subprocess exists")

    return "\n".join(lines) + "\n"
