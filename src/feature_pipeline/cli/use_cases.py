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
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

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
    load_execute_specs,
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
from feature_pipeline.application.verified_reuse import (
    EvidenceEligibilityError,
    VerifiedEvidenceStore,
    resolve_default_reuse,
    supersession_graph,
)
from feature_pipeline.application.results import Outcome, PipelineResult
from feature_pipeline.application.selection import (
    SelectionError,
    prune_reused_ancestors,
    resolve_selection,
)
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.graph import TaskGraph
from feature_pipeline.contracts import SchemaError, validate_preconditions, validate_relative_path

from feature_pipeline.bootstrap import (
    AmendmentError,
    AmendmentRequest,
    build_amendment_revision,
    canonical_amendment_fields,
    contract_digest,
)

from .commands import RunCommand
from .errors import CliError
from .parser import (
    AMEND_MODE,
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
        if not isinstance(entry, dict) or not entry.get("id") or not (entry.get("type") or entry.get("task_type")):
            raise CliError(EXIT_ERROR, "every plan task needs an 'id' and a 'type'")
        tid = str(entry["id"])
        if tid in seen:
            raise CliError(EXIT_ERROR, f"duplicate task id in plan: {tid}")
        seen.add(tid)
        depends_on = entry.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise CliError(EXIT_ERROR, f"{tid}: depends_on must be a list")
        tasks.append({**entry, "id": tid, "type": str(entry.get("type") or entry["task_type"]),
                      "depends_on": [str(d) for d in depends_on]})
    if not tasks:
        raise CliError(EXIT_ERROR, "plan has no tasks")
    return (str(data["feature"]) if data.get("feature") else None), tasks


def _dry_run_reuse_definitions(
    plan_path: Path, feature: str, plan_tasks: list[dict],
) -> dict[str, object]:
    """Load exact execution contracts where available, retaining thin-preview support.

    A Markdown plan (and a rich JSON plan) has the same task contracts execute uses. A
    route-only JSON preview has no such body, so it can only match legacy task-ID evidence.
    """
    if plan_path.suffix.lower() == ".md":
        thin = not any((plan_path.parent / "tasks").glob("*.md"))
    else:
        thin = all(not (set(task) & {"executor", "allowed_scope", "acceptance_criteria"})
                   for task in plan_tasks)
    if thin:
        predicates = {}
        for task in plan_tasks:
            try:
                predicates[task["id"]] = validate_preconditions(task.get("preconditions", ()))
            except SchemaError as exc:
                raise CliError(EXIT_ERROR, str(exc)) from None
        return {
            task["id"]: SimpleNamespace(
                id=task["id"],
                path=f"tasks/{task['id']}.md",
                depends_on=task["depends_on"],
                allowed_scope=(f"tasks/{task['id']}.md",),
                out_of_scope=(),
                acceptance_criteria=(),
                verification_commands=(),
                verification_tier="full",
                preconditions=predicates[task["id"]],
            )
            for task in plan_tasks
        }
    _, specs = load_execute_specs(plan_path, feature)
    return {spec.id: spec for spec in specs}


def _load_profile(profile_path: Path):
    """Load the runnable profile, translating every load failure to a typed ``CliError``."""
    try:
        return load_runnable_profile(profile_path)
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "profile file not found") from None
    except SchemaError as exc:
        raise CliError(EXIT_ERROR, f"profile is invalid: {exc}") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "profile file is not valid JSON") from None


def _load_post_task_config(
    profile_path: Path, post_task: bool
) -> tuple[ReleasePolicy | None, str | None, int]:
    """``release-dry-run`` consumes the project's post-task contract as-is: the
    tool-integration block and the release policy declared beside the profile. Returns
    ``(release_policy, wrapper_dir, expected_output_count)``; a plain run gets the neutral
    ``(None, None, 0)``. Loading is fail-closed."""
    if not post_task:
        return None, None, 0
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
    return release_policy, integration.wrapper_dir, len(integration.expected_outputs)


