"""Portable read-only Git safety: a parsed allowlist and an explicit-outcome inspector.

See :mod:`feature_pipeline.infrastructure.git.safety` for the allowlist (ADR 007 §7,
BL-02 F-06) and :mod:`feature_pipeline.infrastructure.git.inspector` for running one
inspection without ever presenting missing evidence as a clean result.
"""

from __future__ import annotations

from .inspector import GitInspection, inspect
from .safety import (
    CODE_DISALLOWED_ARGUMENT,
    CODE_EMPTY_ARGV,
    CODE_LEADING_OPTION,
    CODE_MALFORMED_ARGUMENT,
    CODE_UNKNOWN_OPERATION,
    READ_ONLY_OPERATIONS,
    GitPolicyError,
    OperationSchema,
    ParsedGitCommand,
    parse_read_only_git,
)

__all__ = [
    "CODE_DISALLOWED_ARGUMENT",
    "CODE_EMPTY_ARGV",
    "CODE_LEADING_OPTION",
    "CODE_MALFORMED_ARGUMENT",
    "CODE_UNKNOWN_OPERATION",
    "READ_ONLY_OPERATIONS",
    "GitInspection",
    "GitPolicyError",
    "OperationSchema",
    "ParsedGitCommand",
    "inspect",
    "parse_read_only_git",
]
