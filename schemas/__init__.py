"""Schema contracts used by the portable feature-pipeline core."""

from .contracts import (
    AcceptanceCriterionSpec,
    CommandSpec,
    LogicalPaths,
    Profile,
    ProfileRegistry,
    RunState,
    SchemaError,
    TaskMetadata,
    TaskRoute,
    TaskSpec,
    ToolStage,
    Verdict,
    ensure_no_role_escalation,
    load_profile,
    validate_acceptance_criteria,
    validate_relative_path,
)

__all__ = [
    "AcceptanceCriterionSpec", "CommandSpec", "LogicalPaths", "Profile", "ProfileRegistry",
    "RunState", "SchemaError", "TaskMetadata", "TaskRoute", "TaskSpec", "ToolStage",
    "Verdict", "ensure_no_role_escalation", "load_profile", "validate_acceptance_criteria",
    "validate_relative_path",
]
