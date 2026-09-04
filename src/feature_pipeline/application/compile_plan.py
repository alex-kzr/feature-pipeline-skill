"""Compile every execution decision once, before any side effect (CP-01).

:func:`compile_run_plan` is a pure function. From normalized
:class:`~feature_pipeline.domain.models.TaskDefinition` inputs (IN-01), a typed
:class:`~feature_pipeline.inputs.profile.CompiledProfile` (the profile owns task-type routes),
and an in-process :class:`~feature_pipeline.ports.adapters.AdapterRegistry`, it builds one
immutable :class:`~feature_pipeline.domain.plan.CompiledRunPlan`.

It resolves the route, working root, storage root, verification commands, executor role and
grant, execution adapter and its declared capabilities, the launch timeout, the two output
budgets, the serialized-program set, and the repair bound — each accepted control recorded
once with its :class:`~feature_pipeline.domain.controls.ResolvedControl` provenance (AC-1).
Every denial — an unknown route, an unsafe anchor, conflicting run storage, an unsupported
control value, an incompatible adapter requirement — is raised here, before the caller creates
run state, a lease, an artifact, or a process (AC-2). The current built-in task types all
resolve through the default generated profile (AC-3).

CP-02 cuts the production dry-run / execute / resume paths over to this object; ``pipeline_core``
is unchanged by CP-01.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Union

from feature_pipeline.application.selection import resolve_selection
from feature_pipeline.domain.controls import ControlSource, ResolvedControl
from feature_pipeline.domain.graph import TaskGraph
from feature_pipeline.domain.identifiers import FeatureId
from feature_pipeline.domain.models import TaskDefinition
from feature_pipeline.domain.paths import RelativeGlob
from feature_pipeline.domain.plan import (
    CompiledRunPlan,
    ConflictingStorage,
    ResolvedCheck,
    ResolvedTask,
    UnsupportedControl,
)
from feature_pipeline.domain.repair import DEFAULT_MAX_REPAIR_ATTEMPTS, RepairBound
from feature_pipeline.inputs.profile import CompiledProfile
from feature_pipeline.ports.adapters import AdapterRegistry

#: Mirrors :data:`pipeline_core.commands.ROUTINE_OUTPUT_BUDGET` /
#: :data:`pipeline_core.commands.DIAGNOSTIC_OUTPUT_BUDGET`.
ROUTINE_OUTPUT_BUDGET = 16 * 1024
DIAGNOSTIC_OUTPUT_BUDGET = 64 * 1024

#: Mirrors :data:`pipeline_core.execution.DEFAULT_ROLE_GRANT`. A read-only role can never keep
#: ``write`` (:func:`pipeline_core.adapters.effective_grant`); the executor is write-capable.
DEFAULT_ROLE_GRANT = ("read", "run_checks", "write")

#: The single executor role every synthesised route dispatches (``project_profile._EXECUTOR``).
EXECUTOR_ROLE = "executor"

#: This umbrella repository has no compiled-language build tool, so nothing is serialized
#: behind the cross-process write mutex (``pipeline_core.concurrency``). A project that ships
#: one declares its basename; the compiler would carry it here.
SERIALIZED_PROGRAMS: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShallowTaskInput:
    """A route-only task input: id, type and dependency edges, nothing from the task body.

    The dry-run preview never loads task files — it routes an ``id`` + ``type`` (+
    ``depends_on``) plan. That is not enough to build a :class:`TaskDefinition` (its wrapped
    :class:`~feature_pipeline.contracts.TaskSpec` requires an allowed scope), so the preview
    feeds these instead. Execute mode always passes full :class:`TaskDefinition` values.
    None of the dry-run plan's C1–C8 sections displays a task-body field, so the shallow
    input drives the preview completely.
    """

    id: str
    task_type: str
    depends_on: tuple[str, ...] = ()
    executor: str = ""
    allowed_scope: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS


#: What :func:`compile_run_plan` accepts per task — a fully normalized definition (execute)
#: or a route-only shallow input (dry-run preview). Both satisfy the ``.id`` / ``.depends_on``
#: shape :meth:`TaskGraph.from_definitions` needs.
TaskInput = Union[TaskDefinition, ShallowTaskInput]


@dataclass(frozen=True)
class ControlOverrides:
    """The operational controls a caller may set explicitly.

    Each is ``None`` when the flag was not passed, in which case the documented default is
    resolved and tagged :data:`ControlSource.DEFAULT`.
    """

    max_repair_attempts: int | None = None
    routine_output_byte_budget: int | None = None
    diagnostic_output_byte_budget: int | None = None
    timeout_s: float | None = None
    adapter: str | None = None


#: The "no explicit control" sentinel — a frozen singleton, so it is safe as a default.
_NO_OVERRIDES = ControlOverrides()


def compile_run_plan(
    *,
    feature: str,
    definitions: Sequence[TaskInput],
    profile: CompiledProfile,
    overrides: ControlOverrides = _NO_OVERRIDES,
    adapters: AdapterRegistry | None = None,
    task: str | None = None,
    through: str | None = None,
    required_adapter_capabilities: Sequence[str] = (),
) -> CompiledRunPlan:
    """Compile one :class:`CompiledRunPlan`. Pure — no state, lease, artifact, or process."""
    registry = adapters if adapters is not None else AdapterRegistry.default()
    slug = FeatureId(feature)

    # 1. Dependency correctness + deterministic order + selection, all fail-closed.
    graph = TaskGraph.from_definitions(definitions)
    selection = resolve_selection(graph, task=task, through=through)

    # 2. Resolve the execution adapter once; reject a required capability it cannot meet.
    adapter = registry.resolve(overrides.adapter)
    if required_adapter_capabilities:
        registry.require(adapter.name, required_adapter_capabilities)
    adapter_explicit = overrides.adapter is not None
    adapter_source = ControlSource.EXPLICIT if adapter_explicit else ControlSource.DEFAULT

    # 3. Run-level controls — each resolved once, carrying its provenance.
    routine_control = _budget_control(
        "routine_output_byte_budget",
        overrides.routine_output_byte_budget,
        ROUTINE_OUTPUT_BUDGET,
    )
    diagnostic_control = _budget_control(
        "diagnostic_output_byte_budget",
        overrides.diagnostic_output_byte_budget,
        DIAGNOSTIC_OUTPUT_BUDGET,
    )
    timeout_control = _timeout_control(overrides.timeout_s, adapter.default_timeout_s)
    run_controls: tuple[ResolvedControl[Any], ...] = (
        ResolvedControl("adapter_requested", overrides.adapter or "auto", adapter_source),
        ResolvedControl("adapter_resolved", adapter.name, adapter_source),
        routine_control,
        diagnostic_control,
        timeout_control,
    )

    by_id = {definition.id: definition for definition in definitions}
    resolved_tasks: list[ResolvedTask] = []
    storage_roots: dict[str, object] = {}

    for task_id in selection.task_ids:
        definition = by_id[task_id]
        route = profile.route_for(definition.task_type)
        storage_root = profile.storage_root(route.storage_key)
        storage_roots[str(storage_root)] = storage_root

        checks = tuple(
            ResolvedCheck(command.name, command.argv, command.cwd)
            for command in (
                profile.check_command(name) for name in route.check_names
            )
        )

        repair_control = _repair_control(definition, overrides.max_repair_attempts)

        resolved_tasks.append(
            ResolvedTask(
                task_id=task_id,
                task_type=definition.task_type,
                executor=definition.executor,
                working_root=route.working_root,
                storage_root=storage_root,
                role=EXECUTOR_ROLE,
                role_grant=tuple(sorted(set(DEFAULT_ROLE_GRANT))),
                adapter=adapter.name,
                capabilities=adapter.capabilities,
                checks=checks,
                timeout_s=float(timeout_control.value),
                routine_output_budget=int(routine_control.value),
                diagnostic_output_budget=int(diagnostic_control.value),
                serialized_programs=SERIALIZED_PROGRAMS,
                repair_bound=RepairBound(int(repair_control.value)),
                depends_on=tuple(definition.depends_on),
                allowed_scope=tuple(
                    RelativeGlob.parse(entry) for entry in definition.allowed_scope
                ),
                out_of_scope=tuple(
                    RelativeGlob.parse(entry) for entry in definition.out_of_scope
                ),
                controls=(repair_control,),
            )
        )

    # 4. A run has exactly one run-storage tree; a selection that spans two is a conflict.
    if len(storage_roots) > 1:
        raise ConflictingStorage(
            "selected tasks resolve to more than one run-storage root: "
            + ", ".join(sorted(storage_roots))
        )

    return CompiledRunPlan(
        feature=slug,
        project=profile.project,
        selection_mode=selection.mode,
        selection=tuple(selection.task_ids),
        order=graph.order,
        tasks=tuple(resolved_tasks),
        adapter=adapter.name,
        controls=run_controls,
    )


# --- control resolution ------------------------------------------------------------------


def _repair_control(
    definition: "TaskInput", override: int | None
) -> ResolvedControl[Any]:
    """Resolve one task's repair bound.

    An explicit ``--max-repair-attempts`` overrides every task's declared bound (the same
    effect as ``pipeline_core.execution._apply_repair_bound``); otherwise the task keeps the
    bound its own file declared. A negative or non-integer value raises
    :class:`~feature_pipeline.domain.errors.InvalidControl` here, before any side effect
    (``docs/adr/007`` §5).
    """
    if override is None:
        value = definition.max_repair_attempts
        source = ControlSource.DEFAULT
    else:
        value = int(RepairBound(override))
        source = ControlSource.EXPLICIT
    # Validate the declared value too — a task file is normally checked by TaskSpec.build,
    # but the compiler must never trust an un-revalidated bound onto the plan.
    RepairBound(value)
    _ = DEFAULT_MAX_REPAIR_ATTEMPTS  # documented default; kept referenced for clarity
    return ResolvedControl("max_repair_attempts", value, source)


def _budget_control(
    name: str, override: int | None, default: int
) -> ResolvedControl[Any]:
    if override is None:
        return ResolvedControl(name, default, ControlSource.DEFAULT)
    if isinstance(override, bool) or not isinstance(override, int) or override <= 0:
        raise UnsupportedControl(f"{name} must be a positive integer, got {override!r}")
    return ResolvedControl(name, override, ControlSource.EXPLICIT)


def _timeout_control(
    override: float | None, default: float
) -> ResolvedControl[Any]:
    if override is None:
        return ResolvedControl("timeout_s", default, ControlSource.DEFAULT)
    if isinstance(override, bool) or not isinstance(override, (int, float)) or override <= 0:
        raise UnsupportedControl(
            f"timeout_s must be a positive number of seconds, got {override!r}"
        )
    return ResolvedControl("timeout_s", float(override), ControlSource.EXPLICIT)


__all__ = [
    "ROUTINE_OUTPUT_BUDGET",
    "DIAGNOSTIC_OUTPUT_BUDGET",
    "DEFAULT_ROLE_GRANT",
    "EXECUTOR_ROLE",
    "SERIALIZED_PROGRAMS",
    "ControlOverrides",
    "ShallowTaskInput",
    "TaskInput",
    "compile_run_plan",
]
