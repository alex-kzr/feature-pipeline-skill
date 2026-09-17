"""Fail-closed WorkItem registry for executor and verifier launch attribution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Mapping, Sequence

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.work_items import ProducerDescriptor, WorkItem
from pipeline_core.state import Run


class WorkItemError(RuntimeError):
    """Raised when a launch has no registered, active producer WorkItem."""

    code = "unregistered-producer"


_INVENTORY: dict[tuple[str, str], dict[str, WorkItem]] = {}
_DESCRIPTORS: dict[tuple[str, str], dict[str, ProducerDescriptor]] = {}
_ACTIVE: ContextVar[WorkItem | None] = ContextVar("active_pipeline_work_item", default=None)


def register_work_items(
    run: Run,
    specs: Sequence[TaskSpec],
    *,
    descriptors: Mapping[str, ProducerDescriptor] | None = None,
) -> tuple[WorkItem, ...]:
    """Register complete, versioned producer contracts for ``run`` once.

    The descriptor inventory is deliberately process-local: the task contract digest and
    all execution evidence remain in ``Run``'s one durable state store.  On resume this
    function rebuilds descriptors from that durable contract rather than creating another
    ledger.
    """
    key = (str(run.run_dir), run.run_id)
    inventory = _INVENTORY.setdefault(key, {})
    registered_descriptors = _DESCRIPTORS.setdefault(key, {})
    for spec in specs:
        snapshot = run.task(spec.id).task_contract_digest
        item = WorkItem.from_spec(run.run_id, spec, snapshot=snapshot)
        descriptor = (descriptors or {}).get(spec.id) or ProducerDescriptor(
            name=spec.executor,
            emitted_kind=item.kind,
            schema_revision=item.schema_revision,
        )
        try:
            descriptor.require_compatible(item)
        except ValueError as exc:
            raise WorkItemError(str(exc)) from None
        prior = inventory.get(spec.id)
        if prior is not None and (prior.run_id, prior.task_id, prior.parent_id) != (
            item.run_id, item.task_id, item.parent_id,
        ):
            raise WorkItemError(f"producer identity changed for {spec.id}")
        inventory[spec.id] = item
        registered_descriptors[spec.id] = descriptor
    items = tuple(inventory[spec.id] for spec in specs)
    return items


@contextmanager
def activate_work_item(run: Run, task_id: str) -> Iterator[WorkItem]:
    """Make one registered WorkItem the sole producer for nested launch calls."""
    item = _INVENTORY.get((str(run.run_dir), run.run_id), {}).get(task_id)
    if item is None:
        raise WorkItemError(f"unregistered producer WorkItem for {task_id}")
    descriptor = _DESCRIPTORS.get((str(run.run_dir), run.run_id), {}).get(task_id)
    if descriptor is None:
        raise WorkItemError(f"unregistered producer descriptor for {task_id}")
    try:
        descriptor.require_compatible(item)
    except ValueError as exc:
        raise WorkItemError(str(exc)) from None
    token = _ACTIVE.set(item)
    try:
        yield item
    finally:
        _ACTIVE.reset(token)


def require_active_work_item(run: Run, task_id: str) -> WorkItem:
    """Return the matching active producer, denying direct or cross-task launches."""
    item = _ACTIVE.get()
    registered = _INVENTORY.get((str(run.run_dir), run.run_id), {}).get(task_id)
    descriptor = _DESCRIPTORS.get((str(run.run_dir), run.run_id), {}).get(task_id)
    if item is None or registered is None or descriptor is None:
        raise WorkItemError(f"unregistered producer WorkItem for {task_id}")
    if item != registered:
        raise WorkItemError(f"inactive producer WorkItem for {task_id}")
    try:
        descriptor.require_compatible(item)
    except ValueError as exc:
        raise WorkItemError(str(exc)) from None
    return item


__all__ = [
    "WorkItemError", "activate_work_item", "register_work_items", "require_active_work_item",
]
