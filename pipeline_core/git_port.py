"""Restricted Git primitives — a parsed read-only allowlist, not a bypassable denylist.

Every argv is parsed by
:func:`feature_pipeline.infrastructure.git.safety.parse_read_only_git` (ADR 007 §7): the
subcommand must be ``argv[0]`` itself and one of a fixed set of read-only operations, and
every remaining token must match that operation's argument schema. A leading global option, a
``-c`` alias override, an attached ``--git-dir=`` redirect, and an unrecognised subcommand all
fail closed — "the spelling was not recognised" is never a reason a mutating call runs.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from feature_pipeline.infrastructure.git.safety import (
    GitPolicyError,
    parse_read_only_git,
)


class GitSafetyError(ValueError):
    """A Git command outside the portable read-only safety policy."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitPort:
    """Run a narrowly permitted, parsed read-only Git argv in one repository."""

    def __init__(self, repo_root: str | Path, timeout: float = 120.0) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.timeout = timeout

    def run(self, argv: Sequence[str]) -> GitResult:
        """Run a read-only Git argv, denying anything outside the parsed allowlist."""
        try:
            parsed = parse_read_only_git(argv)
        except GitPolicyError as denial:
            raise GitSafetyError(
                f"git command is denied by the portable safety policy ({denial.code})"
            ) from denial
        result = subprocess.run(
            ["git", *parsed.argv], cwd=self.repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="surrogateescape", timeout=self.timeout, check=False,
        )
        return GitResult(result.returncode, result.stdout, result.stderr)
