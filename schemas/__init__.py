"""Deprecated import shim for the pre-move top-level ``schemas`` package.

The contracts moved to :mod:`feature_pipeline.contracts` in PKG-02
(``docs/adr/002-installable-package-layout.md``). This package keeps ``import schemas``,
``from schemas import <name>`` and ``from schemas.contracts import <name>`` resolving to the
**same** objects for exactly one deprecation window; its removal is gated on the
repository-wide import scan in DOC-02
(``docs/adr/007-compatibility-and-versioning-policy.md``).

New code must import from ``feature_pipeline.contracts``.
"""

from __future__ import annotations

import warnings as _warnings

from feature_pipeline.contracts import (
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

_warnings.warn(
    "The top-level 'schemas' package is a temporary compatibility shim; "
    "import from 'feature_pipeline.contracts' instead.",
    DeprecationWarning,
    stacklevel=2,
)
