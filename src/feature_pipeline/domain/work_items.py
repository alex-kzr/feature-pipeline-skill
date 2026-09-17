"""Revisioned, immutable operation contracts and producer descriptors."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from feature_pipeline.contracts import TaskSpec


def _frozen(values: Mapping[str, object] | None = None) -> Mapping[str, object]:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True)
class WorkItem:
    """The complete, revisioned contract attributed to one producer operation."""

    run_id: str
    task_id: str
    parent_id: str = ""
    operation_id: str = ""
    schema_revision: int = 1
    kind: str = ""
    stack: str = ""
    role: str = ""
    goal: str = ""
    inputs: Mapping[str, object] = field(default_factory=_frozen)
    outputs: Mapping[str, object] = field(default_factory=_frozen)
    scope: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    checks: tuple[object, ...] = ()
    characteristics: Mapping[str, object] = field(default_factory=_frozen)
    provenance: Mapping[str, object] = field(default_factory=_frozen)
    context: Mapping[str, object] = field(default_factory=_frozen)
    budgets: Mapping[str, object] = field(default_factory=_frozen)
    freshness: str = ""
    snapshot: str | None = None
    generation: int = 1

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        object.__setattr__(self, "parent_id", self.parent_id or self.task_id)
        object.__setattr__(
            self, "operation_id",
            self.operation_id or f"{self.run_id}:{self.task_id}:executor:{self.generation}",
        )
        for name in ("inputs", "outputs", "characteristics", "provenance", "context", "budgets"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))

    @classmethod
    def from_spec(
        cls, run_id: str, spec: "TaskSpec", *, generation: int = 1, snapshot: str | None = None
    ) -> "WorkItem":
        """Build the canonical operation contract from a normalized task contract."""
        return cls(
            run_id=run_id, task_id=spec.id, parent_id=spec.id,
            operation_id=f"{run_id}:{spec.id}:executor:{generation}",
            kind=spec.task_type, stack=spec.task_type, role=spec.executor, goal=spec.title,
            inputs=_frozen({"depends_on": spec.depends_on, "preconditions": spec.preconditions}),
            outputs=_frozen({"documentation_impact": spec.documentation_impact}),
            scope=spec.allowed_scope, skills=spec.required_skills,
            checks=spec.verification_commands,
            characteristics=_frozen({"verification_tier": spec.verification_tier,
                                     "accepts_scoped": spec.accepts_scoped,
                                     "acceptance_criteria": spec.acceptance_criteria}),
            provenance=_frozen({"path": spec.path, "metadata_source": spec.metadata_source,
                                "defaults_applied": spec.defaults_applied}),
            context=_frozen({"out_of_scope": spec.out_of_scope,
                             "blocking_conditions": spec.blocking_conditions}),
            budgets=_frozen({"max_repair_attempts": spec.max_repair_attempts}),
            freshness=spec.metadata_source, snapshot=snapshot, generation=generation,
        )

    @property
    def producer_id(self) -> str:
        return f"{self.run_id}:{self.task_id}"


@dataclass(frozen=True)
class ProducerDescriptor:
    """The versioned declaration a live producer must satisfy before launch."""

    name: str
    emitted_kind: str
    schema_revision: int
    active: bool = True

    def require_compatible(self, item: WorkItem) -> None:
        if not self.active:
            raise ValueError(f"inactive producer descriptor '{self.name}'")
        if self.schema_revision != item.schema_revision or self.emitted_kind != item.kind:
            raise ValueError(f"incompatible producer descriptor '{self.name}'")


__all__ = ["ProducerDescriptor", "WorkItem"]
