"""The one fail-closed CLI failure type.

Every parsing, resolution, routing, or gate rejection the CLI boundary raises carries a
stable exit code and a message already safe to print (the caller still redacts it, since a
message may still name a logical path). No renderer or use case invents its own ad hoc
exception; this is the single carrier :func:`pipeline_core.runner_cli.main` catches.

Standard library only.
"""

from __future__ import annotations


class CliError(Exception):
    """A fail-closed CLI failure carrying a stable exit code and a leak-free message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["CliError"]
