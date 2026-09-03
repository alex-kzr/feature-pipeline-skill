"""Stages 10–16: the post-task lifecycle, modeled explicitly over one durable run-state
contract.

The minimum execution engine (:mod:`pipeline_core.execution`) drives every selected task to
``verified`` and stops after stage 9. This module owns the stages that may run *after* that,
each behind an explicit guard:

===== ===================== ================================================================
Stage Name                  Guard
===== ===================== ================================================================
10    ``documentation``     unreachable until **every selected task** is ``verified`` (AC-1)
11    ``documentation-audit`` runs in a context that is never the stage-10 context
12    ``graphify-refresh``  unreachable unless stage 11 returned ``PASS`` (AC-2); invokes
                            only the project-configured wrapper; enforces the declared
                            expected outputs and ``tracked-empty`` diff policy; refuses a
                            ``graphify … install`` argv (AC-3)
13    ``graphify-verification`` independently re-checks every configured output and the
                            tracked-empty policy (AC-3)
14    ``final-verification`` unreachable until stage 13 is ``PASS``; runs only the
                            project-declared final verification commands, sequentially
15    ``final-diff-approval`` a human gate; ``BLOCKED`` (pending) without an explicit
                            approval, but the lifecycle still emits stage 16's dry run
16    ``release``           a deterministic, non-mutating release plan; a real commit needs
                            an explicit commit control + an approved final diff + a passing
                            final verification, and even then nothing is committed or pushed
===== ===================== ================================================================

Stages 14–16 (:mod:`pipeline_core.release`) run only when the request carries a
:class:`~pipeline_core.release.ReleasePolicy`; without one the lifecycle ends after stage 13,
unchanged.

Every stage verdict is flushed to ``run.json`` under ``stages[<name>]`` via
:meth:`pipeline_core.state.Run.record_stage_outcome`, so a restart resumes at the first stage
that has not yet recorded a ``PASS``. No stage here has a Git push or commit code path — an
argv that names ``push`` / ``commit`` is refused before it can run.

Composition, not a new CLI mode: :func:`run_post_task_lifecycle` is called with the run's
:class:`~pipeline_core.lifecycle.RunLifecycle`, the loaded
:class:`~pipeline_core.integrations.GraphifyIntegration`, and two injected work contexts
(documentation maintenance and its independent audit).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from schemas import SchemaError, ToolStage

from .integrations import GraphifyIntegration
from .lifecycle import RunLifecycle
from .release import (
    FINAL_DIFF_APPROVAL,
    FINAL_VERIFICATION,
    RELEASE,
    RELEASE_STAGES,
    DiffProbe,
    PointerProbe,
    ReleasePolicy,
    final_diff_result,
    final_verification_result,
    release_plan_result,
)
from .stages import BLOCKED, FAIL, PASS, CommandRunner, TrackedChangesProbe, run_tool_stage
from .commands import run_command

DOCUMENTATION = "documentation"
DOCUMENTATION_AUDIT = "documentation-audit"
GRAPHIFY_REFRESH = "graphify-refresh"
GRAPHIFY_VERIFICATION = "graphify-verification"

#: The explicit, ordered post-task lifecycle: ``(stage-number, stage-name)``. Stages 14–16
#: (from :mod:`pipeline_core.release`) run only when the request carries a ``ReleasePolicy``.
POST_TASK_STAGES: tuple[tuple[int, str], ...] = (
    (10, DOCUMENTATION),
    (11, DOCUMENTATION_AUDIT),
    (12, GRAPHIFY_REFRESH),
    (13, GRAPHIFY_VERIFICATION),
) + RELEASE_STAGES
STAGE_NUMBERS = {name: number for number, name in POST_TASK_STAGES}

#: argv verbs no post-task stage may run — this lifecycle produces evidence, never a mutation.
FORBIDDEN_STAGE_VERBS = frozenset({"push", "commit", "tag"})

_JSON_SCALARS = (str, int, float, bool, type(None))


class PostTaskError(RuntimeError):
    """A post-task lifecycle precondition failure. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DocumentationRequest:
    """The facts handed to the stage-10 documentation-maintenance context."""

    run_id: str
    feature: str
    documentation_impact: tuple[str, ...]
    verified_tasks: tuple[str, ...]