def _preview_exit_code(
    *,
    dep_blocked: dict[str, list[str]],
    lease_blocked: bool,
    resume: bool,
    unresolved: bool,
    lease_dir: Path,
    project_dir: Path,
    feature: str,
) -> int:
    """The frozen ``EXIT_*`` outcome for a status / dry-run / plan-only preview."""
    if dep_blocked:
        return EXIT_BLOCKED
    if lease_blocked:
        return EXIT_BLOCKED
    if resume:
        return _resume_exit(lease_dir, project_dir, feature)
    if unresolved:
        return EXIT_ERROR
    # A non-blocked run - dry or real - stops with a delivery gate still pending.
    # The legacy runner and the MI-01 exit-code table ("10 ... preserve") return
    # EXIT_GATE_PENDING for every non-blocked --dry-run; the core preserves that.
    return EXIT_GATE_PENDING


def _substitute_superseded(
    scope: Sequence[str], selected: Sequence[str], order: Sequence[str], graph,
) -> tuple[str, ...]:
    """Replace each non-selected scope task a later task supersedes with that replacement.

    Used by the ``--verify-dependency-chain`` preview so a retired blocked predecessor is
    never in the planned dispatch set; its live replacement (which *can* be verified) stands
    in. Order follows the plan; the result is de-duplicated.
    """
    selected_set = set(selected)
    resolved: set[str] = set()
    for task_id in scope:
        if task_id in selected_set:
            resolved.add(task_id)
            continue
        node = task_id
        seen: set[str] = set()
        while True:
            nxt = graph.replacement_for(node)
            if nxt is None or nxt in seen:
                break
            seen.add(nxt)
            node = nxt
        resolved.add(node)
    return tuple(task_id for task_id in order if task_id in resolved)


