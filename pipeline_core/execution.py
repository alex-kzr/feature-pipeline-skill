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

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from feature_pipeline.application.pipeline_engine import (
    CallableStage,
    PipelineEngine,
    TaskExecutionStage,
)
from feature_pipeline.application.task_engine import (
    ExecutionError,
    RepairPass,
    TaskEngine,
    TaskExecution,
    TaskRunResult,
)
from feature_pipeline.domain.plan import CompiledRunPlan
from feature_pipeline.domain.stages import (
    StageContext,
    StageHandler,
    StageOutcome,
    StageStatus,
    compile_stage_sequence,
)
from feature_pipeline.domain.vocabulary import StageId
from schemas.contracts import TaskSpec

from .adapter_resolution import (
    AdapterResolution,
    AdapterResolutionError,
    ensure_pinned_adapter,
    pin_adapter,
    resolve_adapter,
)
from .adapters import Adapter
from .commands import (
    DIAGNOSTIC_OUTPUT_BUDGET,
    ROUTINE_OUTPUT_BUDGET,
)
from .concurrency import pipeline_lease, task_lease
from .dispatch import DispatchError
from .lease import LeaseHeldError
from .lifecycle import RunLifecycle
from .prompt_envelope import EnvelopeAnchors
from .state import ResumeError, Run, StateError, repo_relative
from .verification import (
    VerifierAnchors,
    VerifierLaunchers,
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

#: A safe ``--attest-dependency`` ``SOURCE_FEATURE`` token: a bare directory name under the
#: run-storage root — no path separator, ``.``, ``..``, or absolute/drive-qualified path.
#: Enforced here independently of the CLI's own syntax check (defense in depth).
_SOURCE_FEATURE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_task(life: RunLifecycle, request: TaskExecution) -> TaskRunResult:
    """Drive ``request.spec`` from ``ready`` (or a resumed ``implemented``/repair state) to a
    terminal ``verified`` or ``blocked``, including bounded repair.

    Thin compatibility wrapper: the bounded repair loop, the independent-verification gate, and
    the pre-transition diagnostic all live in one service path now —
    :class:`feature_pipeline.application.task_engine.TaskEngine`, which sequences
    :class:`~feature_pipeline.application.verification_service.VerificationService` and
    :class:`~feature_pipeline.application.diagnostic_service.DiagnosticService`. This module
    keeps the ``run_task`` / :class:`TaskExecution` / :class:`TaskRunResult` names the
    execute-mode integration and the existing tests import.
    """
    return TaskEngine().run(life, request)


# ============================================================================================
# PE-02: stages 5-9 (task selection, executor routing, implementation/verification/repair) run
# through the one explicit ``PipelineEngine`` (PE-01) — no second production stage sequence.
# ============================================================================================
#
# Task selection (stage 5) and executor routing (stage 6) are no longer *decisions* made at
# task-loop time — CP-01/CP-02 resolved both once, into ``CompiledRunPlan``, before any task
# ran. These two handlers make that already-resolved decision visible on the engine's outcome
# trail; they never re-decide it. Stages 7-9 stay exactly what PE-01 built:
# :class:`~feature_pipeline.application.pipeline_engine.TaskExecutionStage` delegating whole to
# :class:`TaskEngine`.


def _task_selection_stage(context: StageContext) -> StageOutcome:
    """Stage 5 — the task ``execute_run``'s loop already chose (dependency-ready, plan order)."""
    task_id = cast(str, context.resource("selected_task_id"))
    return StageOutcome(
        StageId.TASK_SELECTION, StageStatus.COMPLETED,
        detail=f"{task_id} selected (dependency-ready, plan order)",
        evidence={"task_id": task_id},
    )


def _executor_routing_stage(context: StageContext) -> StageOutcome:
    """Stage 6 — the adapter/role the compiled plan already routed this task to
    (:class:`~feature_pipeline.domain.plan.ResolvedTask`)."""
    task_id = cast(str, context.resource("selected_task_id"))
    resolved = context.plan.task(task_id)
    return StageOutcome(
        StageId.EXECUTOR_ROUTING, StageStatus.COMPLETED,
        detail=f"{task_id} routed to adapter {resolved.adapter!r}, role {resolved.role!r}",
        evidence={"task_id": task_id, "adapter": resolved.adapter, "role": resolved.role},
    )


_STAGE_HANDLERS: Mapping[StageId, StageHandler] = {
    StageId.TASK_SELECTION: CallableStage(_task_selection_stage),
    StageId.EXECUTOR_ROUTING: CallableStage(_executor_routing_stage),
    StageId.IMPLEMENTATION: TaskExecutionStage(TaskEngine()),
}

#: The one compiled sequence for stages 5-9 — built once at import time (the handler set never
#: changes at runtime) and reused for every selected task.
_STAGE_SEQUENCE = compile_stage_sequence(
    [StageId.TASK_SELECTION, StageId.EXECUTOR_ROUTING, StageId.IMPLEMENTATION],
    _STAGE_HANDLERS,
)


def _run_selected_task(
    life: RunLifecycle, plan: CompiledRunPlan | None, task_id: str, request: TaskExecution,
) -> TaskRunResult:
    """Drive one dependency-ready task to a terminal state.

    When a compiled plan is present — the real CLI path (CP-02), every ``execute`` invocation
    that reaches here through :mod:`pipeline_core.runner_cli` — this is the *only* production
    call into :class:`~feature_pipeline.application.pipeline_engine.PipelineEngine`: stages 5-9
    run through the one compiled sequence, and the terminal
    :class:`~feature_pipeline.application.task_engine.TaskRunResult` is recovered from the
    ``"task_result_sink"`` resource the ``IMPLEMENTATION`` stage populates (never re-dispatched
    to read it back). ``plan is None`` keeps the pre-CP-02 wiring :func:`run_task` used
    directly — the focused unit scenarios that construct an :class:`ExecuteRequest` without a
    compiled plan; that surface is unchanged by PE-02.
    """
    if plan is None:
        return run_task(life, request)
    sink: list[TaskRunResult] = []
    PipelineEngine(_STAGE_SEQUENCE).run(
        plan,
        resources={
            "lifecycle": life,
            "task_execution": request,
            "selected_task_id": task_id,
            "task_result_sink": sink,
        },
    )
    return sink[0]


# ============================================================================================
# Execute-mode integration (EMI-01): one public entry that drives the whole minimum engine.
# ============================================================================================
#
# :func:`execute_run` composes what the earlier phases built — the normalized task contract,
# durable schema-v2 lifecycle, deterministic adapter resolution and pinning, cross-process
# write leases, executor dispatch, independent verification, and the bounded repair loop — into
# one dependency-ordered orchestration that carries every selected task to ``verified`` or to a
# truthful non-zero terminal state. It deliberately stops after stage 9: no documentation,
# knowledge-graph refresh, final verification, release, archive, purge, or recovery code is
# reachable from here.


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
    #: ``(dep_id, source_feature)`` pairs from repeated ``--attest-dependency`` flags. Valid
    #: only alongside ``task`` — ``--through`` resolves its own dependency closure and must
    #: never trust an external attestation instead.
    attested_dependencies: tuple[tuple[str, str], ...] = ()

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
    #: The one immutable execution plan (CP-02). When set, ``execute_run`` takes its
    #: selection, per-task working root and role grant, and repair bound from here — the same
    #: object the dry-run preview renders — and persists its fingerprint so a resume can
    #: report field-level plan incompatibility. ``None`` keeps the pre-CP-02 wiring for the
    #: focused unit scenarios that construct a request directly.
    compiled_plan: CompiledRunPlan | None = None
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


# --- --attest-dependency: a read-only bridge from an already-closed run's verified task --------


def _validate_attestation_scope(
    controls: ExecuteControls, by_id: Mapping[str, TaskSpec]
) -> None:
    """Shape/scope checks that need no filesystem access — enforced before anything else runs."""
    attestations = controls.attested_dependencies
    if not attestations:
        return
    if controls.task is None:
        raise ExecutionError(
            "--attest-dependency is only valid with --task; --through resolves its own "
            "dependency closure and must not trust an external attestation instead",
            "attestation-requires-task-scope")
    declared = set(by_id[controls.task].depends_on)
    seen: set[str] = set()
    for dep_id, source_feature in attestations:
        if dep_id in seen:
            raise ExecutionError(
                f"--attest-dependency names {dep_id} more than once",
                "duplicate-attestation-dependency")
        seen.add(dep_id)
        if dep_id not in declared:
            raise ExecutionError(
                f"{dep_id} is not a declared dependency of {controls.task}",
                "attestation-not-a-dependency")
        _validate_source_feature(source_feature)


def _validate_source_feature(source_feature: str) -> None:
    if not _SOURCE_FEATURE_RE.match(source_feature):
        raise ExecutionError(
            f"--attest-dependency source feature '{source_feature}' is not a bare directory "
            "name (letters, digits, '-', '_' only; no path separator, '.', '..', or "
            "absolute/drive-qualified path)",
            "attestation-unsafe-source")


def _resolve_source_run_dir(run_dir: Path, source_feature: str) -> Path:
    """Resolve ``source_feature`` under the same run-storage root as ``run_dir`` and re-check
    containment after resolution — an independent path safe-guard even when the CLI-level
    syntax check already ran (defense in depth against a planted symlink)."""
    _validate_source_feature(source_feature)
    storage_root = run_dir.parent.resolve()
    candidate = (storage_root / source_feature).resolve()
    try:
        candidate.relative_to(storage_root)
    except ValueError:
        raise ExecutionError(
            f"--attest-dependency source feature '{source_feature}' escapes the run-storage "
            "root",
            "attestation-unsafe-source") from None
    return candidate


def _resolve_attestation(
    *, dep_id: str, source_feature: str, run_dir: Path, repo_root: Path,
    prompt_path: Path | str, plan_path: Path | str | None,
) -> dict[str, Any]:
    """Read-only: resolve and validate one attestation's source run. Never writes to it."""
    source_dir = _resolve_source_run_dir(run_dir, source_feature)
    run_json = source_dir / "run.json"
    if not run_json.is_file():
        raise ExecutionError(
            f"attestation source run '{source_feature}' is missing or unreadable",
            "attestation-source-missing")
    try:
        source_run = Run.load(source_dir, repo_root)
    except StateError:
        raise ExecutionError(
            f"attestation source run '{source_feature}' is missing or unreadable",
            "attestation-source-missing") from None

    this_prompt = repo_relative(prompt_path, repo_root)
    this_plan = repo_relative(plan_path, repo_root) if plan_path else None
    if source_run.prompt_path != this_prompt or source_run.plan_path != this_plan:
        raise ExecutionError(
            f"attestation source run '{source_feature}' tracks a different prompt/plan than "
            "this run — evidence for a different feature is not evidence for this one",
            "attestation-source-identity-mismatch")

    try:
        source_task = source_run.task(dep_id)
    except StateError:
        raise ExecutionError(
            f"attestation source run '{source_feature}' does not track {dep_id}",
            "attestation-source-dependency-absent") from None

    if source_task.status != "verified":
        raise ExecutionError(
            f"attestation source run '{source_feature}' has {dep_id} at status "
            f"'{source_task.status}', not 'verified'",
            "attestation-source-not-verified")

    digest = hashlib.sha256(run_json.read_bytes()).hexdigest()
    return {
        "dep_id": dep_id,
        "source_feature": source_feature,
        "source_run_id": source_run.run_id,
        "source_digest": f"sha256:{digest}",
        "task_verdict": source_task.verification.get("task_verdict"),
        "test_verdict": source_task.verification.get("test_verdict"),
        "verified_at": source_task.verification.get("verified_at"),
        "attested_at": _utcnow(),
    }


def _attestation_control_value(controls: ExecuteControls) -> list[str]:
    return [f"{dep_id}={source}" for dep_id, source in controls.attested_dependencies]


def _ensure_attestations_match(run: Run, requested: tuple[tuple[str, str], ...]) -> None:
    """A resume that also passes ``--attest-dependency`` must name exactly the recorded set
    (any order); a bare resume keeps whatever was recorded. There is no code path to add or
    replace an attestation on an existing run."""
    if not requested:
        return
    persisted = set(run.controls.get("attest_dependency", {}).get("value") or [])
    requested_set = {f"{dep_id}={source}" for dep_id, source in requested}
    if requested_set != persisted:
        raise ExecutionError(
            "resume --attest-dependency does not name exactly the recorded attestation set",
            "attestation-mismatch")


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
        "attest_dependency": (
            (_attestation_control_value(controls), "explicit")
            if controls.attested_dependencies else ([], "default")),
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


def _plan_fingerprint(plan: CompiledRunPlan) -> dict[str, str]:
    """A flat ``{field-path: value}`` map over every resolved decision in ``plan``.

    Persisted verbatim on ``run.json`` (control ``plan_fingerprint``); a resume recomputes it
    from the freshly compiled plan and reports the **first** key whose value moved (AC-3).
    The order mirrors ``feature_pipeline.domain.plan._canonical_plan`` so the first
    divergence a reader sees is the most significant one.
    """
    out: dict[str, str] = {
        "selection_mode": plan.selection_mode,
        "selection": ",".join(plan.selection),
        "order": ",".join(plan.order),
        "adapter": plan.adapter,
    }
    for control in plan.controls:
        out[f"control.{control.name}"] = f"{control.value!r} ({control.source})"
    for task in plan.tasks:
        prefix = f"task.{task.task_id}"
        out[f"{prefix}.working_root"] = str(task.working_root)
        out[f"{prefix}.storage_root"] = str(task.storage_root)
        out[f"{prefix}.role"] = task.role
        out[f"{prefix}.role_grant"] = ",".join(task.role_grant)
        out[f"{prefix}.adapter"] = task.adapter
        out[f"{prefix}.checks"] = "; ".join(
            f"{check.name}:{' '.join(check.argv)}" for check in task.checks
        )
        out[f"{prefix}.timeout_s"] = repr(task.timeout_s)
        out[f"{prefix}.routine_output_budget"] = repr(task.routine_output_budget)
        out[f"{prefix}.diagnostic_output_budget"] = repr(task.diagnostic_output_budget)
        out[f"{prefix}.repair_bound"] = repr(int(task.repair_bound))
        out[f"{prefix}.depends_on"] = ",".join(task.depends_on)
        for control in task.controls:
            out[f"{prefix}.control.{control.name}"] = (
                f"{control.value!r} ({control.source})"
            )
    return out


def _ensure_plan_compatible(run: Run, plan: CompiledRunPlan) -> None:
    """Resume guard: the freshly compiled plan must match the recorded one field for field.

    Raises :class:`ExecutionError` naming the first divergent field, before the run is
    mutated in any way (AC-3).
    """
    recorded = run.controls.get("plan_fingerprint", {}).get("value")
    now = _plan_fingerprint(plan)
    if not isinstance(recorded, dict):
        # A run created before CP-02 (or by the pre-CP-02 wiring) recorded no fingerprint;
        # a bare digest check is the strongest statement available.
        recorded_digest = run.controls.get("plan_digest", {}).get("value")
        if recorded_digest is not None and recorded_digest != plan.digest:
            raise ExecutionError(
                "resume: the recorded run's plan digest does not match the plan compiled "
                "now; recompile from the same inputs or start a new run",
                "plan-incompatible",
            )
        return
    for key in list(recorded) + [k for k in now if k not in recorded]:
        before = recorded.get(key, "<absent>")
        after = now.get(key, "<absent>")
        if before != after:
            raise ExecutionError(
                f"resume: compiled plan is incompatible with the recorded run at "
                f"'{key}': recorded={before!r}, now={after!r}",
                "plan-incompatible",
            )


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

    Stages 10–16 are never reached: no documentation, knowledge-graph refresh, final
    verification, release, archive, purge, or recovery code is called from here.
    """
    specs = list(request.specs)
    if not specs:
        return _error("execute mode needs at least one task in the plan", request)
    order = [spec.id for spec in specs]
    spec_by_id = {spec.id: spec for spec in specs}
    plan = request.compiled_plan

    try:
        if plan is not None:
            # The compiled plan already resolved the selection (through
            # ``resolve_selection``); trust it and only re-run the shape checks.
            selected = list(plan.selection)
        else:
            selected = _select_ids(order, request.controls.task, request.controls.through)
        _validate_attestation_scope(request.controls, spec_by_id)
    except ExecutionError as exc:
        return _error(f"{exc.code}: {exc}", request)

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

    if plan is not None:
        planned_ids = {task.task_id for task in plan.tasks}
        by_id = {
            tid: (
                replace(spec_by_id[tid],
                        max_repair_attempts=int(plan.task(tid).repair_bound))
                if tid in planned_ids else spec_by_id[tid]
            )
            for tid in order
        }
    else:
        by_id = _apply_repair_bound(specs, request.controls.max_repair_attempts)
    specs = [by_id[tid] for tid in order]

    controls_map = _controls_map(request.controls, resolution)
    if plan is not None:
        controls_map["plan_digest"] = (plan.digest, "explicit")
        controls_map["plan_fingerprint"] = (_plan_fingerprint(plan), "explicit")

    # 3. Durable lifecycle: initialize a fresh run, or resume the persisted one. A fresh run's
    #    attestations are resolved against their source runs before any run.json exists
    #    (AC-2's "no partial state" on a denial); a resume trusts whatever was recorded.
    try:
        if request.controls.resume:
            life = RunLifecycle.resume(
                request.run_dir, request.repo_root,
                feature=request.feature, prompt_path=request.prompt_path,
                plan_path=request.plan_path,
                expected_tasks={spec.id: list(spec.depends_on) for spec in specs},
            )
            ensure_pinned_adapter(life.run, resolution)
            _ensure_attestations_match(life.run, request.controls.attested_dependencies)
            if plan is not None:
                _ensure_plan_compatible(life.run, plan)
            for name, (value, sourced) in controls_map.items():
                if sourced == "explicit":
                    life.run.set_control(name, value, sourced=sourced)
        else:
            attestations = [
                _resolve_attestation(
                    dep_id=dep_id, source_feature=source_feature,
                    run_dir=request.run_dir, repo_root=request.repo_root,
                    prompt_path=request.prompt_path, plan_path=request.plan_path)
                for dep_id, source_feature in request.controls.attested_dependencies
            ]
            run = Run.create(
                request.feature, request.prompt_path, request.plan_path,
                request.run_dir, request.repo_root)
            life = RunLifecycle.initialize(
                run,
                tasks=[(spec.id, list(spec.depends_on)) for spec in specs],
                controls=controls_map,
                adapter_requested=resolution.requested,
                adapter_resolved=resolution.resolved,
            )
            if attestations:
                for evidence in attestations:
                    life.run.record_attestation(
                        request.controls.task, evidence["dep_id"], evidence)
                life.recompute_readiness()
    except (StateError, ResumeError, AdapterResolutionError, ExecutionError) as exc:
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
                outcome = _run_selected_task(
                    life, plan, task_id,
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
                f"stage 9 — documentation, knowledge-graph refresh, final verification, "
                f"release, and archive/purge are not run in execute mode.",
                request.run_dir, life.run.run_id, tuple(results))
        life.run.status = "blocked"
        life.run.save()
        return ExecuteResult(
            "blocked", EXIT_BLOCKED, _pending_reason(life, pending),
            request.run_dir, life.run.run_id, tuple(results))
    finally:
        lease.release()
