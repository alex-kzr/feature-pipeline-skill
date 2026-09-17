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
from typing import Callable, Mapping, Sequence

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.plan import ResolvedCheck

from pipeline_core.commands import (
    VerificationRun,
    active_revision,
    run_verification_commands,
    verification_stage,
)
from pipeline_core.state import Run
from pipeline_core.verification import (
    VerificationOutcome,
    VerifierAnchors,
    VerifierLaunchers,
    build_verification_evidence,
    orchestrate_verification,
    remote_evidence_required,
)

__all__ = [
    "VerificationContractError",
    "VerificationRequest",
    "VerificationService",
    "validate_declared_commands",
]


class VerificationContractError(ValueError):
    """A task-local verification declaration diverges from its selected profile checks."""


@dataclass(frozen=True)
class VerificationRequest:
    """Everything one verification gate needs that is not already carried on the run."""

    spec: TaskSpec
    launchers: VerifierLaunchers
    anchors: VerifierAnchors
    attempt: int
    plan_path: str | None = None
    timeout: float | None = None
    model: str | None = None
    effort: str | None = None
    #: Executor-claimed checks, so a claim with no runner-recorded command surfaces as a
    #: fact-only ``FAIL`` rather than a prompt to re-run the check.
    claimed_checks: tuple[object, ...] = field(default_factory=tuple)
    #: The selected profile checks, when the caller has a compiled task.  The runner still
    #: executes ``spec.verification_commands`` and records their original argv/cwd; this is a
    #: pre-execution contract check, not a second command path.
    expected_checks: tuple[ResolvedCheck, ...] = field(default_factory=tuple)
    #: A runner-wired, read-only port. It is invoked only for a task contract that requires
    #: remote acceptance facts, after local command capture and before verdict settlement.
    #: ``None`` deliberately means no observation is available; settlement then fails closed.
    remote_observer: Callable[[TaskSpec, int], Sequence[Mapping[str, object]]] | None = None


class VerificationService:
    """Sequence one independent-verification gate over runner-owned evidence."""

    def verify(self, run: Run, request: VerificationRequest) -> VerificationOutcome:
        spec = request.spec
        task_id = spec.id
        if request.expected_checks:
            validate_declared_commands(spec, request.expected_checks)
        revision = active_revision(run, task_id)
        stage = verification_stage(task_id, attempt=request.attempt, revision=revision)
        command_ids = run.stage_command_ids(stage)
        declared_count = len(spec.verification_commands)
        if len(command_ids) == declared_count:
            # A verifier interruption occurs after these runner-owned command records have
            # settled.  Reuse those immutable facts on resume; in particular, a non-zero
            # command remains recorded as a failure instead of being hidden by a rerun.
            commands_run = VerificationRun(tuple(run.command(command_id) for command_id in command_ids))
            run.record_operation(
                task_id, "verification", "resumed",
                "reusing settled runner-owned command evidence", attempt=request.attempt,
            )
        else:
            commands_run = run_verification_commands(
                run,
                spec.verification_commands,
                stage=stage,
                task_id=task_id,
                attempt=request.attempt,
                timeout=request.timeout,
            )
        execution = run.task(task_id).execution_evidence or {}
        if (remote_evidence_required(spec) and request.remote_observer is not None
                and not execution.get("remote_evidence")):
            observations = request.remote_observer(spec, request.attempt)
            if observations:
                run.record_remote_evidence(
                    task_id, attempt=request.attempt, observations=observations,
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
            model=request.model,
            effort=request.effort,
        )
        run.save()
        return outcome


def validate_declared_commands(
    spec: TaskSpec, expected_checks: Sequence[ResolvedCheck]
) -> None:
    """Require a task's declared commands to be exactly its compiled check contract.

    Command execution remains task-local: the verification service passes the original
    :attr:`TaskSpec.verification_commands` to the runner unchanged.  This comparison simply
    prevents a caller that already has a compiled route from dropping a required repository
    check or introducing an unrelated stack command.
    """
    declared = tuple((command.cwd, tuple(command.argv)) for command in spec.verification_commands)
    expected = tuple((str(check.cwd), check.argv) for check in expected_checks)
    if declared != expected:
        raise VerificationContractError(
            f"{spec.id} verification commands do not match its selected profile checks"
        )
