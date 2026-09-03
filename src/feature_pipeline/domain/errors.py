"""Typed domain failures for the feature-pipeline core.

Every rejection raised while constructing a domain value object is a :class:`DomainError`
carrying a stable, machine-readable ``code`` slug. The application layer maps a
:class:`DomainError` to a CLI exit code in exactly one place
(:class:`feature_pipeline.application.results.ResultMapper`); nothing in the domain knows
about process exit codes.

These types are introduced by **DF-01**
(``docs/plans/tasks/DF-01_typed-vocabulary-paths-errors.md``). They mirror the failure
semantics that ``pipeline_core``/``feature_pipeline.contracts`` already enforce with bare
``SchemaError``/``ValueError`` raises and ad-hoc strings, so that a later slice can migrate a
call site without changing which inputs are accepted.

Standard library only.
"""

from __future__ import annotations


class DomainError(ValueError):
    """Base class for every fail-closed domain rejection.

    Subclasses fix ``code`` to a stable slug. ``code`` is part of the contract: it is safe to
    branch on and to record in evidence, and it never leaks a host path or a secret.
    """

    #: Stable, kebab-case identifier for the failure category.
    code: str = "domain-error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return super().__str__()


class InvalidIdentifier(DomainError):
    """A task ID or feature ID does not match its required shape."""

    code = "invalid-identifier"


class InvalidPath(DomainError):
    """A repository-relative path is absolute, traverses its anchor, or is malformed."""

    code = "invalid-path"


class InvalidGlob(DomainError):
    """A repository-relative glob is malformed or uses an unsupported construct."""

    code = "invalid-glob"


class PathNotContained(DomainError):
    """A path resolves outside the anchor it was required to stay within."""

    code = "path-not-contained"


class InvalidArgv(DomainError):
    """An argv is empty, has a non-string token, or contains a shell operator."""

    code = "invalid-argv"


class InvalidTerm(DomainError):
    """A string is not a member of a closed domain vocabulary (status, verdict, ...)."""

    code = "invalid-term"


__all__ = [
    "DomainError",
    "InvalidIdentifier",
    "InvalidPath",
    "InvalidGlob",
    "PathNotContained",
    "InvalidArgv",
    "InvalidTerm",
]
