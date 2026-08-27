"""Restricted Git primitives that permanently deny history-rewriting commands."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DENIED_SUBCOMMANDS = frozenset({"push", "reset", "checkout", "restore", "rebase", "clean", "filter-branch", "update-ref", "remote"})
DENIED_FLAGS = frozenset({"--force", "--force-with-lease", "--no-verify", "--amend", "--hard"})


class GitSafetyError(ValueError):
    """A Git command outside the portable safety policy."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitPort:
    """Run a narrowly permitted Git argv in one repository."""

    def __init__(self, repo_root: str | Path, timeout: float = 120.0) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.timeout = timeout

    def run(self, argv: Sequence[str]) -> GitResult:
        """Run non-mutating Git argv, denying push and history-rewriting operations."""
        if not argv:
            raise GitSafetyError("git argv must not be empty")
        if argv[0] in DENIED_SUBCOMMANDS or DENIED_FLAGS.intersection(argv):
            raise GitSafetyError("git command is denied by the portable safety policy")
        result = subprocess.run(
            ["git", *argv], cwd=self.repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="surrogateescape", timeout=self.timeout, check=False,
        )
        return GitResult(result.returncode, result.stdout, result.stderr)
