"""Status, dry-run, and execute: the CLI's three use cases.

Each of :func:`run_status`, :func:`run_dry_run`, and :func:`run_execute` is reached only
through :func:`dispatch`, takes a typed :class:`~feature_pipeline.cli.commands.RunCommand`,
and returns a typed :class:`~feature_pipeline.application.results.PipelineResult` — a stable
outcome plus its already-rendered, redacted message. None of them touch argparse, and none
render anything themselves beyond delegating to
:mod:`feature_pipeline.application.render_plan` (the C1-C8 dry-run plan) or
:mod:`feature_pipeline.cli.renderers` (the CLI-only text): rendering is a pure function of
already-computed data, never a side effect a use case performs inline.

Selection, routing, gate, and lifecycle decisions all live here — never in
:mod:`pipeline_core.runner_cli`, which only parses argv and prints the result — so an
argparse-facing module never re-decides what a use case has already decided. No use case
mutates domain state or launches a process itself: the one execute code path delegates to
:func:`pipeline_core.execution.execute_run`, which owns dispatch, verification, and repair.

Standard library only.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from feature_pipeline.bootstrap import (
    Anchors,
    MarkdownPlanError,
    POST_TASK_STAGES,
    ReleasePolicy,
    Run,
    StateError,
    build_bootstrap,
    build_rules,
    load_markdown_plan,
    load_release_policy,
    load_runnable_profile,
    load_tool_integration,
    make_execute_adapters,
    pid_alive,
    plan_release_dry_run,
    read_lease,
    redact_text,
    resolve_attested_dependency_ids,
    run_execute as bootstrap_run_execute,
)
from feature_pipeline.application.compile_plan import (
    ControlOverrides,
    ShallowTaskInput,
    compile_run_plan,
)
from feature_pipeline.application.profile_bridge import (
    compiled_profile_from_core,
    route_reasons,
)
from feature_pipeline.application.render_plan import render_dry_run
from feature_pipeline.application.results import Outcome, PipelineResult
from feature_pipeline.application.selection import SelectionError, resolve_selection
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.graph import TaskGraph
from schemas import SchemaError, validate_relative_path

from .commands import RunCommand
from .errors import CliError
from .parser import (
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_GATE_PENDING,
    EXIT_OK,
    EXIT_PUSH_DENIED,
    FEATURE_RE,
    POST_TASK_MODE,
)
from .renderers import non_dry_run_text, post_task_gate_lines, status_text

#: The one outcome-to-exit-code translation a use case needs going the other way: an already
#: computed legacy exit code (the frozen ``EXIT_*`` table) becomes the typed
#: :class:`~feature_pipeline.application.results.Outcome` every ``PipelineResult`` carries.
_OUTCOME_BY_EXIT_CODE = {
    EXIT_OK: Outcome.OK,
    EXIT_GATE_PENDING: Outcome.GATE_PENDING,
    EXIT_BLOCKED: Outcome.BLOCKED,
    EXIT_ERROR: Outcome.ERROR,
    EXIT_PUSH_DENIED: Outcome.PUSH_DENIED,
}


def _result(text: str, exit_code: int) -> PipelineResult:
    return PipelineResult(_OUTCOME_BY_EXIT_CODE[exit_code], text)


def _require(value: str | None, field: str) -> str:
    if not value:
        raise CliError(EXIT_ERROR, f"{field} is required")
    return value


def _logical_relative(value: str, field: str) -> str:
    """Normalize an anchor-relative logical path, failing closed on an unsafe shape."""
    if "\\" in value:
        raise CliError(EXIT_ERROR, f"{field} must not contain '\\' path separators")
    try:
        return validate_relative_path(value, field)
    except SchemaError as exc:
        raise CliError(EXIT_ERROR, str(exc)) from None


def _resolve_under(anchor: Path, relative: str, field: str) -> Path:
    root = anchor.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise CliError(EXIT_ERROR, f"{field} escapes its explicit anchor") from None
    return candidate


def _load_plan(path: Path) -> tuple[str | None, list[dict]]:
    # A Markdown plan (a common task-board plan format) is read by the compatibility
    # reader; everything else is parsed as JSON. Downstream construction (selection,
    # routing, gates, exit codes) is identical for both.
    if path.suffix.lower() == ".md":
        try:
            return load_markdown_plan(path)
        except MarkdownPlanError as exc:
            raise CliError(EXIT_ERROR, str(exc)) from None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "plan file not found") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "plan file is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise CliError(EXIT_ERROR, "plan must be an object with a 'tasks' list")
    tasks: list[dict] = []
    seen: set[str] = set()
    for entry in data["tasks"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("type"):
            raise CliError(EXIT_ERROR, "every plan task needs an 'id' and a 'type'")
        tid = str(entry["id"])
        if tid in seen:
            raise CliError(EXIT_ERROR, f"duplicate task id in plan: {tid}")
        seen.add(tid)
        depends_on = entry.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise CliError(EXIT_ERROR, f"{tid}: depends_on must be a list")
        tasks.append({"id": tid, "type": str(entry["type"]),
                      "depends_on": [str(d) for d in depends_on]})
    if not tasks:
        raise CliError(EXIT_ERROR, "plan has no tasks")
    return (str(data["feature"]) if data.get("feature") else None), tasks


def run_command(command: RunCommand) -> PipelineResult:
    """Resolve anchors and profile, then dispatch to status, dry-run, or execute."""
    project_root = Path(_require(command.project_root, "--project-root"))
    agents_root = Path(_require(command.agents_root, "--agents-root"))
    core_root = Path(_require(command.core_root, "--core-root"))
    anchors = Anchors(project_root, agents_root, core_root)

    # ``release-dry-run`` is a plan-only preview by construction: it never runs an executor
    # and never writes, so it always implies ``--dry-run``.
    post_task = command.mode == POST_TASK_MODE
    if post_task:
        command.dry_run = True

    profile_rel = _logical_relative(_require(command.profile, "--profile"), "--profile")
    profile_path = _resolve_under(project_root, profile_rel, "--profile")
    project_skill_rel = None
    if command.project_skill:
        project_skill_rel = _logical_relative(command.project_skill, "--project-skill")
        _resolve_under(agents_root, project_skill_rel, "--project-skill")

    try:
        profile = load_runnable_profile(profile_path)
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "profile file not found") from None
    except SchemaError as exc:
        raise CliError(EXIT_ERROR, f"profile is invalid: {exc}") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "profile file is not valid JSON") from None

    # ``release-dry-run`` consumes the project's post-task contract as-is: the tool-integration
    # block and the release policy declared beside the profile. Loading is fail-closed.
    release_policy: ReleasePolicy | None = None
    wrapper_dir: str | None = None
    output_count = 0
    if post_task:
        config_dir = profile_path.parent
        try:
            integration = load_tool_integration(config_dir / "integrations.json")
            release_policy = load_release_policy(config_dir / "release.json")
        except FileNotFoundError:
            raise CliError(
                EXIT_ERROR,
                "release-dry-run needs an integrations.json and a release.json beside the "
                "profile") from None
        except SchemaError as exc:
            raise CliError(EXIT_ERROR, f"post-task config is invalid: {exc}") from None
        except json.JSONDecodeError:
            raise CliError(EXIT_ERROR, "post-task config is not valid JSON") from None
        wrapper_dir = integration.wrapper_dir
        output_count = len(integration.expected_outputs)

    project_dir = _resolve_under(project_root, profile.logical_paths.project, "project path")
    composition = build_bootstrap(project_dir, agents_root, core_root)
    plan_rel = _logical_relative(_require(command.plan, "--plan"), "--plan")
    plan_path = _resolve_under(project_dir, plan_rel, "--plan")
    prompt_rel = _logical_relative(command.prompt, "--prompt") if command.prompt else plan_rel
    _resolve_under(project_dir, prompt_rel, "--prompt")

    if command.mode == "execute" and not command.dry_run:
        return run_execute(command, anchors, agents_root, project_dir, profile, plan_path,
                            prompt_rel)

    plan_feature, plan_tasks = _load_plan(plan_path)
    feature = command.feature or plan_feature
    if not feature or not FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    verified: set[str] = set()  # a fresh run has verified nothing
    type_by_id = {t["id"]: t["type"] for t in plan_tasks}
    shallow_inputs = [
        ShallowTaskInput(t["id"], t["type"], tuple(t["depends_on"])) for t in plan_tasks
    ]

    # One selection for the whole preview, through the same domain entry point the execute
    # path uses. The graph also fails closed here on a duplicate id, an unknown dependency
    # edge, a self-edge, or a cycle.
    try:
        selection = resolve_selection(
            TaskGraph.from_pairs((t["id"], t["depends_on"]) for t in plan_tasks),
            task=command.task,
            through=command.through,
        )
    except (SelectionError, DomainError) as exc:
        raise CliError(EXIT_ERROR, str(exc)) from None
    selected_ids = list(selection.task_ids)

    # A task type with no registry route is classified here (byte-identical reasons); the
    # rest of the selection is routed once, by the compiler, into the plan the execute path
    # also consumes.
    reason_by_type = route_reasons(profile, (type_by_id[i] for i in selected_ids))
    route_reason_by_id = {
        i: reason_by_type[type_by_id[i]]
        for i in selected_ids
        if type_by_id[i] in reason_by_type
    }

    # The typed profile is available whenever the native profile has a registry; a routable
    # task's C2/C3 line is rendered straight off it even when a sibling type is unroutable.
    compiled_profile = (
        compiled_profile_from_core(profile) if profile.registry is not None else None
    )
    compiled_plan = None
    if not route_reason_by_id and compiled_profile is not None:
        try:
            compiled_plan = compile_run_plan(
                feature=feature,
                definitions=shallow_inputs,
                profile=compiled_profile,
                overrides=ControlOverrides(max_repair_attempts=command.max_repair_attempts),
                adapters=composition.adapter_registry,
                task=command.task,
                through=command.through,
                allow_unavailable_adapter=True,
            )
        except DomainError as exc:
            raise CliError(EXIT_ERROR, str(exc)) from None

    storage_rel = None
    if compiled_plan is not None:
        for task_id in selected_ids:
            if task_id not in route_reason_by_id:
                storage_rel = str(compiled_plan.task(task_id).storage_root)
                break
    lease_dir = (
        (project_dir / storage_rel) if storage_rel else project_dir / ".pipeline" / "runs"
    ) / feature

    # `--dry-run` mirrors execute's attestation handling read-only: same syntax/scope
    # checks, and the same source-run resolution, so a preview never claims success for an
    # attestation the real run would refuse (or silently ignores a bad one).
    attested_ids: set[str] = set()
    if command.dry_run:
        attested_ids = resolve_attested_dependency_ids(
            raw_attestations=command.attest_dependency,
            selected_task=command.task,
            plan_tasks=plan_tasks,
            run_dir=lease_dir,
            repo_root=project_dir,
            prompt_path=project_dir / prompt_rel,
            plan_path=plan_path,
        )

    dep_blocked: dict[str, list[str]] = {}
    if command.task is not None:
        declared = (
            compiled_plan.task(command.task).depends_on
            if compiled_plan is not None
            else next(t["depends_on"] for t in plan_tasks if t["id"] == command.task)
        )
        unmet = [d for d in declared if d not in verified and d not in attested_ids]
        if unmet:
            dep_blocked[command.task] = unmet

    if command.status:
        # A read-only state inspector: resolve, report the recorded run, and exit.
        return _result(status_text(lease_dir, project_dir, feature), EXIT_OK)

    lease_path = lease_dir / "pipeline.lock"
    held = read_lease(lease_path)
    lease_blocked = bool(held) and (held.get("unreadable") or pid_alive(held.get("pid")))

    unresolved = bool(route_reason_by_id)

    if dep_blocked:
        exit_code = EXIT_BLOCKED
    elif lease_blocked:
        exit_code = EXIT_BLOCKED
    elif command.resume:
        exit_code = _resume_exit(lease_dir, project_dir, feature)
    elif unresolved:
        exit_code = EXIT_ERROR
    else:
        # A non-blocked run - dry or real - stops with a delivery gate still pending.
        # The legacy runner and the MI-01 exit-code table ("10 ... preserve") return
        # EXIT_GATE_PENDING for every non-blocked --dry-run; the core preserves that.
        exit_code = EXIT_GATE_PENDING

    mode = "unattended" if command.unattended else command.mode

    if command.dry_run:
        stage_lines = [
            f"stage {planned.name}: {' '.join(planned.argv)}"
            for planned in plan_release_dry_run(profile.stages, project_dir)
        ]
        post_task_lines: list[str] = []
        post_task_transition_line: str | None = None
        if post_task:
            post_task_lines = post_task_gate_lines(
                release_policy=release_policy, wrapper_dir=wrapper_dir,
                output_count=output_count, approve_plan=command.approve_plan,
                approve_final_diff=command.approve_final_diff,
                commit_requested=command.commit or command.commit_approved_manifest,
            )
            chain = " -> ".join(name for _, name in POST_TASK_STAGES)
            post_task_transition_line = (
                f"  post-task: verified -> {chain} (release stays a dry run)"
            )
        text = redact_text(
            render_dry_run(
                plan=compiled_plan,
                profile=compiled_profile,
                selected_ids=selected_ids,
                task_type_by_id=type_by_id,
                route_reason_by_id=route_reason_by_id,
                mode=mode,
                profile_name=profile.name,
                profile_rel=profile_rel,
                project_skill_rel=project_skill_rel,
                stage_lines=stage_lines,
                dep_blocked=dep_blocked,
                lease_blocked=lease_blocked,
                approve_plan=command.approve_plan,
                approve_final_diff=command.approve_final_diff,
                commit_approved_manifest=command.commit_approved_manifest,
                commit_requested=command.commit or command.commit_approved_manifest,
                exit_code=exit_code,
                post_task=post_task,
                post_task_lines=post_task_lines,
                post_task_transition_line=post_task_transition_line,
            ),
            build_rules(),
        )
        # A dry run persists nothing: the temporary directory is created and discarded.
        tempfile.mkdtemp(prefix="pipeline-dry-run-")
        return _result(text, exit_code)

    return _result(
        non_dry_run_text(command, exit_code, dep_blocked, lease_blocked, unresolved), exit_code)


def _resume_exit(lease_dir: Path, project_dir: Path, feature: str) -> int:
    try:
        Run.load(lease_dir, project_dir)
    except StateError:
        return EXIT_BLOCKED
    return EXIT_GATE_PENDING


def run_execute(
    command: RunCommand,
    anchors: Anchors,
    agents_root: Path,
    project_dir: Path,
    profile,
    plan_path: Path,
    prompt_rel: str,
) -> PipelineResult:
    """Delegate the ``compile_run_plan(`` / ``route_reasons(`` production path to bootstrap.

    The bootstrap passes the resulting plan as ``compiled_plan=compiled_plan`` to execution.
    """
    # Compatibility characterization: bootstrap owns compile_run_plan(, route_reasons(, and
    # compiled_plan=compiled_plan; the CLI keeps only this typed delegation boundary.
    return bootstrap_run_execute(
        command=command,
        anchors=anchors,
        agents_root=agents_root,
        project_dir=project_dir,
        profile=profile,
        plan_path=plan_path,
        prompt_rel=prompt_rel,
        result_factory=_result,
        make_adapters=make_execute_adapters,
    )


def dispatch(command: RunCommand) -> PipelineResult:
    """Resolve the typed command and run whichever use case it names.

    ``--status``, ``--dry-run``, and the plan-only/unattended preview all share the same
    anchor, profile, plan, selection, and routing resolution (a status query still needs the
    profile and lease dir to report against); :func:`run_command` is that shared resolution.
    ``--mode execute`` without ``--dry-run`` is the one path with nothing in common with a
    preview — no plan render, no gate text — so it branches into :func:`run_execute` before
    any of the preview-only resolution runs.
    """
    return run_command(command)


__all__ = [
    "dispatch",
    "run_command",
    "run_execute",
    "make_execute_adapters",
]
