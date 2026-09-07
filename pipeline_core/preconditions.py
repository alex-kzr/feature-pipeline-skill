"""Parsing and bounded Git reads for the shared task-precondition contract."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from feature_pipeline.contracts import Precondition, SchemaError

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def parse_preconditions(text: str) -> tuple[Precondition, ...]:
    items: list[Precondition] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip().lower() == "## preconditions"
            continue
        if not inside or not line.strip():
            continue
        if not line.lstrip().startswith("- ") or ":" not in line:
            raise SchemaError("Preconditions entries must be '- <kind>: <value>'")
        kind, value = (part.strip() for part in line.strip()[2:].split(":", 1))
        item = Precondition(kind, value)
        if item in items:
            raise SchemaError(f"duplicate precondition '{item.identifier}'")
        items.append(item)
    return tuple(items)


def bind_refs(predicates: Iterable[Precondition], *, repo_root: Path,
              core_root: Path | None, run: GitRunner = subprocess.run) -> dict[str, str]:
    """Bind project HEAD and its reviewed gitlink at the explicit core anchor."""
    bindings: dict[str, str] = {}
    for source in sorted({p.value for p in predicates if p.kind == "ref-published"}):
        if source == "parent-head":
            argv = ["git", "rev-parse", "HEAD"]
        else:
            if core_root is None:
                raise ValueError("core-gitlink requires an explicit core anchor")
            relative = core_root.resolve().relative_to(repo_root.resolve()).as_posix()
            argv = ["git", "ls-tree", "HEAD", "--", relative]
        result = run(argv, cwd=repo_root, text=True, capture_output=True, check=True, timeout=15)
        if source == "core-gitlink":
            fields = result.stdout.strip().split()
            if len(fields) < 4 or fields[:2] != ["160000", "commit"]:
                raise ValueError("core anchor is not a reviewed gitlink")
            bindings[source] = fields[2]
        else:
            bindings[source] = result.stdout.strip()
        if not bindings[source]:
            raise ValueError(f"empty local binding for {source}")
    return bindings


def evaluate_preconditions(
    predicates: Iterable[Precondition], *, repo_root: Path,
    grants: Iterable[str], approvals: Iterable[str], published_refs: dict[str, str],
    expected_refs: dict[str, str] | None = None, core_root: Path | None = None,
    run: GitRunner = subprocess.run,
) -> tuple[Precondition, str] | None:
    """Return the first unmet predicate; all Git operations use the injectable read port."""
    granted, approved = set(grants), set(approvals)
    for predicate in predicates:
        if predicate.kind == "capability" and predicate.value not in granted:
            return predicate, "missing grant"
        if predicate.kind == "approval" and predicate.value not in approved:
            return predicate, "missing approval"
        if predicate.kind != "ref-published":
            continue
        ref = published_refs.get(predicate.value)
        if not ref:
            return predicate, "missing published-ref mapping"
        if not ref.startswith(("refs/heads/", "refs/tags/")):
            return predicate, "invalid published-ref mapping"
        remote_root = repo_root if predicate.value == "parent-head" else core_root
        if remote_root is None:
            return predicate, "missing explicit core anchor"
        try:
            bindings = expected_refs if expected_refs is not None else bind_refs(
                (predicate,), repo_root=repo_root, core_root=core_root, run=run,
            )
            expected = bindings.get(predicate.value)
            if not expected:
                return predicate, "missing expected SHA binding"
            result = run(
                ["git", "-c", "credential.interactive=false", "ls-remote", "--exit-code",
                 "origin", ref, f"{ref}^{{}}"],
                cwd=remote_root, text=True, capture_output=True, check=True, timeout=15,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                     "GCM_INTERACTIVE": "Never", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"},
            )
            rows = dict((parts[1], parts[0]) for line in result.stdout.splitlines()
                        if len(parts := line.split()) == 2)
            observed = rows.get(f"{ref}^{{}}", rows.get(ref))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return predicate, f"remote read failed: {exc}"
        if observed is None:
            return predicate, "missing published ref"
        if observed != expected:
            return predicate, f"expected {expected}, observed {observed}"
    return None