def run_amend(command: RunCommand) -> PipelineResult:
    """Persist one explicit, human-approved amendment revision (TAM-01).

    Deliberately independent of profile/plan resolution and task selection: an amendment is
    a control-plane act on the already-persisted run, not a dispatch. It requires only the
    project root (the run's repository root) and the run's feature name to locate
    ``run.json`` at the runner's standard storage layout, plus the explicit amendment inputs
    below. Fails closed — via :class:`~pipeline_core.plan.AmendmentError` — for a missing
    rationale or approval, a completed task, a task-id change, or an amendment that touches
    a forbidden control-plane path; nothing is persisted on any rejection.
    """
    project_root = Path(_require(command.project_root, "--project-root"))
    feature = _require(command.feature, "--feature")
    task_id = _require(command.amend_task, "--amend-task")
    rationale = _require(command.amend_rationale, "--amend-rationale")
    approved_by = _require(command.amend_approved_by, "--amend-approved-by")
    evidence = _require(command.amend_evidence, "--amend-evidence")
    contract_rel = _require(command.amend_contract, "--amend-contract")
    contract_path = project_root / contract_rel
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError(EXIT_ERROR, f"--amend-contract could not be read: {exc}") from None
    except json.JSONDecodeError as exc:
        raise CliError(EXIT_ERROR, f"--amend-contract is not valid JSON: {exc}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("new_contract"), dict):
        raise CliError(EXIT_ERROR, "--amend-contract must declare a 'new_contract' object")
    new_contract = payload["new_contract"]
    prior_contract = payload.get("prior_contract")
    if not isinstance(prior_contract, dict):
        prior_contract = {}
    added_paths = payload.get("added_paths", [])
    if not isinstance(added_paths, list):
        raise CliError(EXIT_ERROR, "--amend-contract 'added_paths' must be a list")

    lease_dir = project_root / ".pipeline" / "runs" / feature
    try:
        run = Run.load(lease_dir, project_root)
    except StateError as exc:
        raise CliError(EXIT_ERROR, f"amendment target run could not be loaded: {exc}") from None
    try:
        record = run.task(task_id)
    except StateError as exc:
        raise CliError(EXIT_ERROR, str(exc)) from None

    request = AmendmentRequest(
        task_id=task_id, task_status=record.status, prior_contract=prior_contract,
        new_contract=new_contract, rationale=rationale, approved_by=approved_by,
        source_evidence=evidence, added_paths=tuple(str(item) for item in added_paths),
    )
    try:
        revision = build_amendment_revision(
            request, next_revision=record.current_revision + 1,
            next_epoch=record.current_revision + 1,
        )
        new_digest = contract_digest(canonical_amendment_fields(new_contract))
        run.apply_amendment(
            revision, new_digest=new_digest, new_digest_version="tam01-amendment-v1")
        # An amendment creates a new execution epoch.  Keep the strict-resume
        # fingerprint aligned with amendment-governed repair policy, otherwise
        # a legitimate bound change is rejected before that epoch can dispatch.
        if "max_repair_attempts" in new_contract:
            repair_bound = int(new_contract["max_repair_attempts"])
            fingerprint = dict(run.controls.get("plan_fingerprint", {}).get("value") or {})
            prefix = f"task.{task_id}"
            fingerprint[f"{prefix}.repair_bound"] = repr(repair_bound)
            fingerprint[f"{prefix}.control.max_repair_attempts"] = f"{repair_bound} (default)"
            run.set_control("plan_fingerprint", fingerprint, sourced="amendment")
    except AmendmentError as exc:
        raise CliError(EXIT_BLOCKED, f"amendment rejected ({exc.code}): {exc}") from None
    run.save()
    return _result(
        f"amendment applied: {task_id} revision {revision.revision} approved by "
        f"{approved_by}; changed fields: {', '.join(revision.changed_fields) or 'none'}\n",
        EXIT_OK,
    )


def run_command(command: RunCommand) -> PipelineResult:
    """Resolve anchors and profile, then dispatch to status, dry-run, or execute."""
    if command.mode == AMEND_MODE:
        return run_amend(command)
    project_root = Path(_require(command.project_root, "--project-root"))
    agents_root = Path(_require(command.agents_root, "--agents-root"))
    core_root = Path(_require(command.core_root, "--core-root"))
    anchors = Anchors(project_root, agents_root, core_root)

    # ``release-dry-run`` is a plan-only preview by construction: it never runs an executor
    # and never writes, so it always implies ``--dry-run``.
    post_task = command.mode == POST_TASK_MODE
    if post_task:
        command.dry_run = True

    if (command.recovery_source_feature or command.recovery_task) and (
        command.mode != "execute" or command.dry_run
    ):
        raise CliError(EXIT_ERROR, "recovery selectors are valid only for --mode execute")
    if (command.operational_unblock_task or command.human_authorized_operational_unblock
            or command.uv_cache_dir) and (command.mode != "execute" or command.dry_run):
        raise CliError(EXIT_ERROR, "operational unblock controls are valid only for --mode execute")
    if (command.model is not None or command.effort is not None) and command.mode != "execute":
        raise CliError(EXIT_ERROR, "--model and --effort are valid only for --mode execute")

    profile_rel = _logical_relative(_require(command.profile, "--profile"), "--profile")
    profile_path = _resolve_under(project_root, profile_rel, "--profile")
    project_skill_rel = None
    if command.project_skill:
        project_skill_rel = _logical_relative(command.project_skill, "--project-skill")
        _resolve_under(agents_root, project_skill_rel, "--project-skill")

    profile = _load_profile(profile_path)

    release_policy, wrapper_dir, output_count = _load_post_task_config(profile_path, post_task)

    project_dir = _resolve_under(project_root, profile.logical_paths.project, "project path")
    composition = build_bootstrap(project_dir, agents_root, core_root)
    plan_rel = _logical_relative(_require(command.plan, "--plan"), "--plan")
    plan_path = _resolve_under(project_dir, plan_rel, "--plan")
    prompt_rel = _logical_relative(command.prompt, "--prompt") if command.prompt else plan_rel
    _resolve_under(project_dir, prompt_rel, "--prompt")

    # ``--status`` is a read-only inspector even when the caller also supplies
    # ``--mode execute`` and delivery-gate flags: it must never reach ``run_execute`` (which
    # evaluates the plan gate, initializes/reconciles a run, and can dispatch an executor).
    # Execute-mode controls, adapter/model/effort, and approval flags stay inert compatibility
    # inputs for a status query; only the shared, read-only preview resolution below runs.
    if command.mode == "execute" and not command.dry_run and not command.status:
        return run_execute(command, anchors, agents_root, project_dir, profile, plan_path,
                            prompt_rel)

    plan_feature, plan_tasks = _load_plan(plan_path)
    feature = command.feature or plan_feature
    if not feature or not FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    verified: set[str] = set()  # a fresh run has verified nothing
    definitions = _dry_run_reuse_definitions(plan_path, feature, plan_tasks)
    type_by_id = {t["id"]: t["type"] for t in plan_tasks}
    shallow_inputs = [
        ShallowTaskInput(t["id"], t["type"], tuple(t["depends_on"]),
                         executor=getattr(definitions[t["id"]], "executor", ""),
                         preconditions=definitions[t["id"]].preconditions)
        for t in plan_tasks
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
                overrides=ControlOverrides(
                    max_repair_attempts=command.max_repair_attempts,
                    adapter=command.adapter,
                    model=command.model,
                    effort=command.effort,
                    verify_dependency_chain=(
                        True if command.verify_dependency_chain else None
                    ),
                ),
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
    reused_sources: dict[str, str] = {}
    execution_scope = compiled_plan.execution_scope if compiled_plan is not None else tuple(selected_ids)
    if command.dry_run:
        attested_ids = resolve_attested_dependency_ids(
            raw_attestations=command.attest_dependency,
            selected_task=command.task,
            plan_tasks=plan_tasks,
            definitions=definitions,
            run_dir=lease_dir,
            repo_root=project_dir,
            prompt_path=project_dir / prompt_rel,
            plan_path=plan_path,
            resolve_sources=not command.verify_dependency_chain,
        )
        if compiled_plan is not None and not command.verify_dependency_chain:
            reused_sources.update(
                dict(item.split("=", 1) for item in (command.attest_dependency or ()))
            )
            store = VerifiedEvidenceStore(lease_dir.parent, project_dir)
            try:
                reused = resolve_default_reuse(
                    store, definitions, compiled_plan.execution_scope, compiled_plan.selection,
                    project_dir, tuple(attested_ids),
                )
            except EvidenceEligibilityError as exc:
                raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None
            for task_id, evidence in reused.items():
                attested_ids.add(task_id)
                reused_sources[task_id] = evidence["source_run_id"]
            execution_scope = prune_reused_ancestors(
                execution_scope, compiled_plan.selection, reused_sources,
                {task_id: definition.depends_on for task_id, definition in definitions.items()},
            )
            execution_scope = tuple(
                task_id for task_id in execution_scope
                if task_id in compiled_plan.selection or "replacement_id" not in reused.get(task_id, {})
            )
        elif compiled_plan is not None and command.verify_dependency_chain:
            # `--verify-dependency-chain` re-verifies the chain rather than trusting reuse,
            # but a retired blocked predecessor cannot be re-run: substitute the live task
            # that supersedes it so the preview never plans to dispatch the blocked one.
            superseders = supersession_graph(definitions, project_dir)
            if superseders is not None:
                execution_scope = _substitute_superseded(
                    execution_scope, compiled_plan.selection, compiled_plan.order, superseders
                )

    dep_blocked: dict[str, list[str]] = {}
    if command.task is not None:
        declared = (
            compiled_plan.task(command.task).depends_on
            if compiled_plan is not None
            else next(t["depends_on"] for t in plan_tasks if t["id"] == command.task)
        )
        unmet = [] if command.verify_dependency_chain else [
            d for d in declared if d not in verified and d not in attested_ids
        ]
        if unmet:
            dep_blocked[command.task] = unmet

    if command.status:
        # A read-only state inspector: resolve, report the recorded run, and exit.
        return _result(status_text(lease_dir, project_dir, feature), EXIT_OK)

    lease_path = lease_dir / "pipeline.lock"
    held = read_lease(lease_path)
    lease_blocked = bool(held) and (held.get("unreadable") or pid_alive(held.get("pid")))

    unresolved = bool(route_reason_by_id)

    exit_code = _preview_exit_code(
        dep_blocked=dep_blocked,
        lease_blocked=lease_blocked,
        resume=command.resume,
        unresolved=unresolved,
        lease_dir=lease_dir,
        project_dir=project_dir,
        feature=feature,
    )

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
                verify_dependency_chain=command.verify_dependency_chain,
                execution_scope=(
                    execution_scope
                ),
                reused_sources=reused_sources,
            ),
            build_rules(),
        )
        predicate_lines = [
            f"  {task_id}: {p.identifier} — unresolved (execute-time evaluation required)"
            for task_id in execution_scope for p in definitions[task_id].preconditions
        ]
        if predicate_lines:
            text += redact_text("\nPreconditions:\n" + "\n".join(predicate_lines) + "\n", build_rules())
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
    "run_amend",
    "make_execute_adapters",
]
