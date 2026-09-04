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
from types import SimpleNamespace

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
from schemas.contracts import TaskSpec

from pipeline_core.adapters import ClaudeAdapter
from pipeline_core.execution import (
    ExecuteControls,
    ExecuteRequest,
    ExecutionError,
    execute_run,
    _resolve_attestation,
    _validate_attestation_scope,
)
from pipeline_core.integrations import load_tool_integration
from pipeline_core.plan_md import MarkdownPlanError, load_markdown_plan
from pipeline_core.post_task import POST_TASK_STAGES
from pipeline_core.profiles import Anchors
from pipeline_core.project_profile import load_runnable_profile
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.redaction import build_rules, redact_text
from pipeline_core.release import ReleasePolicy, load_release_policy
from pipeline_core.stages import plan_release_dry_run
from pipeline_core.state import Run, StateError, read_lease, pid_alive
from pipeline_core.task_files import load_task_spec
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers

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
    SOURCE_FEATURE_RE,
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
                task=command.task,
                through=command.through,
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
        attestations = _parse_attestations(command.attest_dependency)
        if attestations:
            by_id = {t["id"]: SimpleNamespace(depends_on=t["depends_on"]) for t in plan_tasks}
            attestation_controls = ExecuteControls(
                task=command.task, attested_dependencies=attestations)
            try:
                _validate_attestation_scope(attestation_controls, by_id)
                for dep_id, source_feature in attestations:
                    _resolve_attestation(
                        dep_id=dep_id, source_feature=source_feature, run_dir=lease_dir,
                        repo_root=project_dir, prompt_path=project_dir / prompt_rel,
                        plan_path=plan_path)
                    attested_ids.add(dep_id)
            except ExecutionError as exc:
                raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None

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


# --- execute mode (stages 5-9) -----------------------------------------------------------


def _project_relative(path: Path, project_dir: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return None


def _load_execute_specs(
    plan_path: Path, feature_override: str | None
) -> tuple[str | None, list[TaskSpec]]:
    """Resolve the plan to complete :class:`TaskSpec` values for execute mode.

    A ``.md`` plan reads its task table and normalizes each ``tasks/<ID>_*.md`` file through
    :func:`pipeline_core.task_files.load_task_spec`. A ``.json`` plan must carry the full
    execution metadata per task (``task_type``/``executor``/``allowed_scope``/…) — the
    id+type-only shape the dry run accepts is not enough to launch anything.
    """
    if plan_path.suffix.lower() == ".md":
        try:
            feature, entries = load_markdown_plan(plan_path)
        except MarkdownPlanError as exc:
            raise CliError(EXIT_ERROR, str(exc)) from None
        tasks_dir = plan_path.parent / "tasks"
        specs: list[TaskSpec] = []
        for entry in entries:
            tid = entry["id"]
            matches = sorted(tasks_dir.glob(f"{tid}_*.md"))
            if not matches:
                raise CliError(
                    EXIT_ERROR,
                    f"execute mode: no task file 'tasks/{tid}_*.md' beside the plan")
            try:
                specs.append(load_task_spec(matches[0]))
            except SchemaError as exc:
                raise CliError(EXIT_ERROR, f"{tid}: {exc}") from None
        return (feature_override or feature), specs

    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "plan file not found") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "plan file is not valid JSON") from None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise CliError(EXIT_ERROR, "plan must be an object with a non-empty 'tasks' list")
    specs = []
    seen: set[str] = set()
    for entry in tasks:
        if not isinstance(entry, dict):
            raise CliError(EXIT_ERROR, "every plan task must be an object")
        kwargs = dict(entry)
        if "task_type" not in kwargs and "type" in kwargs:
            kwargs["task_type"] = kwargs.pop("type")
        if "allowed_scope" not in kwargs:
            raise CliError(
                EXIT_ERROR,
                "execute mode needs full task metadata: give each JSON task 'task_type', "
                "'executor', 'allowed_scope', and 'acceptance_criteria', or point --plan at a "
                "Markdown plan with task files")
        try:
            spec = TaskSpec.build(**kwargs)
        except (SchemaError, TypeError) as exc:
            raise CliError(EXIT_ERROR, f"{entry.get('id', '?')}: {exc}") from None
        if spec.id in seen:
            raise CliError(EXIT_ERROR, f"duplicate task id in plan: {spec.id}")
        seen.add(spec.id)
        specs.append(spec)
    feature = str(data["feature"]) if data.get("feature") else None
    return (feature_override or feature), specs


def _parse_attestations(raw: list[str] | None) -> tuple[tuple[str, str], ...]:
    """Parse repeated ``--attest-dependency DEP_ID=SOURCE_FEATURE`` flags, failing closed on
    unsafe syntax before any source-run path is built. ``execution._validate_attestation_scope``
    and ``_resolve_attestation`` own every other denial reason (scope, duplicate,
    not-a-dependency, source resolution)."""
    if not raw:
        return ()
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if "=" not in item:
            raise CliError(
                EXIT_ERROR, f"--attest-dependency must be DEP_ID=SOURCE_FEATURE: '{item}'")
        dep_id, source_feature = item.split("=", 1)
        if not dep_id:
            raise CliError(EXIT_ERROR, f"--attest-dependency has an empty DEP_ID: '{item}'")
        if not SOURCE_FEATURE_RE.match(source_feature):
            raise CliError(
                EXIT_ERROR,
                "--attest-dependency source feature must be a bare directory name (letters, "
                f"digits, '-', '_' only; no path separator, '.', '..', or absolute/drive path): "
                f"'{source_feature}'")
        parsed.append((dep_id, source_feature))
    return tuple(parsed)