@dataclass(frozen=True)
class AuditRequest:
    """The facts handed to the stage-11 independent documentation-audit context."""

    run_id: str
    feature: str
    documentation_impact: tuple[str, ...]
    documentation_report: str | None


#: Stage-10 work: returns ``{"status": "implemented" | <other>, "report": <rel path>, ...}``.
Documenter = Callable[[DocumentationRequest], Mapping[str, object]]
#: Stage-11 work: returns ``{"verdict": "PASS" | "FAIL" | "BLOCKED", "report": <rel path>?}``.
Auditor = Callable[[AuditRequest], Mapping[str, object]]


@dataclass(frozen=True)
class StageResult:
    """One post-task stage's verdict and the reasons behind it."""

    stage: str
    number: int
    verdict: str  # PASS | FAIL | BLOCKED
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PostTaskOutcome:
    """The terminal shape of one post-task lifecycle invocation."""

    status: str  # 'complete' | 'gate-pending' | 'blocked'
    stages: tuple[StageResult, ...]

    @property
    def ok(self) -> bool:
        return self.status == "complete"

    def stage(self, name: str) -> StageResult | None:
        for result in self.stages:
            if result.stage == name:
                return result
        return None


@dataclass(frozen=True)
class PostTaskRequest:
    """Everything :func:`run_post_task_lifecycle` needs beyond the run itself."""

    selected_task_ids: tuple[str, ...]
    integration: GraphifyIntegration
    root: Path
    documenter: Documenter
    auditor: Auditor
    documentation_impact: tuple[str, ...] = ()
    #: Which wrapper file under ``integration.wrapper_dir`` to invoke: ``"sh"`` -> ``build.sh``
    #: (``bash …``), ``"ps1"`` -> ``build.ps1`` (``pwsh -File …``).
    wrapper: str = "sh"
    #: Override the resolved wrapper argv (tests inject a fake program, or a forbidden one).
    graphify_argv: tuple[str, ...] | None = None
    command_runner: CommandRunner = run_command
    tracked_changes: TrackedChangesProbe | None = None
    #: Stages 14–16 run only when a release policy is supplied; without one the lifecycle
    #: ends after stage 13, exactly as before.
    release_policy: ReleasePolicy | None = None
    final_diff_approved: bool = False
    commit_control: bool = False
    diff_probe: DiffProbe | None = None
    submodule_pointer_probe: PointerProbe | None = None
    verification_timeout: float | None = None


