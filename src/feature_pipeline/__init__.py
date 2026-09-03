"""Installable namespace for the portable feature-pipeline core.

This package is the migration target defined by
``docs/adr/002-installable-package-layout.md``. It is introduced by **PKG-02** as a thin
**compatibility facade**: the behaviour still lives in the flat ``pipeline_core`` package
and is moved here one coherent module group at a time. The first group to land is the
project-neutral schema contracts, now at :mod:`feature_pipeline.contracts` (previously the
collision-prone top-level ``schemas`` package, kept working for one deprecation window via a
shim — ``docs/adr/007``).

Public API
----------
Only the names in :data:`__all__` are supported for import from ``feature_pipeline``
directly. Everything under ``pipeline_core`` remains an internal implementation detail while
the migration is in progress and must not be imported by out-of-tree callers.
"""

from __future__ import annotations

from . import contracts
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
    "contracts",
    "AcceptanceCriterionSpec",
    "CommandSpec",
    "LogicalPaths",
    "Profile",
    "ProfileRegistry",
    "RunState",
    "SchemaError",
    "TaskMetadata",
    "TaskRoute",
    "TaskSpec",
    "ToolStage",
    "Verdict",
    "ensure_no_role_escalation",
    "load_profile",
    "validate_acceptance_criteria",
    "validate_relative_path",
]
