"""The immutable compiled execution plan (CP-01).

``pipeline_core`` decides route, root, storage, checks, role grant, adapter, timeout, output
budgets, serialized programs and the repair bound in several places, and again — differently —
between the dry-run preview (:mod:`pipeline_core.runner_cli`) and real execution
(:mod:`pipeline_core.execution`). That split is the "split-brain" defect this phase closes
(``docs/adr/006``).

:class:`CompiledRunPlan` is the single immutable value that resolves *every* execution
decision once, before any side effect. :class:`ResolvedTask` carries the fully-resolved
per-task decision; each accepted operational control appears exactly once, carrying its
:class:`~feature_pipeline.domain.controls.ResolvedControl` provenance (CP-01 AC-1). The
compiler that builds it is :func:`feature_pipeline.application.compile_plan.compile_run_plan`;
CP-02 makes dry-run and execute consume the same object.

The domain knows nothing about processes, the filesystem, or CLI exit codes. Every rejection
is a :class:`~feature_pipeline.domain.errors.DomainError`.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterator

from .controls import ResolvedControl
from .errors import DomainError
from .identifiers import FeatureId
from .paths import RelativeGlob, RelativePath
from .repair import RepairBound


class PlanCompilationError(DomainError):
    """Base class for a fail-closed compiled-plan rejection."""

    code = "plan-compilation-error"


class ConflictingStorage(PlanCompilationError):
    """Selected tasks resolve to more than one run-storage root — a run has exactly one."""

    code = "conflicting-task-storage"


class UnsupportedControl(PlanCompilationError):
    """An operational control value is outside its accepted domain."""

    code = "unsupported-control"


@dataclass(frozen=True)
class ResolvedCheck:
    """One verification command, resolved from the profile: name, argv, working directory."""

    name: str
    argv: tuple[str, ...]
    cwd: RelativePath


@dataclass(frozen=True)
class ResolvedTask:
    """Every execution decision for one task, resolved once."""

    task_id: str
    task_type: str
    executor: str
    working_root: RelativePath
    storage_root: RelativePath
    role: str
    role_grant: tuple[str, ...]
    adapter: str
    capabilities: tuple[str, ...]
    checks: tuple[ResolvedCheck, ...]
    timeout_s: float
    routine_output_budget: int
    diagnostic_output_budget: int
    serialized_programs: tuple[str, ...]
    repair_bound: RepairBound
    depends_on: tuple[str, ...]
    allowed_scope: tuple[RelativeGlob, ...]
    out_of_scope: tuple[RelativeGlob, ...]
    #: Every per-task operational control, each carrying its source (CP-01 AC-1).
    controls: tuple[ResolvedControl[object], ...]

    def control(self, name: str) -> ResolvedControl[object]:
        """The per-task control named ``name`` (``KeyError`` if this task has no such control)."""
        for resolved in self.controls:
            if resolved.name == name:
                return resolved
        raise KeyError(name)


@dataclass(frozen=True)
class CompiledRunPlan:
    """The authoritative, immutable plan every mode consumes (CP-02)."""

    feature: FeatureId
    project: str
    selection_mode: str
    selection: tuple[str, ...]
    order: tuple[str, ...]
    tasks: tuple[ResolvedTask, ...]
    adapter: str
    #: Every run-level operational control, each carrying its source (CP-01 AC-1).
    controls: tuple[ResolvedControl[object], ...]

    def __iter__(self) -> Iterator[ResolvedTask]:
        return iter(self.tasks)

    def task(self, task_id: str) -> ResolvedTask:
        """The resolved task with id ``task_id`` (``KeyError`` if it is not in the selection)."""
        for resolved in self.tasks:
            if resolved.task_id == task_id:
                return resolved
        raise KeyError(task_id)

    def control(self, name: str) -> ResolvedControl[object]:
        """The run-level control named ``name`` (``KeyError`` if absent)."""
        for resolved in self.controls:
            if resolved.name == name:
                return resolved
        raise KeyError(name)

    def control_records(self) -> dict[str, dict[str, object]]:
        """The ``{name: {"value", "sourced"}}`` shape ``run.json`` persists per control."""
        return {resolved.name: resolved.as_record() for resolved in self.controls}

    @property
    def digest(self) -> str:
        """A deterministic SHA-256 over every resolved field.

        Two compilations of the same inputs produce the same digest; any change to the
        selection, a route, a control value or its provenance changes it. CP-02 persists this
        for resume compatibility.
        """
        payload = json.dumps(_canonical_plan(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_control(resolved: ResolvedControl[object]) -> dict[str, object]:
    return {"name": resolved.name, "value": resolved.value, "sourced": str(resolved.source)}


def _canonical_check(check: ResolvedCheck) -> dict[str, object]:
    return {"name": check.name, "argv": list(check.argv), "cwd": str(check.cwd)}


def _canonical_task(task: ResolvedTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "executor": task.executor,
        "working_root": str(task.working_root),
        "storage_root": str(task.storage_root),
        "role": task.role,
        "role_grant": list(task.role_grant),
        "adapter": task.adapter,
        "capabilities": list(task.capabilities),
        "checks": [_canonical_check(check) for check in task.checks],
        "timeout_s": task.timeout_s,
        "routine_output_budget": task.routine_output_budget,
        "diagnostic_output_budget": task.diagnostic_output_budget,
        "serialized_programs": list(task.serialized_programs),
        "repair_bound": int(task.repair_bound),
        "depends_on": list(task.depends_on),
        "allowed_scope": [str(glob) for glob in task.allowed_scope],
        "out_of_scope": [str(glob) for glob in task.out_of_scope],
        "controls": [_canonical_control(control) for control in task.controls],
    }


def _canonical_plan(plan: CompiledRunPlan) -> dict[str, object]:
    return {
        "feature": str(plan.feature),
        "project": plan.project,
        "selection_mode": plan.selection_mode,
        "selection": list(plan.selection),
        "order": list(plan.order),
        "adapter": plan.adapter,
        "controls": [_canonical_control(control) for control in plan.controls],
        "tasks": [_canonical_task(task) for task in plan.tasks],
    }


__all__ = [
    "PlanCompilationError",
    "ConflictingStorage",
    "UnsupportedControl",
    "ResolvedCheck",
    "ResolvedTask",
    "CompiledRunPlan",
]
