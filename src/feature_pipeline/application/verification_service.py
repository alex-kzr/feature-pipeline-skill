"""The single verification use case: runner-owned commands, immutable evidence, two verifiers.

:class:`VerificationService` is the one place stage 8 is sequenced:

1. run every declared verification command through the runner
   (:func:`pipeline_core.commands.run_verification_commands`) — the runner, never a verifier,
   owns what actually ran;
2. package that command evidence with the task/attempt identity and the implementation
   manifest/diff/report references into one immutable
   :class:`~pipeline_core.verification.VerificationEvidence`;
3. obtain two fresh, independent, read-only verdicts over that one payload
   (:func:`pipeline_core.verification.orchestrate_verification`) and let only their parsed
   combination move task state.

The launcher pair is injected (:class:`~pipeline_core.verification.VerifierLaunchers`); the
service parses no arguments and constructs no adapter (AC-1).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from schemas.contracts import TaskSpec

from pipeline_core.commands import run_verification_commands, verification_stage
from pipeline_core.state import Run
from pipeline_core.verification import (
    VerificationOutcome,
    VerifierAnchors,
    VerifierLaunchers,
    build_verification_evidence,
    orchestrate_verification,
)

__all__ = ["VerificationRequest", "VerificationService"]


@dataclass(frozen=True)
class VerificationRequest:
    """Everything one verification gate needs that is not already carried on the run."""

    spec: TaskSpec
    launchers: VerifierLaunchers
    anchors: VerifierAnchors
    attempt: int
    plan_path: str | None = None
    timeout: float | None = None
    #: Executor-claimed checks, so a claim with no runner-recorded command surfaces as a
    #: fact-only ``FAIL`` rather than a prompt to re-run the check.
    claimed_checks: tuple[object, ...] = field(default_factory=tuple)


class VerificationService:
    """Sequence one independent-verification gate over runner-owned evidence."""

    def verify(self, run: Run, request: VerificationRequest) -> VerificationOutcome:
        spec = request.spec
        task_id = spec.id
        commands_run = run_verification_commands(
            run,
            spec.verification_commands,
            stage=verification_stage(task_id, attempt=request.attempt),
            task_id=task_id,
            attempt=request.attempt,
            timeout=request.timeout,
        )
        evidence = build_verification_evidence(
            run,
            task_id,
            attempt=request.attempt,
            commands_run=commands_run,
            claimed_checks=request.claimed_checks,
        )
        outcome = orchestrate_verification(
            run,
            spec,
            evidence,
            launchers=request.launchers,
            anchors=request.anchors,
            attempt=request.attempt,
            plan_path=request.plan_path,
        )
        run.save()
        return outcome