def run_post_task_lifecycle(life: RunLifecycle, request: PostTaskRequest) -> PostTaskOutcome:
    """Drive stages 10–13 (and, with a ``release_policy``, 14–16) in order, stopping at the
    first guard that is not satisfied.

    Restart-safe: a stage already recorded ``PASS`` on ``run.json`` is not re-run.
    """
    run = life.run
    _validate_request(run, request)
    results: list[StageResult] = []

    # --- Stage 10 guard (AC-1): documentation is unreachable while any selected task is
    #     not yet 'verified'. Nothing is dispatched; the run simply is not ready.
    unverified = [tid for tid in request.selected_task_ids
                  if run.task(tid).status != "verified"]
    if unverified:
        pending = StageResult(
            DOCUMENTATION, 10, BLOCKED,
            (f"documentation cannot begin: selected task(s) not verified: "
             f"{', '.join(unverified)}",),
        )
        _record(life, pending)
        return PostTaskOutcome("gate-pending", (pending,))

    # --- Stage 10: documentation maintenance, in its own context.
    r10 = _recorded(run, DOCUMENTATION)
    if r10 is None or r10.verdict != PASS:
        doc = dict(request.documenter(DocumentationRequest(
            run.run_id, run.feature, tuple(request.documentation_impact),
            tuple(request.selected_task_ids))))
        if str(doc.get("status")) != "implemented":
            r10 = StageResult(
                DOCUMENTATION, 10, BLOCKED,
                (f"documentation context did not complete "
                 f"({doc.get('failure') or doc.get('status')})",),
                _clean(doc))
            _record(life, r10)
            results.append(r10)
            return PostTaskOutcome("blocked", tuple(results))
        r10 = StageResult(DOCUMENTATION, 10, PASS, (), _clean(doc))
        _record(life, r10)
    results.append(r10)

    # --- Stage 11: independent documentation audit, in a *separate* context.
    if request.auditor is request.documenter:
        raise PostTaskError(
            "stage 11 (documentation audit) must run in a context separate from stage 10 "
            "(documentation maintenance)",
            "audit-not-independent")
    r11 = _recorded(run, DOCUMENTATION_AUDIT)
    if r11 is None or r11.verdict != PASS:
        report = (r10.evidence or {}).get("report") if r10.evidence else None
        audit = dict(request.auditor(AuditRequest(
            run.run_id, run.feature, tuple(request.documentation_impact),
            report if isinstance(report, str) else None)))
        verdict = _verdict_token(audit.get("verdict"))
        r11 = StageResult(
            DOCUMENTATION_AUDIT, 11, verdict,
            () if verdict == PASS else (f"documentation audit returned {verdict}",),
            _clean(audit))
        _record(life, r11)
    results.append(r11)
    if r11.verdict != PASS:
        # AC-2: Graphify stays unreachable. A FAIL blocks; a BLOCKED is an external condition.
        return PostTaskOutcome("blocked" if r11.verdict == FAIL else "gate-pending",
                               tuple(results))

    # --- Stage 12: Graphify refresh through the project-configured wrapper only.
    r12 = _recorded(run, GRAPHIFY_REFRESH)
    if r12 is None or r12.verdict != PASS:
        r12 = _run_graphify_refresh(life, request)
        _record(life, r12)
    results.append(r12)
    if r12.verdict != PASS:
        return PostTaskOutcome("blocked", tuple(results))

    # --- Stage 13: independent verification of every configured Graphify output.
    r13 = _run_graphify_verification(life, request)
    _record(life, r13)
    results.append(r13)
    if r13.verdict != PASS:
        return PostTaskOutcome("blocked", tuple(results))

    if request.release_policy is None:
        return PostTaskOutcome("complete", tuple(results))

    return _run_release_tail(life, request, results)


def _run_release_tail(
    life: RunLifecycle, request: PostTaskRequest, results: list[StageResult]
) -> PostTaskOutcome:
    """Stages 14–16: final verification, the human final-diff gate, and the release plan."""
    run = life.run
    policy = request.release_policy
    assert policy is not None  # guarded by the caller

    # --- Stage 14: final verification. AC-1 — unreachable until Graphify verification passed.
    r14 = _recorded(run, FINAL_VERIFICATION)
    if r14 is None or r14.verdict != PASS:
        graphify = _recorded(run, GRAPHIFY_VERIFICATION)
        if graphify is None or graphify.verdict != PASS:
            r14 = StageResult(
                FINAL_VERIFICATION, 14, BLOCKED,
                ("final verification cannot run before Graphify verification passes",))
        else:
            verdict, reasons, evidence = final_verification_result(
                run, policy, command_runner=request.command_runner,
                timeout=request.verification_timeout)
            r14 = StageResult(FINAL_VERIFICATION, 14, verdict, reasons, evidence)
        _record(life, r14)
    results.append(r14)
    if r14.verdict != PASS:
        return PostTaskOutcome("blocked", tuple(results))

    # --- Stage 15: the human final-diff gate. A missing approval records BLOCKED (pending)
    #     but does NOT halt — stage 16 still emits its non-mutating dry run (AC-2).
    verdict, reasons, evidence = final_diff_result(
        approved=request.final_diff_approved, diff_probe=request.diff_probe)
    r15 = StageResult(FINAL_DIFF_APPROVAL, 15, verdict, reasons, evidence)
    _record(life, r15)
    results.append(r15)

    # --- Stage 16: the release plan — always non-mutating; PASS means "plan emitted".
    verdict, reasons, plan = release_plan_result(
        policy,
        final_verification_pass=r14.verdict == PASS,
        final_diff_approved=r15.verdict == PASS,
        commit_control=request.commit_control,
        submodule_pointer_probe=request.submodule_pointer_probe,
    )
    r16 = StageResult(RELEASE, 16, verdict, reasons, plan)
    _record(life, r16)
    results.append(r16)
    if r16.verdict != PASS:
        return PostTaskOutcome("blocked", tuple(results))

    # A real commit was authorized (control + approval + passing verification) -> complete;
    # otherwise the run legitimately ends at a non-mutating release dry run, still pending a
    # human gate / commit control.
    complete = bool(plan.get("would_commit"))
    return PostTaskOutcome("complete" if complete else "gate-pending", tuple(results))


