"""Read-only migration adapter for legacy task metadata.

Historical task files predate the ``## Execution Metadata`` block. The contract for them is
"extend, do not convert": they stay executable through documented defaults and are never
rewritten just to look uniform. This adapter is the portable form of that rule — it takes an
already-parsed task mapping and returns a normalized :class:`AdaptedTask`, applying
caller-supplied defaults only when the file declared no metadata block.

It performs no file I/O and mutates nothing it is given: the "without rewriting" guarantee is
structural, not a convention. The default *values* (type, executor, checks) are supplied by the
project through :class:`LegacyDefaults`; this module has no per-project inference table.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from schemas.contracts import (
    AcceptanceCriterionSpec,
    CommandSpec,
    SchemaError,
    TaskSpec,
)


class LegacyAdapterError(ValueError):
    """A legacy task mapping that cannot be adapted without guessing."""


#: Fields the contract still lets a *declared* task omit; every other default is historical-only.
DECLARED_DEFAULTABLE = ("max_repair_attempts", "blocking_conditions")


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    text: str
    checked: bool = False


@dataclass(frozen=True)
class LegacyDefaults:
    """Project-supplied defaults for a task file that carries no metadata block."""

    task_type: str
    executor: str
    max_repair_attempts: int = 2
    out_of_scope: tuple[str, ...] = ()
    documentation_impact: tuple[str, ...] = ()
    verification_commands: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class AdaptedTask:
    """One legacy task normalized in memory. Nothing here is written back to disk."""

    task_id: str
    title: str
    task_type: str
    executor: str
    depends_on: tuple[str, ...]
    allowed_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    max_repair_attempts: int
    documentation_impact: tuple[str, ...]
    verification_commands: tuple[Mapping[str, object], ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    blocking_conditions: str | None
    metadata_source: str
    defaults_applied: tuple[str, ...] = field(default_factory=tuple)


def synthesize_acceptance_criteria(items: Sequence[object]) -> tuple[AcceptanceCriterion, ...]:
    """Assign synthetic ``AC-1 … AC-n`` IDs to ordered ``Definition of Done`` entries.

    Each entry is either a string (unchecked) or a ``{"text": ..., "checked": ...}`` mapping.
    The numbering is in-memory only; the source file is not touched.
    """
    criteria: list[AcceptanceCriterion] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, Mapping):
            text = str(item.get("text", "")).strip()
            checked = bool(item.get("checked", False))
        else:
            text, checked = str(item).strip(), False
        if not text:
            raise LegacyAdapterError(f"definition-of-done entry {index} has no text")
        criteria.append(AcceptanceCriterion(f"AC-{index}", text, checked))
    return tuple(criteria)


def _tuple(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise LegacyAdapterError(f"expected a list, got {type(value).__name__}")
    return tuple(value)


def adapt_legacy_task(raw: Mapping[str, object], defaults: LegacyDefaults) -> AdaptedTask:
    """Normalize one parsed task mapping, applying ``defaults`` only for a block-less file.

    ``raw`` is read, never modified. A file that declared an ``## Execution Metadata`` block
    (``raw["has_metadata"]`` truthy) keeps every declared value and may only default
    ``max_repair_attempts`` / ``blocking_conditions``; a historical file takes the full default
    set and reports each key it used in ``defaults_applied``.
    """
    task_id = str(raw.get("id") or "").strip()
    if not task_id:
        raise LegacyAdapterError("legacy task mapping has no 'id'")

    has_metadata = bool(raw.get("has_metadata"))
    applied: list[str] = []

    def resolve(key: str, declared: object, default: object) -> object:
        if declared is not None:
            return declared
        if has_metadata and key not in DECLARED_DEFAULTABLE:
            raise LegacyAdapterError(
                f"{task_id}: declared metadata is missing required field '{key}'"
            )
        applied.append(key)
        return default

    task_type = str(resolve("type", raw.get("type"), defaults.task_type))
    executor = str(resolve("executor", raw.get("executor"), defaults.executor))
    allowed_scope = _tuple(resolve("allowed_scope", raw.get("allowed_scope"),
                                   raw.get("affected_files")))
    if not allowed_scope:
        raise LegacyAdapterError(f"{task_id}: no allowed scope could be resolved")
    out_of_scope = _tuple(resolve("out_of_scope", raw.get("out_of_scope"), defaults.out_of_scope))
    documentation_impact = _tuple(resolve("documentation_impact", raw.get("documentation_impact"),
                                          defaults.documentation_impact))
    verification_commands = _tuple(resolve("verification_commands", raw.get("verification_commands"),
                                           defaults.verification_commands))
    max_repair_attempts = int(resolve("max_repair_attempts", raw.get("max_repair_attempts"),
                                      defaults.max_repair_attempts))

    depends_on = _tuple(raw.get("depends_on"))
    blocking_conditions = raw.get("blocking_conditions")
    if blocking_conditions is not None:
        blocking_conditions = str(blocking_conditions)

    criteria_source = raw.get("acceptance_criteria")
    if criteria_source is not None:
        acceptance_criteria = tuple(
            AcceptanceCriterion(str(c["id"]), str(c["text"]).strip(), bool(c.get("checked", False)))
            for c in criteria_source
        )
    else:
        acceptance_criteria = synthesize_acceptance_criteria(_tuple(raw.get("definition_of_done")))
        if not has_metadata:
            applied.append("acceptance_criteria")

    return AdaptedTask(
        task_id=task_id,
        title=str(raw.get("title") or "").strip(),
        task_type=task_type,
        executor=executor,
        depends_on=depends_on,
        allowed_scope=allowed_scope,
        out_of_scope=out_of_scope,
        max_repair_attempts=max_repair_attempts,
        documentation_impact=documentation_impact,
        verification_commands=verification_commands,
        acceptance_criteria=acceptance_criteria,
        blocking_conditions=blocking_conditions,
        metadata_source="declared" if has_metadata else "defaults",
        defaults_applied=tuple(applied),
    )


def adapt_task_spec(
    raw: Mapping[str, object], defaults: "LegacyDefaults | None" = None
) -> TaskSpec:
    """Normalize a parsed JSON plan entry (or any parsed task mapping) into a ``TaskSpec``.

    This is the JSON counterpart of :func:`pipeline_core.task_files.load_task_spec`: a Markdown
    task file and a JSON plan entry describing the same task land on an equivalent ``TaskSpec``.
    An entry with ``has_metadata`` false and no ``defaults`` fails closed — real execution never
    runs a task whose execution metadata is neither declared nor supplied. ``raw`` is read,
    never modified.
    """
    task_id = str(raw.get("id") or "").strip()
    has_metadata = bool(raw.get("has_metadata"))
    if not has_metadata and defaults is None:
        raise SchemaError(
            f"{task_id or '<task>'}: execution metadata is absent and no defaults were supplied"
        )

    effective = defaults or LegacyDefaults(task_type="", executor="")
    try:
        adapted = adapt_legacy_task(raw, effective)
    except LegacyAdapterError as exc:
        raise SchemaError(str(exc)) from None

    def _commands(value: object) -> tuple[CommandSpec, ...]:
        if not value:
            return ()
        return tuple(CommandSpec.from_data(item) for item in value)  # type: ignore[union-attr]

    try:
        return TaskSpec.build(
            id=adapted.task_id,
            title=adapted.title,
            path=str(raw.get("path") or ""),
            task_type=adapted.task_type,
            executor=adapted.executor,
            depends_on=tuple(adapted.depends_on),
            allowed_scope=tuple(adapted.allowed_scope),
            out_of_scope=tuple(adapted.out_of_scope),
            required_skills=tuple(raw.get("required_skills") or ()),
            max_repair_attempts=adapted.max_repair_attempts,
            documentation_impact=tuple(adapted.documentation_impact),
            verification_commands=_commands(adapted.verification_commands),
            verification_tier=str(raw.get("verification_tier") or "full"),
            accepts_scoped=tuple(raw.get("accepts_scoped") or ()),
            deferred_verification_commands=_commands(raw.get("deferred_verification_commands")),
            blocking_conditions=adapted.blocking_conditions,
            acceptance_criteria=tuple(
                AcceptanceCriterionSpec(item.id, item.text, item.checked)
                for item in adapted.acceptance_criteria
            ),
            metadata_source=adapted.metadata_source,
            defaults_applied=tuple(adapted.defaults_applied),
        )
    except SchemaError:
        raise
    except LegacyAdapterError as exc:
        raise SchemaError(str(exc)) from None
