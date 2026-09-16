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
from typing import Callable, Mapping, Sequence

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.infrastructure.board_projection import (
    BoardProjectionError,
    CommandEvidence,
    CompletionEvidence,
    project_task_state,
)

from pipeline_core.adapters import Adapter
from pipeline_core.artifacts import write_json_atomic
from pipeline_core.commands import active_revision, verification_stage
from pipeline_core.dispatch import DispatchRequest, dispatch_executor
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.reports import (
    RepairReport,
    newest_repair_report,
    repair_report_path,
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
    "build_completion_evidence",
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
    model: str | None = None
    effort: str | None = None
    #: Executor-claimed checks, so :func:`build_verification_evidence` can surface a claim
    #: with no runner-recorded command as a fact-only ``FAIL``.
    claimed_checks: tuple[object, ...] = ()
    #: Runner-only read port for remote acceptance facts.  It receives neither executor
    #: capability nor a write path; absent observations must settle as a verification failure.
    remote_observer: Callable[[TaskSpec, int], Sequence[Mapping[str, object]]] | None = None
    #: **KLC-03**. The Markdown active board's path (``docs/kanban.md``); ``None`` keeps this
    #: run projection-free (boardless JSON plans). When set, ``spec.path`` — already
    #: repository-relative for a Markdown-backed task — resolves the task file to project onto.
    board_path: Path | None = None
    pre_dispatch: Callable[[], str | None] | None = None
    #: TAM-01: an optional pre-flight, pre-dispatch baseline diagnosis. Called only on the
    #: task's very first gate (``attempts == 0``), before any executor launch or repair
    #: budget is spent. A non-``None`` return produces the ``amendment_required`` terminal
    #: status below rather than a dispatch or a spent repair attempt (AC-4).
    baseline_diagnosis: Callable[[], object | None] | None = None


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
        return self.status in {"done", "verified"}

    @property
    def exit_code(self) -> int:
        """0 only for ``verified``; a blocked task exits non-zero (AC-4)."""
        return 0 if self.ok else 1


def build_completion_evidence(run: Run, spec: TaskSpec) -> CompletionEvidence:
    """Build the ``verified`` :class:`CompletionEvidence` for ``spec`` from durable run state.

    **KLC-03**. Never copies untrusted executor prose — every field is either a structured
    ``run.json`` fact (``verified_at``, ``run_id``, ``attempts``, both verdicts) or a
    repository-relative reference to a runner-owned artifact (the declared verification
    commands' recorded exit codes, the two verifier reports). Deterministic from ``run`` alone,
    so a live pass (:meth:`TaskEngine.run`) and a later resume's reconciliation
    (:func:`pipeline_core.execution.execute_run`) build byte-identical evidence for the same
    persisted state — the ``completed_at`` field is the durable ``verified_at`` timestamp, never
    the wall clock at call time.
    """
    record = run.task(spec.id)
    gate = record.attempts + 1
    revision = active_revision(run, spec.id)
    stage = verification_stage(spec.id, attempt=gate, revision=revision)
    commands = tuple(
        CommandEvidence(
            cwd=entry["cwd"], command=" ".join(entry["argv"]), exit_code=entry["exit_code"])
        for entry in (run.command(cid) for cid in run.stage_command_ids(stage))
    )
    arts = verifier_artifacts(run.run_dir, spec.id, gate, revision=revision)
    evidence_paths = tuple(
        repo_relative(path, run.repo_root)
        for path in (arts.task_report, arts.test_report)
        if path.is_file()
    )
    return CompletionEvidence(
        completed_at=record.verification.get("verified_at") or _utcnow(),
        run_id=run.run_id,
        resolution=record.resolution or "completed",
        resolution_reason=record.resolution_reason,
        repair_count=record.attempts,
        gate_count=gate,
        task_verdict=record.verification.get("task_verdict"),
        test_verdict=record.verification.get("test_verdict"),
        commands=commands,
        evidence_paths=evidence_paths,
    )


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
        record = run.task(task_id)
        repair_of, skip_executor = self._enter(life, request)
        if skip_executor:
            return TaskRunResult(task_id, "done", record.attempts, 0, None, None, ())
        passes: list[RepairPass] = []
        gates = 0

        while True:
            record = run.task(task_id)
            gate = record.attempts + 1

            if not skip_executor:
                if record.attempts == 0 and request.baseline_diagnosis is not None:
                    finding = request.baseline_diagnosis()
                    if finding is not None:
                        reason = getattr(finding, "reason", str(finding))
                        life.record_operation(
                            task_id, "baseline", "amendment_required", reason)
                        return TaskRunResult(task_id, "amendment_required", record.attempts,
                                             gates, reason, None, tuple(passes))
                if request.pre_dispatch is not None:
                    blocker = request.pre_dispatch()
                    if blocker:
                        life.record_operation(task_id, "precondition", "blocked", blocker)
                        return TaskRunResult(task_id, "waiting", record.attempts, gates,
                                             blocker, None, tuple(passes))
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
                        model=request.model,
                        effort=request.effort,
                    ),
                    request.adapter,
                )
                if dispatch.status == "retryable":
                    return TaskRunResult(
                        task_id, "retryable", run.task(task_id).attempts, gates,
                        dispatch.failure, dispatch.artifacts.result_protocol_invalid
                        if dispatch.artifacts.result_protocol_invalid.exists()
                        else dispatch.artifacts.launch_failure,
                        tuple(passes),
                    )
                if dispatch.status != "implemented":
                    # A settled executor block is a protocol fact, not prose to decorate:
                    # preserve its validated reason unchanged through the task-engine result
                    # and durable operation history.  Generic context is only for the
                    # impossible no-reason fallback (RLC-01 AC-2).
                    blocker = (dispatch.failure or run.task(task_id).blocker
                               or f"executor could not implement {task_id}")
                    life.record_operation(task_id, "executor", "failed", blocker, gate=gate)
                    passes.append(RepairPass(gate, "waiting", repair_of, None, None))
                    return TaskRunResult(task_id, "waiting", record.attempts, gates,
                                         blocker, None, tuple(passes))
            skip_executor = False

            gates += 1
            outcome = self._verify_gate(run, request, gate)
            verdict = (outcome.task_verdict, outcome.test_verdict)

            if outcome.status == "done":
                passes.append(RepairPass(gate, "done", repair_of, *verdict))
                life.recompute_readiness()
                run.save()
                self._project(run, request, "done", build_completion_evidence(run, spec))
                return TaskRunResult(
                    task_id, "done", run.task(task_id).attempts, gates, None, None,
                    tuple(passes))

            if outcome.failure or "BLOCKED" in verdict:
                # External waits and verifier launch failures are resumable operations.
                blocker = run.task(task_id).blocker or "verification waiting on an external cause"
                life.record_operation(task_id, "verification", "blocked", blocker, gate=gate)
                passes.append(RepairPass(gate, "waiting", repair_of, *verdict))
                run.save()
                return TaskRunResult(task_id, "waiting", record.attempts, gates,
                                     blocker, outcome.diagnostic, tuple(passes))

            # A failed gate is auditable and a later operation may always continue it.
            report = self._write_repair_report(run, spec, gate, outcome)
            passes.append(RepairPass(gate, "failed", repair_of, *verdict))

            if not run.begin_repair(task_id, maximum=maximum):
                run.save()
                blocker = (f"maximum repair attempts ({maximum}) reached after verification "
                           f"gate {gate}; last verdicts task={verdict[0]} test={verdict[1]}")
                return TaskRunResult(task_id, "escalated", record.attempts, gates,
                                     blocker, None, tuple(passes))
            run.save()
            repair_of = repo_relative(report.path, run.repo_root)

    # -- entry reconciliation -----------------------------------------------------------

    def _enter(self, life: RunLifecycle, request: TaskExecution) -> tuple[str | None, bool]:
        """Resolve the latest resumable operation without reintroducing legacy states."""
        run = life.run
        task_id = request.spec.id
        record = run.task(task_id)
        if record.status == "to_do":
            self._project(run, request, "to_do")
            life.transition(task_id, "in_progress", actor=ACTOR_RUNNER,
                            note="executor operation started")
            self._project(run, request, "in_progress")
        if record.status == "in_progress":
            report = newest_repair_report(
                run.run_dir, task_id, revision=active_revision(run, task_id))
            # An executor which already reported ``implemented`` is settled work.  If the
            # next unfinished boundary is verification (for example a verifier was
            # unavailable after runner-owned commands had completed), continue that gate
            # rather than manufacturing another implementation operation.  A failed gate,
            # repair escalation, or an executor failure has a different latest operation and
            # deliberately starts a fresh executor window.
            history = record.operation_history
            latest = history[-1] if history else None
            implemented = any(
                entry.get("kind") == "executor" and entry.get("outcome") == "succeeded"
                for entry in history
            )
            verification_pending = (
                isinstance(latest, dict)
                and latest.get("kind") == "verification"
                and latest.get("outcome") == "blocked"
            )
            return (repo_relative(report, run.repo_root) if report else None), (
                implemented and verification_pending
            )
        if record.status == "done":
            self._project(run, request, "done", build_completion_evidence(run, request.spec))
            return None, True
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
                model=request.model,
                effort=request.effort,
                claimed_checks=request.claimed_checks,
                remote_observer=request.remote_observer,
            ),
        )

    def _write_repair_report(
        self, run: Run, spec: TaskSpec, gate: int, outcome: VerificationOutcome
    ) -> RepairReport:
        task_id = spec.id
        revision = active_revision(run, task_id)
        attempt = run.task(task_id).attempts + 2
        persisted = repair_report_path(run.run_dir, task_id, attempt, revision=revision)
        if persisted.is_file():
            if not _valid_repair_report(persisted, task_id, attempt, revision):
                raise ExecutionError(
                    f"{task_id} has a malformed persisted repair report for attempt {attempt}",
                    "malformed-repair-report",
                )
            return RepairReport(persisted, attempt, gate, (), (), (), ())
        arts = verifier_artifacts(run.run_dir, task_id, gate, revision=revision)
        product, environment, regression = _classify(spec, outcome)
        return write_repair_report(
            run, spec, attempt,
            task_verifier_text=_read(arts.task_report),
            test_verifier_text=_read(arts.test_report),
            source_attempt=gate,
            revision=revision,
            product_defects=product,
            environment_problems=environment,
            regression_tests=regression,
        )

    # -- Markdown board/task projection (KLC-03) ---------------------------------------

    def _project(
        self, run: Run, request: TaskExecution, state: str,
        evidence: CompletionEvidence | None = None,
    ) -> None:
        """Render ``state`` (and, for ``verified``, ``evidence``) onto the Markdown board and
        task file, when ``request.board_path`` names one (a boardless JSON plan leaves it
        ``None`` and stays projection-free).

        Called only *after* the durable transition it renders has already been saved to
        ``run.json`` — see each call site. A :class:`BoardProjectionError` here (a malformed
        board/task file, a missing or duplicate card) is re-raised as a truthful
        :class:`ExecutionError`; ``run.json`` is already intact, so the next ``--resume``
        repairs the human view idempotently (:func:`pipeline_core.execution.execute_run`).
        """
        if request.board_path is None:
            return
        spec = request.spec
        # ``spec.path`` is already repository-relative for a Markdown-backed task file.
        task_path = run.repo_root / spec.path
        try:
            project_task_state(
                board_path=Path(request.board_path),
                task_path=task_path,
                task_id=spec.id,
                task_title=spec.title,
                state=state,
                evidence=evidence,
            )
            # The projection has changed protected, human-facing files before the next
            # executor window opens.  Persist its actor and content digests now; otherwise a
            # later ambient diff cannot distinguish runner lifecycle maintenance from an
            # executor edit.
            run.record_runner_projection(spec.id, (Path(request.board_path), task_path))
            run.save()
        except BoardProjectionError as exc:
            raise ExecutionError(
                f"board projection failed for {spec.id} -> {state!r}: {exc}",
                "board-projection-failed",
            ) from exc

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
        life.block(task_id, blocker)

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
        # KLC-03: project 'blocked' back onto the Markdown board only after every durable
        # write above (blocker, packet, ``## Blockers`` section) has already landed on disk.
        self._project(run, request, "blocked")
        return TaskRunResult(
            task_id, "blocked", record.attempts, gates, blocker, Path(diagnostic),
            tuple(passes))


def _valid_repair_report(
    path: Path, task_id: str, attempt: int, revision: int | None = None,
) -> bool:
    """Return whether persisted repair evidence has the required identity and provenance.

    ``attempt`` and ``revision`` are the caller's own expected identity — never re-derived from
    the filename — so a persisted report whose declared revision disagrees with the task's
    active revision is rejected rather than silently accepted as this revision's evidence.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    required = (
        f"# Repair Report — {task_id} — attempt {attempt} of ",
        f"- Task: {task_id} — ",
        f"- Attempt: {attempt} of ",
        f"- Revision: {revision or 0}",
        f"- Source verification gate: {attempt - 1}",
        "- Original scope (unchanged): ",
        "- Out of scope (unchanged): ",
        "## Task-verifier report (verbatim)",
        "## Test-verifier report (verbatim)",
    )
    return all(marker in text for marker in required)


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
