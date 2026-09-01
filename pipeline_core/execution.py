"""Bounded, auditable repair loop: one task from ``ready`` to a terminal state.

:func:`run_task` drives a single dependency-ready task through the standard executor dispatch
(:func:`pipeline_core.dispatch.dispatch_executor`) and the full independent-verification gate
(:func:`pipeline_core.verification.orchestrate_verification`), and — on a verification
``FAIL`` — through a bounded repair cycle:

* consolidate both verifier reports into one attempt-numbered repair report
  (:func:`pipeline_core.reports.write_repair_report`) that de-duplicates the findings,
  separates product defects from environment problems, lists the regression tests the repair
  must rerun, and restates the original allowed/out-of-scope verbatim;
* spend exactly one repair attempt (:meth:`pipeline_core.state.Run.begin_repair`), reopen the
  task as a fresh executor window, and redispatch the *same* executor role with the repair
  report and no resumed session;
* require the repair to return ``implemented`` and rerun *every* verification command and
  *every* acceptance criterion through two fresh, independent verifiers.

An external ``BLOCKED`` outcome never spends a repair attempt. When the attempt budget is
exhausted the task is blocked once, durably: the blocker is persisted on ``run.json``, an
idempotent ``## Blockers`` section is written into the task file, pending dependents are
suppressed with a ``blocked_by`` marker, and the returned :class:`TaskRunResult` reports a
non-zero :attr:`~TaskRunResult.exit_code`. Every generation directory, verifier report,
command record, and diagnostic from a failed attempt is preserved — a later attempt only ever
writes new artifacts.

Standard library only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from schemas.contracts import TaskSpec

from .adapter_resolution import (
    AdapterResolution,
    AdapterResolutionError,
    ensure_pinned_adapter,
    pin_adapter,
    resolve_adapter,
)
from .adapters import Adapter
from .artifacts import write_json_atomic
from .commands import (
    DIAGNOSTIC_OUTPUT_BUDGET,
    ROUTINE_OUTPUT_BUDGET,
    run_verification_commands,
    verification_stage,
)
from .concurrency import pipeline_lease, task_lease
from .diagnostics import write_diagnostic_report
from .dispatch import DispatchError, DispatchRequest, dispatch_executor
from .lease import LeaseHeldError
from .lifecycle import RunLifecycle
from .prompt_envelope import EnvelopeAnchors
from .reports import RepairReport, newest_repair_report, verifier_artifacts, write_repair_report
from .state import ACTOR_RUNNER, ResumeError, Run, StateError, repo_relative
from .task_files import upsert_blockers_section
from .verification import (
    VerificationOutcome,
    VerifierAnchors,
    VerifierLaunchers,
    build_verification_evidence,
    orchestrate_verification,
)

#: Process exit codes, byte-identical to :mod:`pipeline_core.runner_cli` — ``execute`` mode
#: shares the baseline compatibility table so one exit code means one thing across modes.
EXIT_OK = 0
EXIT_GATE_PENDING = 10
EXIT_BLOCKED = 20
EXIT_ERROR = 30

#: The executor capability grant an ``execute`` run composes when a caller does not override
#: it. A read-only role can never keep ``write`` (:func:`pipeline_core.adapters.effective_grant`).
DEFAULT_ROLE_GRANT = ("read", "run_checks", "write")

__all__ = [
    "EXIT_BLOCKED",
    "EXIT_ERROR",
    "EXIT_GATE_PENDING",
    "EXIT_OK",
    "ExecuteControls",
    "ExecuteRequest",
    "ExecuteResult",
    "ExecutionError",
    "RepairPass",
    "TaskExecution",
    "TaskRunResult",
    "execute_run",
    "run_task",
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
    """Everything :func:`run_task` needs that is not already carried on the run."""

    spec: TaskSpec
    adapter: Adapter
    launchers: VerifierLaunchers
    verifier_anchors: VerifierAnchors
    envelope_anchors: EnvelopeAnchors
    role_grant: tuple[str, ...] = ("read", "run_checks", "write")
    execution_mode: str = "separate"
    plan_path: str | None = None
    working_root: str = "."
    tools: tuple[str, ...] = ()
    timeout: float | None = None
    #: Executor-claimed checks, so :func:`build_verification_evidence` can surface a claim with
    #: no runner-recorded command as a fact-only ``FAIL``.
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


def run_task(life: RunLifecycle, request: TaskExecution) -> TaskRunResult:
    """Drive ``request.spec`` from ``ready`` (or a resumed ``implemented``/repair state) to a
    terminal ``verified`` or ``blocked``, including bounded repair. See the module docstring.
    """
    run = life.run
    spec = request.spec
    task_id = spec.id
    maximum = int(spec.max_repair_attempts)

    repair_of, skip_executor = _enter(life, task_id)
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
                    tools=tuple(request.tools),
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
                return _block(
                    life, request, gate, gates, passes,
                    blocker=(run.task(task_id).blocker
                             or f"executor could not implement {task_id}: {dispatch.failure}"),
                    diagnostic=None)
        skip_executor = False

        gates += 1
        outcome = _verify_gate(run, request, gate)
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
            return _block(
                life, request, gate, gates, passes,
                blocker=run.task(task_id).blocker or "verification blocked (external cause)",
                diagnostic=outcome.diagnostic)

        # outcome.status == 'verification_failed': consolidate and try a bounded repair.
        report = _write_repair_report(run, spec, gate, outcome)
        passes.append(RepairPass(gate, "verification_failed", repair_of, *verdict))

        if not run.begin_repair(task_id, maximum=maximum):
            run.save()
            diagnostic = write_diagnostic_report(
                run, task_id=task_id, attempt=gate,
                note=f"maximum repair attempts ({maximum}) reached; no repair budget remains")
            return _block(
                life, request, gate, gates, passes,
                blocker=(f"maximum repair attempts ({maximum}) reached after verification gate "
                         f"{gate}; last verdicts task={verdict[0]} test={verdict[1]}"),
                diagnostic=diagnostic, repair_report=report.path)
        run.save()
        repair_of = repo_relative(report.path, run.repo_root)


# --- entry reconciliation -----------------------------------------------------------------


def _enter(life: RunLifecycle, task_id: str) -> tuple[str | None, bool]:
    """Resolve the loop's starting point from the persisted task state.

    Returns ``(repair_report_path, skip_executor)``. A resume that rolled an interrupted
    ``repairing`` back to ``verification_failed`` (or an interrupted repair executor back to
    ``ready``) is continued against the already-written repair report so the attempt is not
    counted twice.
    """
    run = life.run
    record = run.task(task_id)
    report = newest_repair_report(run.run_dir, task_id)

    if record.status == "implemented":
        return (repo_relative(report, run.repo_root) if report else None), True

    if record.status == "verification_failed":
        if report is None:
            raise ExecutionError(
                f"{task_id} resumed at 'verification_failed' with no persisted repair report",
                "missing-repair-report")
        life.transition(
            task_id, "repairing", actor=ACTOR_RUNNER, note="resume: continue interrupted repair")
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


# --- verification gate + repair-report assembly ------------------------------------------


def _verify_gate(run: object, request: TaskExecution, gate: int) -> VerificationOutcome:
    """Run every declared verification command then both fresh verifiers for one attempt."""
    spec = request.spec
    task_id = spec.id
    commands_run = run_verification_commands(
        run, spec.verification_commands,
        stage=verification_stage(task_id, attempt=gate),
        task_id=task_id, attempt=gate, timeout=request.timeout,
    )
    evidence = build_verification_evidence(
        run, task_id, attempt=gate, commands_run=commands_run,
        claimed_checks=request.claimed_checks,
    )
    outcome = orchestrate_verification(
        run, spec, evidence,
        launchers=request.launchers, anchors=request.verifier_anchors,
        attempt=gate, plan_path=request.plan_path,
    )
    run.save()
    return outcome


def _classify(spec: TaskSpec, outcome: VerificationOutcome) -> tuple[list[str], list[str], list[str]]:
    """Split the gate's failure signal into (product defects, environment problems, regressions)."""
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


