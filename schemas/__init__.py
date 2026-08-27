"""Schema contracts used by the portable feature-pipeline core."""

from .contracts import (
    LogicalPaths,
    Profile,
    RunState,
    SchemaError,
    TaskMetadata,
    ToolStage,
    Verdict,
    ensure_no_role_escalation,
    load_profile,
    validate_relative_path,
)

__all__ = [
    "LogicalPaths", "Profile", "RunState", "SchemaError", "TaskMetadata", "ToolStage",
    "Verdict", "ensure_no_role_escalation", "load_profile", "validate_relative_path",
]
