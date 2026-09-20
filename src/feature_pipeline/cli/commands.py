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
from pathlib import Path
from typing import Any


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
    # ``None`` means the switch was omitted.  This is significant on ``--resume``, where
    # execution inherits the immutable value from run.json.
    verify_dependency_chain: bool | None = None
    resume: bool = False
    mode: str = "plan-only"
    feature: str | None = None
    prompt: str | None = None
    grants: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    published_refs: list[str] = field(default_factory=list)
    recovery_source_feature: str | None = None
    recovery_task: str | None = None
    operational_unblock_task: str | None = None
    human_authorized_operational_unblock: bool = False
    uv_cache_dir: str | None = None
    amend_task: str | None = None
    amend_rationale: str | None = None
    amend_approved_by: str | None = None
    amend_evidence: str | None = None
    amend_contract: str | None = None

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
    codex_runtime: str = "host"
    docker_codex_image: str | None = None
    docker_proxy_image: str | None = None
    docker_codex_version: str | None = None
    docker_codex_auth_file: str | None = None
    model: str | None = None
    effort: str | None = None
    max_repair_attempts: int | None = None
    routine_output_byte_budget: int | None = None
    diagnostic_output_byte_budget: int | None = None
    verbose: bool = False
    quiet: bool = False
    # Runner-owned containment experiment. This is deliberately separate from execute mode.
    live_isolation_probe: bool = False
    live_probe_opt_in: bool = False
    live_probe_timeout: float | None = None
    live_probe_max_attempts: int | None = None
    live_probe_request_count: int = 0

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
            verify_dependency_chain=args.verify_dependency_chain,
            resume=args.resume,
            mode=args.mode,
            feature=args.feature,
            prompt=args.prompt,
            grants=list(args.grant or []), approvals=list(args.approve or []),
            published_refs=list(args.published_ref or []),
            recovery_source_feature=args.recovery_source_feature,
            recovery_task=args.recovery_task,
            operational_unblock_task=args.operational_unblock_task,
            human_authorized_operational_unblock=args.human_authorized_operational_unblock,
            uv_cache_dir=args.uv_cache_dir,
            amend_task=args.amend_task,
            amend_rationale=args.amend_rationale,
            amend_approved_by=args.amend_approved_by,
            amend_evidence=args.amend_evidence,
            amend_contract=args.amend_contract,
            approve_plan=args.approve_plan,
            approve_final_diff=args.approve_final_diff,
            commit=args.commit,
            commit_approved_manifest=args.commit_approved_manifest,
            dry_run=args.dry_run,
            push=args.push,
            status=args.status,
            unattended=args.unattended,
            adapter=args.adapter,
            codex_runtime=args.codex_runtime,
            docker_codex_image=args.docker_codex_image,
            docker_proxy_image=args.docker_proxy_image,
            docker_codex_version=args.docker_codex_version,
            docker_codex_auth_file=args.docker_codex_auth_file,
            model=args.model,
            effort=args.effort,
            max_repair_attempts=args.max_repair_attempts,
            routine_output_byte_budget=args.routine_output_byte_budget,
            diagnostic_output_byte_budget=args.diagnostic_output_byte_budget,
            verbose=args.verbose,
            quiet=args.quiet,
            live_isolation_probe=bool(args.live_isolation_probe_count),
            live_probe_opt_in=args.live_probe_opt_in,
            live_probe_timeout=args.live_probe_timeout,
            live_probe_max_attempts=args.live_probe_max_attempts,
            live_probe_request_count=args.live_isolation_probe_count or 0,
        )


def build_live_probe_request(
    *,
    task_id: str,
    report_path: Path,
    allowed_scope: tuple[str, ...],
    timeout: float,
) -> object:
    """Create a probe-only request that cannot name an executor or verifier role."""
    from pipeline_core.adapters import LiveProbeRequest

    return LiveProbeRequest(
        task_id=task_id,
        prompt="runner-owned live isolation observation",
        report_path=report_path,
        allowed_scope=allowed_scope,
        timeout=timeout,
    )


def execute_live_probe(
    run: object,
    *,
    task_id: str,
    adapter: object,
    request: object,
    cli_version: str,
    task_contract_digest: str,
    bundle_digest: str,
    timeout_s: float,
    max_attempts: int,
    attempt_id: str,
) -> dict[str, Any]:
    """Delegate the opaque, runner-owned observation to the core boundary."""
    from pipeline_core.commands import run_live_isolation_probe

    return run_live_isolation_probe(
        run,
        task_id=task_id,
        adapter=adapter,
        request=request,
        cli_version=cli_version,
        task_contract_digest=task_contract_digest,
        bundle_digest=bundle_digest,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        attempt_id=attempt_id,
    )


__all__ = [
    "RunCommand",
    "build_live_probe_request",
    "execute_live_probe",
]