def _write_repair_report(
    run: object, spec: TaskSpec, gate: int, outcome: VerificationOutcome
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


def _read(path: Path) -> str:
    target = Path(path)
    return target.read_text(encoding="utf-8") if target.is_file() else ""


# --- durable block at the attempt limit / on an external block ---------------------------


def _block(
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
    """Block the task once, durably: run.json blocker, structured packet, task-file section,
    dependent suppression. Idempotent — a re-entry after a crash writes the same content."""
    run = life.run
    spec = request.spec
    task_id = spec.id

    if diagnostic is None:
        diagnostic = write_diagnostic_report(
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
                    "Repair attempts": f"{record.attempts} of {int(spec.max_repair_attempts)}",
                    "Diagnostic": repo_relative(diagnostic, run.repo_root),
                    "Recorded": _utcnow(),
                },
            )
        except OSError:
            pass

    run.save()
    return TaskRunResult(
        task_id, "blocked", record.attempts, gates, blocker, Path(diagnostic), tuple(passes))


# ============================================================================================
# Execute-mode integration (EMI-01): one public entry that drives the whole minimum engine.
# ============================================================================================
#
# :func:`execute_run` composes what the earlier phases built — the normalized task contract,
# durable schema-v2 lifecycle, deterministic adapter resolution and pinning, cross-process
# write leases, executor dispatch, independent verification, and the bounded repair loop — into
# one dependency-ordered orchestration that carries every selected task to ``verified`` or to a
# truthful non-zero terminal state. It deliberately stops after stage 9: no documentation,
# Graphify, final verification, release, archive, purge, or recovery code is reachable from
# here.


#: The non-terminal task states :func:`execute_run` will still drive forward. A fresh run only
#: ever presents ``ready``; the other three appear when a resume lands mid-flight (an executor
#: that reported ``implemented`` before a crash, an open repair round, a rolled-back window).
_ACTIONABLE_STATES = frozenset({"ready", "implemented", "verification_failed", "repairing"})

_SUPPRESSED_PREFIX = "blocked_by: "


@dataclass(frozen=True)
class ExecuteControls:
    """The real ``execute``-mode controls, each carrying whether the caller set it explicitly.

    ``max_repair_attempts`` overrides every selected task's declared bound when set. The two
    byte budgets and the selection/resume flags are persisted with their source
    (``explicit`` / ``default``) on ``run.json`` so a resume can be reasoned about against the
    controls the run was created with.
    """

    plan_approved: bool = False
    unattended: bool = False
    resume: bool = False
    adapter: str | None = None
    adapter_explicit: bool = False
    max_repair_attempts: int | None = None
    routine_output_byte_budget: int | None = None
    diagnostic_output_byte_budget: int | None = None
    task: str | None = None
    through: str | None = None

    @property
    def gate_opened(self) -> bool:
        """The first writer dispatch is allowed only once the plan gate is satisfied."""
        return bool(self.plan_approved or self.unattended)


@dataclass(frozen=True)
class ExecuteRequest:
    """Everything one ``execute`` invocation needs. The CLI fills this from resolved anchors;
    a test fills it directly with deterministic fake adapters."""

    feature: str
    repo_root: Path
    run_dir: Path
    prompt_path: Path | str
    plan_path: Path | str | None
    specs: tuple[TaskSpec, ...]
    adapter: Adapter
    launchers: VerifierLaunchers
    envelope_anchors: EnvelopeAnchors
    verifier_anchors: VerifierAnchors
    #: Maps an adapter name to a truthy value when it can run here. Passed verbatim to
    #: :func:`pipeline_core.adapter_resolution.resolve_adapter`; the core never probes a
    #: developer machine from inside this module.
    environment: Mapping[str, object] = field(default_factory=dict)
    controls: ExecuteControls = ExecuteControls()
    role_grant: tuple[str, ...] = DEFAULT_ROLE_GRANT
    execution_mode: str = "separate"
    #: The plan file path as it should appear in executor/verifier prompts (logical, not host).
    plan_prompt_path: str | None = None
    working_root: str = "."
    timeout: float | None = None
    #: Injected so a test can pin the lease owner; production uses this process.
    pipeline_pid: int | None = None


@dataclass(frozen=True)
class ExecuteResult:
    """The terminal outcome of one ``execute`` invocation."""

    status: str  # 'ok' | 'gate-pending' | 'blocked' | 'error'
    exit_code: int
    message: str
    run_dir: Path | None
    run_id: str | None = None
    task_results: tuple[TaskRunResult, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _error(message: str, request: ExecuteRequest | None = None,
           results: tuple[TaskRunResult, ...] = (), run_id: str | None = None) -> ExecuteResult:
    return ExecuteResult(
        "error", EXIT_ERROR, message,
        request.run_dir if request is not None else None, run_id, results)


def _select_ids(order: Sequence[str], task: str | None, through: str | None) -> list[str]:
    """Mirror the CLI's ``--task`` / ``--through`` selection against the plan order."""
    ids = list(order)
    if task is not None:
        if task not in ids:
            raise ExecutionError(f"--task names an unknown task: {task}", "unknown-task")
        return [task]
    if through is not None:
        if through not in ids:
            raise ExecutionError(f"--through names an unknown task: {through}", "unknown-task")
        return ids[: ids.index(through) + 1]
    return ids


def _controls_map(
    controls: ExecuteControls, resolution: AdapterResolution
) -> dict[str, tuple[object, str]]:
    """``{name: (value, sourced)}`` for :meth:`RunLifecycle.initialize` — every input control
    recorded with whether it was explicit or a default."""
    routine = controls.routine_output_byte_budget
    diagnostic = controls.diagnostic_output_byte_budget
    return {
        "mode": ("execute", "explicit"),
        "adapter_requested": (resolution.requested, "explicit" if controls.adapter_explicit
                              else "default"),
        "adapter_resolved": (resolution.resolved, resolution.sourced),
        "max_repair_attempts": (
            (controls.max_repair_attempts, "explicit")
            if controls.max_repair_attempts is not None else (None, "default")),
        "routine_output_byte_budget": (
            (routine, "explicit") if routine is not None
            else (ROUTINE_OUTPUT_BUDGET, "default")),
        "diagnostic_output_byte_budget": (
            (diagnostic, "explicit") if diagnostic is not None
            else (DIAGNOSTIC_OUTPUT_BUDGET, "default")),
        "resume": (controls.resume, "explicit" if controls.resume else "default"),
        "task": ((controls.task, "explicit") if controls.task is not None
                 else (None, "default")),
        "through": ((controls.through, "explicit") if controls.through is not None
                    else (None, "default")),
        "plan_approval": (
            "approve-plan" if controls.plan_approved
            else "unattended" if controls.unattended else "none", "explicit"),
    }


def _next_actionable(
    life: RunLifecycle, selected: Sequence[str], order: Sequence[str]
) -> str | None:
    """The first selected task, in plan order, the loop can still move forward.

    A task carrying a ``blocked_by:`` suppression marker is skipped — its blocked root has
    already ended the run.
    """
    chosen = set(selected)
    for task_id in order:
        if task_id not in chosen:
            continue
        record = life.run.task(task_id)
        if record.status not in _ACTIONABLE_STATES:
            continue
        if (record.blocker or "").startswith(_SUPPRESSED_PREFIX):
            continue
        return task_id
    return None


def _pending_reason(life: RunLifecycle, pending: Sequence[str]) -> str:
    """Explain why a selected task never reached ``verified`` — a fail-closed, truthful line."""
    parts: list[str] = []
    for task_id in pending:
        record = life.run.task(task_id)
        if record.status == "blocked":
            parts.append(f"{task_id} blocked: {record.blocker or 'see diagnostics'}")
        elif (record.blocker or "").startswith(_SUPPRESSED_PREFIX):
            parts.append(f"{task_id} suppressed ({record.blocker})")
        else:
            unmet = [dep for dep in record.depends_on
                     if life.run.task(dep).status != "verified"]
            if unmet:
                parts.append(
                    f"{task_id} dependency-not-satisfied: {', '.join(unmet)}")
            else:
                parts.append(
                    f"{task_id} did not reach verified (status '{record.status}')")
    return "; ".join(parts)


def _apply_repair_bound(
    specs: Sequence[TaskSpec], maximum: int | None
) -> dict[str, TaskSpec]:
    by_id = {spec.id: spec for spec in specs}
    if maximum is None:
        return by_id
    return {tid: replace(spec, max_repair_attempts=maximum) for tid, spec in by_id.items()}


def execute_run(request: ExecuteRequest) -> ExecuteResult:
    """Drive every selected task to ``verified`` or to a truthful non-zero terminal state.

    Contract (plan §"Execute Mode Integration"):

    * the first executor dispatch is refused until the plan gate is satisfied
      (``--approve-plan`` or an explicit ``--unattended`` opt-in) — ``EXIT_GATE_PENDING``;
    * the adapter is resolved from ``request.environment`` and pinned; an unknown/unavailable
      adapter, or a resume that would switch it, is ``EXIT_ERROR`` — nothing is substituted;
    * a fresh run persists a complete schema-v2 ``run.json`` (with every control's source)
      before any process exists; a resume reconciles the persisted run and continues;
    * the pipeline write lease is held for the whole loop and a per-task lease for each task;
      a live foreign lease is ``EXIT_BLOCKED``;
    * dependency-ready tasks run in plan order through :func:`run_task` (dispatch → the full
      independent-verification gate → bounded repair). A task that ends ``blocked`` ends the
      run ``EXIT_BLOCKED`` and its dependents are already suppressed;
    * success (``EXIT_OK``) requires *every* selected task ``verified`` — the run never reports
      success while any selected task is non-terminal.

    Stages 10–16 are never reached: no documentation, Graphify, final verification, release,
    archive, purge, or recovery code is called from here.
    """
    specs = list(request.specs)
    if not specs:
        return _error("execute mode needs at least one task in the plan", request)
    order = [spec.id for spec in specs]

    try:
        selected = _select_ids(order, request.controls.task, request.controls.through)
    except ExecutionError as exc:
        return _error(str(exc), request)

    # 1. The plan gate — no executor is dispatched in execute mode until it is satisfied.
    if not request.controls.gate_opened:
        return ExecuteResult(
            "gate-pending", EXIT_GATE_PENDING,
            "plan gate pending: approve with --approve-plan or opt in with --unattended; "
            "execute mode dispatches no executor until then.",
            None)

    # 2. Deterministic adapter resolution — fail closed, never substitute one adapter for
    #    another.
    try:
        resolution = resolve_adapter(request.controls.adapter, request.environment)
    except AdapterResolutionError as exc:
        return _error(f"{exc.code}: {exc}", request)

    by_id = _apply_repair_bound(specs, request.controls.max_repair_attempts)
    specs = [by_id[tid] for tid in order]

    # 3. Durable lifecycle: initialize a fresh run, or resume the persisted one.
    try:
        if request.controls.resume:
            life = RunLifecycle.resume(
                request.run_dir, request.repo_root,
                feature=request.feature, prompt_path=request.prompt_path,
                plan_path=request.plan_path,
                expected_tasks={spec.id: list(spec.depends_on) for spec in specs},
            )
            ensure_pinned_adapter(life.run, resolution)
            for name, (value, sourced) in _controls_map(request.controls, resolution).items():
                if sourced == "explicit":
                    life.run.set_control(name, value, sourced=sourced)
        else:
            run = Run.create(
                request.feature, request.prompt_path, request.plan_path,
                request.run_dir, request.repo_root)
            life = RunLifecycle.initialize(
                run,
                tasks=[(spec.id, list(spec.depends_on)) for spec in specs],
                controls=_controls_map(request.controls, resolution),
                adapter_requested=resolution.requested,
                adapter_resolved=resolution.resolved,
            )
    except (StateError, ResumeError, AdapterResolutionError) as exc:
        return _error(f"{getattr(exc, 'code', 'state-error')}: {exc}", request)

    pin_adapter(life.run, resolution)
    life.run.save()

    # 4. Cross-process write lease for the whole run.
    pid = request.pipeline_pid if request.pipeline_pid is not None else os.getpid()
    lease = pipeline_lease(request.repo_root, life.run.run_id, pid)
    try:
        lease.acquire()
    except LeaseHeldError as exc:
        return ExecuteResult(
            "blocked", EXIT_BLOCKED, f"{exc.code}: {exc}",
            request.run_dir, life.run.run_id)

    results: list[TaskRunResult] = []
    try:
        while True:
            task_id = _next_actionable(life, selected, order)
            if task_id is None:
                break
            spec = by_id[task_id]
            t_lease = task_lease(request.repo_root, life.run.run_id, task_id, pid)
            try:
                t_lease.acquire(task_id)
            except LeaseHeldError as exc:
                return ExecuteResult(
                    "blocked", EXIT_BLOCKED, f"{exc.code}: {exc}",
                    request.run_dir, life.run.run_id, tuple(results))
            try:
                outcome = run_task(
                    life,
                    TaskExecution(
                        spec=spec,
                        adapter=request.adapter,
                        launchers=request.launchers,
                        verifier_anchors=request.verifier_anchors,
                        envelope_anchors=request.envelope_anchors,
                        role_grant=tuple(request.role_grant),
                        execution_mode=request.execution_mode,
                        plan_path=request.plan_prompt_path,
                        working_root=request.working_root,
                        timeout=request.timeout,
                    ),
                )
            except (ExecutionError, DispatchError) as exc:
                return _error(
                    f"{getattr(exc, 'code', 'execution-error')}: {exc}",
                    request, tuple(results), life.run.run_id)
            finally:
                t_lease.release()

            results.append(outcome)
            if outcome.status != "verified":
                life.run.status = "blocked"
                life.run.save()
                return ExecuteResult(
                    "blocked", EXIT_BLOCKED,
                    f"{task_id} ended '{outcome.status}': "
                    f"{outcome.blocker or 'see the persisted diagnostic'}",
                    request.run_dir, life.run.run_id, tuple(results))
            life.recompute_readiness()

        pending = [tid for tid in selected if life.run.task(tid).status != "verified"]
        if not pending:
            life.run.status = "verified"
            life.run.save()
            return ExecuteResult(
                "ok", EXIT_OK,
                f"execute complete: {len(selected)} selected task(s) verified. Stopped after "
                f"stage 9 — documentation, Graphify, final verification, release, and "
                f"archive/purge are not run in execute mode.",
                request.run_dir, life.run.run_id, tuple(results))
        life.run.status = "blocked"
        life.run.save()
        return ExecuteResult(
            "blocked", EXIT_BLOCKED, _pending_reason(life, pending),
            request.run_dir, life.run.run_id, tuple(results))
    finally:
        lease.release()