# --- stage 12 / 13 internals -------------------------------------------------------------


def _wrapper_argv(request: PostTaskRequest) -> tuple[str, ...]:
    if request.graphify_argv is not None:
        return tuple(request.graphify_argv)
    name = "build.ps1" if request.wrapper == "ps1" else "build.sh"
    rel = f"{request.integration.wrapper_dir}/{name}"
    return ("pwsh", "-File", rel) if request.wrapper == "ps1" else ("bash", rel)


def _run_graphify_refresh(life: RunLifecycle, request: PostTaskRequest) -> StageResult:
    integration = request.integration
    argv = _wrapper_argv(request)

    if integration.forbids(argv):
        return StageResult(
            GRAPHIFY_REFRESH, 12, BLOCKED,
            (f"refusing a forbidden Graphify installer command: {' '.join(argv)}",),
            {"argv": list(argv)})
    mutating = sorted({token.lower() for token in argv} & FORBIDDEN_STAGE_VERBS)
    if mutating:
        return StageResult(
            GRAPHIFY_REFRESH, 12, BLOCKED,
            (f"refusing a Graphify argv that names a mutating verb: {', '.join(mutating)}",),
            {"argv": list(argv)})

    name = "build.ps1" if request.wrapper == "ps1" else "build.sh"
    try:
        stage = ToolStage.from_data({
            "name": integration.stage,
            "subagents": ["executor"],
            "argv": list(argv),
            "prerequisites": [f"{integration.wrapper_dir}/{name}"]
            if request.graphify_argv is None else [],
            "outputs": list(integration.expected_outputs),
            "diff_policy": integration.diff_policy,
        })
    except SchemaError as exc:
        return StageResult(GRAPHIFY_REFRESH, 12, BLOCKED,
                           (f"invalid Graphify stage contract: {exc}",), {"argv": list(argv)})

    outcome = run_tool_stage(
        life.run, stage, request.root,
        tracked_changes=request.tracked_changes,
        command_runner=request.command_runner,
    )
    evidence = {
        "argv": list(argv),
        "missing_prerequisites": list(outcome.missing_prerequisites),
        "missing_outputs": list(outcome.missing_outputs),
        "unexpected_tracked_changes": list(outcome.unexpected_tracked_changes),
    }
    return StageResult(GRAPHIFY_REFRESH, 12, outcome.verdict, tuple(outcome.reasons), evidence)


