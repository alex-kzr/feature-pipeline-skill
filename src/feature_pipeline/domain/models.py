"""The input-neutral task definition.

Every supported plan source — a native Markdown task file, a JSON plan entry, a historical
block-less file, a parsed legacy mapping — normalizes to one immutable
:class:`TaskDefinition`. It carries the already-validated execution contract (as a
:class:`feature_pipeline.contracts.TaskSpec`) plus the provenance a caller needs to reason
about compatibility: which source shape produced it, that shape's version, and any field the
source carried that this version deliberately ignores.

Construction lives in :class:`feature_pipeline.inputs.TaskDefinitionBuilder` — the single
builder every source goes through — so validation (fail-closed, before any run artifact
exists) happens in exactly one place.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from feature_pipeline.contracts import (
    AcceptanceCriterionSpec,
    CommandSpec,
    TaskSpec,
)

#: Source shapes the builder accepts. The value is stable — it is recorded on the definition
#: and may be branched on.
MARKDOWN_TASK_FILE = "markdown_task_file"
JSON_PLAN_ENTRY = "json_plan_entry"

SOURCE_FORMATS = frozenset({MARKDOWN_TASK_FILE, JSON_PLAN_ENTRY})

#: The current normalized-contract version. A source whose own version is unknown to this
#: value fails closed in the builder rather than being partially accepted.
DEFINITION_VERSION = 1

#: Fields compared by :attr:`TaskDefinition.semantic`. Two source documents describing the
#: same task agree here even when their file path or title differ. ``defaults_applied`` is
#: deliberately excluded: it is a provenance label whose spelling still follows the source
#: shape (a Markdown historical file records ``"task_type"``, a legacy mapping ``"type"``),
#: and unifying it would change a shipped facade's accepted output — ``metadata_source``
#: already captures the compatibility-relevant fact that defaults were applied.
_SEMANTIC_FIELDS: tuple[str, ...] = (
    "id",
    "task_type",
    "executor",
    "depends_on",
    "allowed_scope",
    "out_of_scope",
    "required_skills",
    "max_repair_attempts",
    "documentation_impact",
    "verification_commands",
    "verification_tier",
    "accepts_scoped",
    "deferred_verification_commands",
    "blocking_conditions",
    "acceptance_criteria",
    "metadata_source",
)


@dataclass(frozen=True)
class TaskDefinition:
    """One normalized task, independent of the source document that produced it."""

    spec: TaskSpec
    source_format: str
    source_version: int = DEFINITION_VERSION
    unknown_fields: tuple[str, ...] = ()

    # --- passthrough to the validated execution contract ---------------------------------

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def source_path(self) -> str:
        return self.spec.path

    @property
    def task_type(self) -> str:
        return self.spec.task_type

    @property
    def executor(self) -> str:
        return self.spec.executor

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.spec.depends_on

    @property
    def allowed_scope(self) -> tuple[str, ...]:
        return self.spec.allowed_scope

    @property
    def out_of_scope(self) -> tuple[str, ...]:
        return self.spec.out_of_scope

    @property
    def required_skills(self) -> tuple[str, ...]:
        return self.spec.required_skills

    @property
    def max_repair_attempts(self) -> int:
        return self.spec.max_repair_attempts

    @property
    def documentation_impact(self) -> tuple[str, ...]:
        return self.spec.documentation_impact

    @property
    def verification_commands(self) -> tuple[CommandSpec, ...]:
        return self.spec.verification_commands

    @property
    def verification_tier(self) -> str:
        return self.spec.verification_tier

    @property
    def accepts_scoped(self) -> tuple[str, ...]:
        return self.spec.accepts_scoped

    @property
    def deferred_verification_commands(self) -> tuple[CommandSpec, ...]:
        return self.spec.deferred_verification_commands

    @property
    def blocking_conditions(self) -> str | None:
        return self.spec.blocking_conditions

    @property
    def acceptance_criteria(self) -> tuple[AcceptanceCriterionSpec, ...]:
        return self.spec.acceptance_criteria

    @property
    def metadata_source(self) -> str:
        return self.spec.metadata_source

    @property
    def defaults_applied(self) -> tuple[str, ...]:
        return self.spec.defaults_applied

    # --- normalized comparison + bridge --------------------------------------------------

    @property
    def semantic(self) -> tuple[object, ...]:
        """The fields two equivalent source documents must agree on (AC-1).

        Excludes the source file path, the title, and the source provenance — a Markdown
        task file and the JSON plan entry for the same task carry different paths but the
        same normalized execution contract.
        """
        return tuple(getattr(self, name) for name in _SEMANTIC_FIELDS)

    def to_task_spec(self) -> TaskSpec:
        """The validated :class:`~feature_pipeline.contracts.TaskSpec` for this task.

        The shipped entry points (``pipeline_core.task_files.load_task_spec``,
        ``pipeline_core.legacy_adapter.adapt_task_spec``) return exactly this object, so a
        caller that migrates to the builder sees byte-identical results (AC-3).
        """
        return self.spec


__all__ = [
    "TaskDefinition",
    "MARKDOWN_TASK_FILE",
    "JSON_PLAN_ENTRY",
    "SOURCE_FORMATS",
    "DEFINITION_VERSION",
]
