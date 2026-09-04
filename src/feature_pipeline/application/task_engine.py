"""Drive one dependency-ready task from ``ready`` to a terminal state — the one repair loop.

:class:`TaskEngine` owns the bounded, auditable sequence for a single task:

* dispatch the executor (:func:`pipeline_core.dispatch.dispatch_executor`) and require
  ``implemented``;
* run the full independent-verification gate through
  :class:`~feature_pipeline.application.verification_service.VerificationService`
  (runner-owned commands, immutable evidence, two fresh read-only verifiers);
* on a verification ``FAIL``, consolidate both verifier reports into one attempt-numbered
  repair report, spend exactly one repair attempt, and redispatch the *same* executor role
  fresh — up to the task's declared bound;
* on an external ``BLOCKED`` or once the repair budget is exhausted, block the task once,
  durably, writing a complete pre-transition diagnostic through
  :class:`~feature_pipeline.application.diagnostic_service.DiagnosticService` first.

The engine parses no arguments and constructs no adapter: the executor adapter and the
verifier launcher pair arrive on the :class:`TaskExecution` request, and the two collaborator
services are injected (AC-1). It has no write path to ``run.json`` beyond the lifecycle it is
handed, so it cannot mark its own work ``verified`` outside the verdict gate.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from schemas.contracts import TaskSpec

from pipeline_core.adapters import Adapter
from pipeline_core.artifacts import write_json_atomic
from pipeline_core.dispatch import DispatchRequest, dispatch_executor
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.reports import (
    RepairReport,
    newest_repair_report,
    verifier_artifacts,
    write_repair_report,
)
from pipeline_core.state import ACTOR_RUNNER, Run, repo_relative
from pipeline_core.task_files import upsert_blockers_section
from pipeline_core.verification import (
    VerificationOutcome,
    VerifierAnchors,
    VerifierLaunchers,
)

from .diagnostic_service import DiagnosticService
from .verification_service import VerificationRequest, VerificationService

__all__ = [
    "ExecutionError",
    "RepairPass",
    "TaskEngine",
    "TaskExecution",
    "TaskRunResult",
]


class ExecutionError(RuntimeError):
    """A repair-loop precondition failure. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class TaskExecution:
    """Everything :meth:`TaskEngine.run` needs that is not already carried on the run."""

    spec: TaskSpec
    adapter: Adapter
    launchers: VerifierLaunchers
    verifier_anchors: VerifierAnchors
    envelope_anchors: EnvelopeAnchors
    role_grant: tuple[str, ...] = ("read", "run_checks", "write")
    execution_mode: str = "separate"
    plan_path: str | None = None
    working_root: str = "."
    timeout: float | None = None
    #: Executor-claimed checks, so :func:`build_verification_evidence` can surface a claim
    #: with no runner-recorded command as a fact-only ``FAIL``.
    claimed_checks: tuple[object, ...] = ()


@dataclass(frozen=True)
class RepairPass:
    """One trip through the verification gate: the initial pass is ``gate == 1``."""

    gate: int
    status: str  # 'verified' | 'verification_failed' | 'blocked'
    repair_report: str | None
    task_verdict: str | None
    test_verdict: str | None


@dataclass(frozen=True)
class TaskRunResult:
    """The terminal outcome of driving one task through the bounded repair loop."""

    task_id: str
    status: str  # 'verified' | 'blocked'
    attempts: int  # repair dispatches consumed (0 -> verified on the first pass)
    gates: int  # verification gates run
    blocker: str | None
    diagnostic: Path | None
    passes: tuple[RepairPass, ...]

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    @property
    def exit_code(self) -> int:
        """0 only for ``verified``; a blocked task exits non-zero (AC-4)."""
        return 0 if self.status == "verified" else 1


