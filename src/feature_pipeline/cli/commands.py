"""The typed, ``argparse``-free view of a parsed invocation.

:class:`RunCommand` is the one place that reads an :class:`argparse.Namespace` field by
name; every use case in :mod:`feature_pipeline.cli.use_cases` takes a ``RunCommand``, never
a raw ``Namespace``, so the fields it depends on — and their types — are declared once,
in the open, instead of implied by scattered ``args.foo`` reads.

``dry_run`` is intentionally mutable: ``--mode release-dry-run`` always implies ``--dry-run``,
and the use case records that once, on the typed command, rather than threading a second
boolean through every downstream call.

Standard library only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field


@dataclass
class RunCommand:
    """A typed, parsing-free snapshot of every flag a use case may need."""

    # Explicit anchors.
    project_root: str | None
    agents_root: str | None
    core_root: str | None

    # Logical paths.
    profile: str | None
    project_skill: str | None

    # Run selection and scope.
    plan: str | None
    task: str | None
    through: str | None
    attest_dependency: list[str] = field(default_factory=list)
    resume: bool = False
    mode: str = "plan-only"
    feature: str | None = None
    prompt: str | None = None

    # Delivery gates.
    approve_plan: bool = False
    approve_final_diff: bool = False
    commit: bool = False
    commit_approved_manifest: bool = False
    dry_run: bool = False
    push: bool = False

    # Compatibility surface.
    status: bool = False
    unattended: bool = False
    adapter: str | None = None
    max_repair_attempts: int | None = None
    routine_output_byte_budget: int | None = None
    diagnostic_output_byte_budget: int | None = None
    verbose: bool = False
    quiet: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RunCommand":
        """Build a typed command from a parsed :func:`~feature_pipeline.cli.parser.build_parser`
        namespace. A field this dataclass does not declare is simply not read."""
        return cls(
            project_root=args.project_root,
            agents_root=args.agents_root,
            core_root=args.core_root,
            profile=args.profile,
            project_skill=args.project_skill,
            plan=args.plan,
            task=args.task,
            through=args.through,
            attest_dependency=list(args.attest_dependency or []),
            resume=args.resume,
            mode=args.mode,
            feature=args.feature,
            prompt=args.prompt,
            approve_plan=args.approve_plan,
            approve_final_diff=args.approve_final_diff,
            commit=args.commit,
            commit_approved_manifest=args.commit_approved_manifest,
            dry_run=args.dry_run,
            push=args.push,
            status=args.status,
            unattended=args.unattended,
            adapter=args.adapter,
            max_repair_attempts=args.max_repair_attempts,
            routine_output_byte_budget=args.routine_output_byte_budget,
            diagnostic_output_byte_budget=args.diagnostic_output_byte_budget,
            verbose=args.verbose,
            quiet=args.quiet,
        )


__all__ = ["RunCommand"]
