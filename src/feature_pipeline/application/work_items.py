"""Fail-closed WorkItem registry for executor and verifier launch attribution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Sequence

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.work_items import WorkItem
from pipeline_core.state import Run


class WorkItemError(RuntimeError):
    """Raised when a launch has no registered, active producer WorkItem."""

    code = "unregistered-producer"


_INVENTORY: dict[tuple[str, str], dict[str, WorkItem]] = {}
_ACTIVE: ContextVar[WorkItem | None] = ContextVar("active_pipeline_work_item", default=None)


def register_work_items(run: Run, specs: Sequence[TaskSpec]) -> tuple[WorkItem, ...]:
    """Register the complete launch-producer inventory for ``run`` once."""
    inventory = _INVENTORY.setdefault((str(run.run_dir), run.run_id), {})
    for spec in specs:
        item = WorkItem(run.run_id, spec.id)
        prior = inventory.get(spec.id)
        if prior is not None and prior != item:
            raise WorkItemError(f"producer identity changed for {spec.id}")
        inventory[spec.id] = item
    items = tuple(inventory[spec.id] for spec in specs)
    return items


@contextmanager
def activate_work_item(run: Run, task_id: str) -> Iterator[WorkItem]:
    """Make one registered WorkItem the sole producer for nested launch calls."""
    item = _INVENTORY.get((str(run.run_dir), run.run_id), {}).get(task_id)
    if item is None:
        raise WorkItemError(f"unregistered producer WorkItem for {task_id}")
    token = _ACTIVE.set(item)
    try:
        yield item
    finally:
        _ACTIVE.reset(token)


def require_active_work_item(run: Run, task_id: str) -> WorkItem:
    """Return the matching active producer, denying direct or cross-task launches."""
    item = _ACTIVE.get()
    registered = _INVENTORY.get((str(run.run_dir), run.run_id), {}).get(task_id)
    if item is None or registered is None:
        raise WorkItemError(f"unregistered producer WorkItem for {task_id}")
    if item != registered:
        raise WorkItemError(f"inactive producer WorkItem for {task_id}")
    return item


__all__ = [
    "WorkItemError", "activate_work_item", "register_work_items", "require_active_work_item",
]
