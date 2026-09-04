"""Run a read-only Git inspection and report its outcome explicitly.

BL-02 finding F-06's companion hazard: a caller that collapses a policy denial, a failed
``git`` invocation, or a non-zero exit into ``(ok=False, "")`` — or worse, into an empty
*successful* result — presents missing evidence as if the working tree were simply clean.
:func:`inspect` never does that: a denial, a launch failure, and a non-zero exit each yield
``available=False`` with a stated ``reason``; ``available=True`` is returned only for a real
exit-zero run, and it always carries that run's stdout.

Standard library only.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .safety import GitPolicyError, parse_read_only_git


@dataclass(frozen=True)
class GitInspection:
    """The outcome of one read-only Git inspection.

    ``available`` is true only when Git ran and exited zero. When it is false, ``reason``
    states why — a policy denial, a launch failure, or a non-zero exit — and is never empty.
    """

    available: bool
    stdout: str
    reason: str | None = None
    returncode: int | None = None


class _GitInvoker(Protocol):
    def __call__(
        self, argv: Sequence[str], cwd: Path, timeout: float
    ) -> "subprocess.CompletedProcess[str]": ...


def _default_invoker(
    argv: Sequence[str], cwd: Path, timeout: float
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        timeout=timeout,
        check=False,
    )


def inspect(
    repo_root: str | Path,
    argv: Sequence[str],
    *,
    timeout: float = 120.0,
    invoker: _GitInvoker | None = None,
) -> GitInspection:
    """Parse ``argv`` through the read-only allowlist and run it in ``repo_root``.

    Returns a :class:`GitInspection`. A :class:`GitPolicyError`, an :class:`OSError` from the
    launch, a timeout, or a non-zero exit all produce ``available=False`` with a reason — the
    evidence gap is stated, never silently emptied.
    """
    run = invoker or _default_invoker
    try:
        parsed = parse_read_only_git(argv)
    except GitPolicyError as denial:
        return GitInspection(
            available=False,
            stdout="",
            reason=f"git inspection denied by policy ({denial.code}): {denial}",
        )

    try:
        completed = run(parsed.argv, Path(repo_root), timeout)
    except subprocess.TimeoutExpired:
        return GitInspection(
            available=False,
            stdout="",
            reason=f"git {parsed.operation} did not complete within {timeout:g}s",
        )
    except OSError as exc:
        return GitInspection(
            available=False,
            stdout="",
            reason=f"git {parsed.operation} could not be launched: {exc}",
        )

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "no diagnostic output"
        return GitInspection(
            available=False,
            stdout=completed.stdout or "",
            reason=f"git {parsed.operation} exited {completed.returncode}: {tail}",
            returncode=completed.returncode,
        )

    return GitInspection(
        available=True,
        stdout=completed.stdout or "",
        reason=None,
        returncode=0,
    )