def _run_graphify_verification(life: RunLifecycle, request: PostTaskRequest) -> StageResult:
    """Independent re-check: every configured output present, the tracked-empty policy held,
    and no recorded Graphify command was a forbidden installer argv."""
    integration = request.integration
    root = request.root
    reasons: list[str] = []
    verdict = PASS

    missing = tuple(out for out in integration.expected_outputs if not (root / out).exists())
    if missing:
        verdict = FAIL
        reasons.append(f"configured Graphify outputs absent: {', '.join(missing)}")

    if integration.diff_policy == "tracked-empty":
        if request.tracked_changes is None:
            return StageResult(
                GRAPHIFY_VERIFICATION, 13, BLOCKED,
                ("diff_policy 'tracked-empty' needs a tracked_changes probe",))
        offending = tuple(sorted(
            line for line in request.tracked_changes()
            if _within(line, integration.workspace)))
        if offending:
            verdict = FAIL
            reasons.append(
                f"tracked changes under {integration.workspace} after Graphify: "
                f"{', '.join(offending)}")

    stage_key = f"stage:{integration.stage}"
    for record in life.run.commands:
        if record.get("stage") == stage_key and integration.forbids(record.get("argv") or []):
            verdict = FAIL
            reasons.append("a recorded Graphify command is a forbidden installer argv")
            break

    return StageResult(GRAPHIFY_VERIFICATION, 13, verdict, tuple(reasons),
                       {"missing_outputs": list(missing)})


def _within(status_line: str, workspace: str) -> bool:
    """True when a ``git status --porcelain``-style line names a path under ``workspace``."""
    text = str(status_line).strip()
    if not text:
        return False
    path = text.split(maxsplit=1)[-1].strip().strip('"').replace("\\", "/")
    prefix = workspace.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


# --- durable state + resume ------------------------------------------------------------


def _record(life: RunLifecycle, result: StageResult) -> None:
    life.run.record_stage_outcome(
        result.stage, number=result.number, verdict=result.verdict,
        reasons=result.reasons, evidence=result.evidence)
    life.run.save()


def _recorded(run: object, stage: str) -> StageResult | None:
    entry = getattr(run, "stages", {}).get(stage)
    if not entry or "verdict" not in entry:
        return None
    return StageResult(
        stage, int(entry.get("number", STAGE_NUMBERS[stage])), str(entry["verdict"]),
        tuple(entry.get("reasons", ())), entry.get("evidence"))


def _validate_request(run: object, request: PostTaskRequest) -> None:
    if not request.selected_task_ids:
        raise PostTaskError("the post-task lifecycle needs at least one selected task",
                            "no-selected-tasks")
    for tid in request.selected_task_ids:
        if tid not in getattr(run, "tasks", {}):
            raise PostTaskError(f"selected task {tid!r} is not tracked by this run",
                                "unknown-selected-task")
    if request.wrapper not in ("sh", "ps1"):
        raise PostTaskError("wrapper must be 'sh' or 'ps1'", "unknown-wrapper")


def _verdict_token(value: object) -> str:
    token = str(value or "").strip().upper()
    if token not in (PASS, FAIL, BLOCKED):
        raise PostTaskError(
            f"documentation audit returned an unknown verdict: {value!r}", "unknown-verdict")
    return token


def _clean(mapping: Mapping[str, object]) -> dict[str, object]:
    """Keep only JSON-scalar / list values so the evidence blob round-trips through
    ``run.json``."""
    return {key: value for key, value in mapping.items()
            if isinstance(value, _JSON_SCALARS) or isinstance(value, list)}


__all__ = [
    "AuditRequest",
    "Auditor",
    "DOCUMENTATION",
    "DOCUMENTATION_AUDIT",
    "DocumentationRequest",
    "Documenter",
    "FINAL_DIFF_APPROVAL",
    "FINAL_VERIFICATION",
    "GRAPHIFY_REFRESH",
    "GRAPHIFY_VERIFICATION",
    "POST_TASK_STAGES",
    "PostTaskError",
    "PostTaskOutcome",
    "PostTaskRequest",
    "RELEASE",
    "ReleasePolicy",
    "StageResult",
    "run_post_task_lifecycle",
]