def _resolve_add_dirs(project_dir: Path, *anchors: Path) -> tuple[Path, ...]:
    """Resolve each anchor and keep only those not already reachable from a granted root.

    The adapter's ``working_root`` (the resolved ``project_dir``) is always reachable, so it
    seeds the covered set without being emitted. Each further anchor is resolved once
    (symlink/junction-following) and emitted only when it does not sit under ``project_dir``
    or under an anchor already granted in this call — a core checkout nested under the agents
    root needs no separate ``--add-dir``; one in an unrelated filesystem tree does. Input
    order is preserved and no resolved path is emitted twice.
    """
    covered: list[Path] = [Path(project_dir).resolve()]
    granted: list[Path] = []
    for anchor in anchors:
        resolved = Path(anchor).resolve()
        if any(resolved.is_relative_to(root) for root in covered):
            continue
        granted.append(resolved)
        covered.append(resolved)
    return tuple(granted)


def make_execute_adapters(project_dir: Path, agents_root: Path, core_root: Path):
    """Build the production executor adapter and the (identical) read-only verifier pair.

    A module-level seam: a test replaces this with deterministic fake adapters.

    ``add_dirs`` carries the fully resolved, symlink/junction-following real paths of the
    anchors the run was given — ``agents_root`` (RDS-10) and ``core_root`` (RDS-17) — that
    are not already reachable from the adapter's ``working_root`` (the resolved
    ``project_dir``). The Claude CLI's own directory sandbox resolves the *requested* path
    and compares that resolved form against its allowed-directory list, so an unresolved
    logical path is silently denied even though it looks like a legitimate subpath: a
    symlinked ``.agents`` that reaches outside ``project_dir`` (a shared Syncthing-replicated
    skills library), or — one link deeper — a ``.agents/skills/software-development/
    feature-pipeline`` discovery link whose real target is a *separate* core checkout, not a
    subdirectory of the resolved ``agents_root``. Resolving here, in Python, before the CLI
    ever sees the argv, is the only way to grant the directory it will actually check. A
    resolved anchor that already sits under ``working_root`` (or under an anchor already
    granted) adds nothing — this emits exactly the roots genuinely outside every other
    grant, never a duplicate, whether the core resolves under ``agents_root``, beside it, or
    in an unrelated tree.
    """
    add_dirs = _resolve_add_dirs(project_dir, agents_root, core_root)
    executor = ClaudeAdapter(working_root=str(project_dir), add_dirs=add_dirs)
    launchers = VerifierLaunchers(task=executor, test=executor)
    environment = {"claude": executor.available(), "codex": False}
    return executor, launchers, environment


def run_execute(
    command: RunCommand,
    anchors: Anchors,
    agents_root: Path,
    project_dir: Path,
    profile,
    plan_path: Path,
    prompt_rel: str,
) -> PipelineResult:
    feature, specs = _load_execute_specs(plan_path, command.feature)
    if not feature or not FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    # One compilation for the whole run: the same object the dry-run preview renders. A task
    # type with no registry route, an unsafe control, or a selection that spans two
    # run-storage roots (AC-2) fails closed here, before any run state, lease, or process.
    reason_by_type = route_reasons(profile, (spec.task_type for spec in specs))
    if reason_by_type:
        bad = ", ".join(f"{t} ({r})" for t, r in sorted(reason_by_type.items()))
        raise CliError(EXIT_ERROR, f"execute mode: unroutable task type(s): {bad}")
    try:
        compiled_plan = compile_run_plan(
            feature=feature,
            definitions=[
                ShallowTaskInput(
                    s.id, s.task_type, tuple(s.depends_on), s.executor,
                    tuple(s.allowed_scope), tuple(s.out_of_scope), s.max_repair_attempts,
                )
                for s in specs
            ],
            profile=compiled_profile_from_core(profile),
            overrides=ControlOverrides(
                max_repair_attempts=command.max_repair_attempts,
                routine_output_byte_budget=command.routine_output_byte_budget,
                diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
                adapter=command.adapter,
            ),
            task=command.task,
            through=command.through,
        )
    except DomainError as exc:
        raise CliError(EXIT_ERROR, f"{getattr(exc, 'code', 'plan-error')}: {exc}") from None

    storage_rel = str(compiled_plan.tasks[0].storage_root) if compiled_plan.tasks else None
    run_dir = (
        (project_dir / storage_rel) if storage_rel else project_dir / ".pipeline" / "runs"
    ) / feature

    executor, launchers, environment = make_execute_adapters(
        project_dir, agents_root, anchors.core_root)
    controls = ExecuteControls(
        plan_approved=command.approve_plan,
        unattended=command.unattended,
        resume=command.resume,
        adapter=command.adapter,
        adapter_explicit=command.adapter is not None,
        max_repair_attempts=command.max_repair_attempts,
        routine_output_byte_budget=command.routine_output_byte_budget,
        diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
        task=command.task,
        through=command.through,
        attested_dependencies=_parse_attestations(command.attest_dependency),
    )
    request = ExecuteRequest(
        feature=feature,
        repo_root=project_dir,
        run_dir=run_dir,
        prompt_path=project_dir / prompt_rel,
        plan_path=plan_path,
        specs=tuple(specs),
        adapter=executor,
        launchers=launchers,
        envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
        verifier_anchors=VerifierAnchors(
            project_root=str(project_dir), agents_root=str(agents_root)),
        environment=environment,
        controls=controls,
        plan_prompt_path=_project_relative(plan_path, project_dir),
        compiled_plan=compiled_plan,
    )
    result = execute_run(request)
    if result.status == "error":
        raise CliError(result.exit_code, result.message)
    return _result(redact_text(result.message.rstrip("\n") + "\n", build_rules()),
                   result.exit_code)


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