class TaskEngine:
    """Sequence one task through dispatch, independent verification, and bounded repair."""

    def __init__(
        self,
        *,
        verification: VerificationService | None = None,
        diagnostics: DiagnosticService | None = None,
    ) -> None:
        self._verification = verification if verification is not None else VerificationService()
        self._diagnostics = diagnostics if diagnostics is not None else DiagnosticService()

    # -- public entry ---------------------------------------------------------------------

    def run(self, life: RunLifecycle, request: TaskExecution) -> TaskRunResult:
        """Drive ``request.spec`` from ``ready`` (or a resumed ``implemented``/repair state) to
        a terminal ``verified`` or ``blocked``, including bounded repair. See the module
        docstring.
        """
        run = life.run
        spec = request.spec
        task_id = spec.id
        maximum = int(spec.max_repair_attempts)

        repair_of, skip_executor = self._enter(life, task_id)
        passes: list[RepairPass] = []
        gates = 0

        while True:
            record = run.task(task_id)
            gate = record.attempts + 1

            if not skip_executor:
                if record.status == "repairing":
                    life.transition(
                        task_id, "running", actor=ACTOR_RUNNER,
                        note=f"repair attempt {record.attempts}: fresh executor window")
                dispatch = dispatch_executor(
                    life,
                    DispatchRequest(
                        spec=spec,
                        role_grant=tuple(request.role_grant),
                        anchors=request.envelope_anchors,
                        execution_mode=request.execution_mode,
                        plan_path=request.plan_path,
                        working_root=request.working_root,
                        timeout=request.timeout,
                        fresh_session=True,
                        attempt=gate,
                        repair_report_path=repair_of,
                    ),
                    request.adapter,
                )
                if dispatch.status != "implemented":
                    passes.append(RepairPass(gate, "blocked", repair_of, None, None))
                    return self._block(
                        life, request, gate, gates, passes,
                        blocker=(run.task(task_id).blocker
                                 or f"executor could not implement {task_id}: "
                                    f"{dispatch.failure}"),
                        diagnostic=None)
            skip_executor = False

            gates += 1
            outcome = self._verify_gate(run, request, gate)
            verdict = (outcome.task_verdict, outcome.test_verdict)

            if outcome.status == "verified":
                passes.append(RepairPass(gate, "verified", repair_of, *verdict))
                life.recompute_readiness()
                run.save()
                return TaskRunResult(
                    task_id, "verified", run.task(task_id).attempts, gates, None, None,
                    tuple(passes))

            if outcome.status == "blocked":
                # An external BLOCKED verdict or a verifier launch/settlement failure — never
                # spends a repair attempt (AC-3). The task is already 'blocked' with a blocker.
                passes.append(RepairPass(gate, "blocked", repair_of, *verdict))
                return self._block(
                    life, request, gate, gates, passes,
                    blocker=run.task(task_id).blocker
                    or "verification blocked (external cause)",
                    diagnostic=outcome.diagnostic)

            # outcome.status == 'verification_failed': consolidate and try a bounded repair.
            report = self._write_repair_report(run, spec, gate, outcome)
            passes.append(RepairPass(gate, "verification_failed", repair_of, *verdict))

            if not run.begin_repair(task_id, maximum=maximum):
                run.save()
                diagnostic = self._diagnostics.collect(
                    run, task_id=task_id, attempt=gate,
                    note=f"maximum repair attempts ({maximum}) reached; no repair budget "
                         f"remains")
                return self._block(
                    life, request, gate, gates, passes,
                    blocker=(f"maximum repair attempts ({maximum}) reached after verification "
                             f"gate {gate}; last verdicts task={verdict[0]} test={verdict[1]}"),
                    diagnostic=diagnostic, repair_report=report.path)
            run.save()
            repair_of = repo_relative(report.path, run.repo_root)

    # -- entry reconciliation -----------------------------------------------------------

    def _enter(self, life: RunLifecycle, task_id: str) -> tuple[str | None, bool]:
        """Resolve the loop's starting point from the persisted task state.

        Returns ``(repair_report_path, skip_executor)``. A resume that rolled an interrupted
        ``repairing`` back to ``verification_failed`` (or an interrupted repair executor back
        to ``ready``) is continued against the already-written repair report so the attempt
        is not counted twice.
        """
        run = life.run
        record = run.task(task_id)
        report = newest_repair_report(run.run_dir, task_id)

        if record.status == "implemented":
            return (repo_relative(report, run.repo_root) if report else None), True

        if record.status == "verification_failed":
            if report is None:
                raise ExecutionError(
                    f"{task_id} resumed at 'verification_failed' with no persisted repair "
                    f"report",
                    "missing-repair-report")
            life.transition(
                task_id, "repairing", actor=ACTOR_RUNNER,
                note="resume: continue interrupted repair")
            return repo_relative(report, run.repo_root), False

        if record.status == "repairing":
            return (repo_relative(report, run.repo_root) if report else None), False

        if record.status == "ready":
            repair_of = (
                repo_relative(report, run.repo_root)
                if record.attempts > 0 and report is not None
                else None
            )
            life.transition(task_id, "running", actor=ACTOR_RUNNER)
            return repair_of, False

        raise ExecutionError(
            f"{task_id} cannot enter the repair loop from '{record.status}'",
            "unexpected-entry-state")

    # -- verification gate + repair-report assembly -----------------------------------

    def _verify_gate(
        self, run: Run, request: TaskExecution, gate: int
    ) -> VerificationOutcome:
        """Run every declared verification command then both fresh verifiers for one attempt."""
        return self._verification.verify(
            run,
            VerificationRequest(
                spec=request.spec,
                launchers=request.launchers,
                anchors=request.verifier_anchors,
                attempt=gate,
                plan_path=request.plan_path,
                timeout=request.timeout,
                claimed_checks=tuple(request.claimed_checks),
            ),
        )

    def _write_repair_report(
        self, run: Run, spec: TaskSpec, gate: int, outcome: VerificationOutcome
    ) -> RepairReport:
        task_id = spec.id
        arts = verifier_artifacts(run.run_dir, task_id, gate)
        product, environment, regression = _classify(spec, outcome)
        return write_repair_report(
            run, spec, run.task(task_id).attempts + 2,
            task_verifier_text=_read(arts.task_report),
            test_verifier_text=_read(arts.test_report),
            source_attempt=gate,
            product_defects=product,
            environment_problems=environment,
            regression_tests=regression,
        )

    # -- durable block at the attempt limit / on an external block -------------------

    def _block(
        self,
        life: RunLifecycle,
        request: TaskExecution,
        gate: int,
        gates: int,
        passes: Sequence[RepairPass],
        *,
        blocker: str,
        diagnostic: Path | None,
        repair_report: Path | None = None,
    ) -> TaskRunResult:
        """Block the task once, durably: run.json blocker, structured packet, task-file
        section, dependent suppression. Idempotent — a re-entry after a crash writes the same
        content. A missing diagnostic is collected here, with full repository evidence."""
        run = life.run
        spec = request.spec
        task_id = spec.id

        if diagnostic is None:
            diagnostic = self._diagnostics.collect(
                run, task_id=task_id, attempt=gate, note=blocker)

        record = run.task(task_id)
        if record.status != "blocked":
            life.block(task_id, blocker)  # transition + blocker + suppress dependents + flush
        else:
            record.blocker = blocker
            run.record_event(f"blocker:{task_id}", to="blocked", note=blocker)
            life.recompute_readiness()  # suppress dependents an earlier block did not
            run.save()

        packet = write_json_atomic(
            Path(run.run_dir) / "reports" / task_id / f"blocked-{gate}.json",
            {
                "task_id": task_id,
                "gate": gate,
                "attempts": record.attempts,
                "max_repair_attempts": int(spec.max_repair_attempts),
                "blocker": blocker,
                "diagnostic": repo_relative(diagnostic, run.repo_root),
                "repair_report": (
                    repo_relative(repair_report, run.repo_root) if repair_report else None),
                "verdicts": {
                    "task": record.verification.get("task_verdict"),
                    "test": record.verification.get("test_verdict"),
                },
                "recorded_at": _utcnow(),
            },
            repo_root=run.repo_root,
        )
        run.artifacts[f"blocker:{task_id}"] = repo_relative(packet, run.repo_root)

        task_path = (run.repo_root / spec.path) if spec.path else None
        if task_path is not None and task_path.is_file():
            try:
                upsert_blockers_section(
                    task_path,
                    key=run.run_id,
                    reason=blocker,
                    fields={
                        "Task": task_id,
                        "Repair attempts": (
                            f"{record.attempts} of {int(spec.max_repair_attempts)}"),
                        "Diagnostic": repo_relative(diagnostic, run.repo_root),
                        "Recorded": _utcnow(),
                    },
                )
            except OSError:
                pass

        run.save()
        return TaskRunResult(
            task_id, "blocked", record.attempts, gates, blocker, Path(diagnostic),
            tuple(passes))


def _classify(
    spec: TaskSpec, outcome: VerificationOutcome
) -> tuple[list[str], list[str], list[str]]:
    """Split the gate's failure signal into (product defects, environment problems,
    regressions)."""
    product: list[str] = []
    environment: list[str] = []
    regression = [f"{cmd.cwd} -> {' '.join(cmd.argv)}" for cmd in spec.verification_commands]
    regression.append("re-check every acceptance criterion through the task verifier")

    if outcome.forced_fail_reason:
        product.append(outcome.forced_fail_reason)
    if outcome.task_verdict == "FAIL":
        product.append(
            "task_verifier returned FAIL — an acceptance criterion is not met by the "
            "implementation (see the verbatim task-verifier report below)")
    if outcome.test_verdict == "FAIL":
        product.append(
            "test_verifier returned FAIL — the recorded command evidence does not show the "
            "verification commands passing (see the verbatim test-verifier report below)")
    for drift in (outcome.task_drift, outcome.test_drift):
        if drift:
            environment.append(f"verifier verdict drift (not a code defect): {drift}")
    return product, environment, regression


def _read(path: Path) -> str:
    target = Path(path)
    return target.read_text(encoding="utf-8") if target.is_file() else ""
