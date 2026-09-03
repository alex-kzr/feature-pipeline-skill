"""Input normalization (IN-01).

One task contract, one builder, one metadata/command parser for every supported plan source.
``pipeline_core.task_files``, ``pipeline_core.plan_md`` and ``pipeline_core.legacy_adapter``
are delegating facades over this package
(``docs/plans/tasks/IN-01_normalized-task-definition.md``).
"""

from __future__ import annotations

from feature_pipeline.domain.models import (
    JSON_PLAN_ENTRY,
    MARKDOWN_TASK_FILE,
    TaskDefinition,
)

from .builder import SourceDefaults, TaskDefinitionBuilder
from .commands import (
    command_from_object,
    command_from_reference,
    commands_from_objects,
    commands_from_pairs,
    parse_markdown_command_line,
)
from .metadata import (
    FIELD_ALIASES,
    LIST_FIELDS,
    REQUIRED_DECLARED_FIELDS,
    SUBLIST_FIELDS,
    RawTaskMetadata,
    canonical_field,
    parse_markdown_metadata_block,
)

__all__ = [
    "TaskDefinition",
    "TaskDefinitionBuilder",
    "SourceDefaults",
    "MARKDOWN_TASK_FILE",
    "JSON_PLAN_ENTRY",
    # metadata parser
    "parse_markdown_metadata_block",
    "RawTaskMetadata",
    "canonical_field",
    "FIELD_ALIASES",
    "REQUIRED_DECLARED_FIELDS",
    "LIST_FIELDS",
    "SUBLIST_FIELDS",
    # command parser
    "parse_markdown_command_line",
    "command_from_reference",
    "command_from_object",
    "commands_from_pairs",
    "commands_from_objects",
]
