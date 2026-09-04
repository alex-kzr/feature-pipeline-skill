"""Executor dispatch coordinator: auditable, non-overwriting launches that end only at
``implemented``.

Given a normalized task and a resolved :class:`~pipeline_core.adapters.Adapter`, one call to
:func:`dispatch_executor`:

* consumes one monotonic executor launch generation *before* the launch, so every attempt —
  a failed one included — owns a unique generation directory and no number is ever reused;
* builds the Standard Subagent Prompt Envelope from the task's own metadata and the explicit
  anchors, and persists it beside the launch (``executor-prompt-<generation>.md``);
* launches the adapter, settles the prose report and the strict JSON status envelope through
  :mod:`pipeline_core.reports`, and fails closed when the two disagree;
* on a trusted ``implemented`` transitions ``running -> implemented`` (executor actor) and
  records the executor report as execution evidence; on anything else — a launch failure, a
  malformed/mismatched status, or an executor-reported ``blocked`` — blocks the task and
  preserves the launch evidence first.

Launch mechanics live in the :class:`~pipeline_core.adapters.Adapter`; report interpretation
lives in :mod:`pipeline_core.reports`; the lifecycle transition and flush live in
:class:`~pipeline_core.lifecycle.RunLifecycle`. This module composes them and exposes no path
that could set ``verified``: the only transitions it makes are ``running -> implemented`` and
``running -> blocked``.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from schemas.contracts import TaskSpec

from .adapters import (
    Adapter,
    AdapterError,
    LaunchRequest,
    LaunchResult,
    grant_tool_names,
)
from .artifacts import write_json_atomic, write_text_atomic
from .lifecycle import RunLifecycle
from .prompt_envelope import EnvelopeAnchors, build_executor_envelope
from .reports import (
    LaunchArtifacts,
    ReportError,
    build_status_envelope_prompt,
    launch_artifacts,
    parse_executor_status,
    settle_executor_status,
)
from .state import ACTOR_EXECUTOR, repo_relative
from .worktree import AttributionResult, attribute_executor_window, capture_snapshot

EXECUTOR_ROLE = "executor"

#: Recorded as the exit code when the adapter refuses to build or start a launch at all.
EXIT_ADAPTER_ERROR = "adapter-error"


class DispatchError(RuntimeError):
    """A dispatch precondition failure. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DispatchRequest:
    """Everything one executor launch needs that is not already on the run."""

    spec: TaskSpec
    role_grant: tuple[str, ...]
    anchors: EnvelopeAnchors
    execution_mode: str = "separate"
    plan_path: str | None = None
    working_root: str = "."
    timeout: float | None = None
    #: A repair redispatch is a fresh context by mandate; a healthy first pass is too, until a
    #: caller has a session worth reusing.
    fresh_session: bool = True
    attempt: int = 1
    repair_report_path: str | None = None


@dataclass(frozen=True)
class DispatchOutcome:
    """The result of one launch attempt. ``status`` is the task's state afterwards."""

    task_id: str
    generation: int
    status: str  # 'implemented' | 'blocked'
    settled_status: str | None  # 'implemented' | 'blocked'; None when the launch never settled
    artifacts: LaunchArtifacts
    launch_result: LaunchResult
    envelope_result: LaunchResult | None
    report_text: str | None
    drift: str | None = None
    failure: str | None = None
    #: Runner-owned attribution of the implementation diff to this launch window. Present
    #: only on a trusted ``implemented``; ``None`` for every blocked outcome.
    attribution: AttributionResult | None = None


def dispatch_executor(
    life: RunLifecycle, request: DispatchRequest, adapter: Adapter
) -> DispatchOutcome:
    """Launch the executor for ``request.spec`` once, settling its status against the contract."""
    run = life.run
    spec = request.spec
    task_id = spec.id

    record = run.task(task_id)
    if record.status != "running":
        raise DispatchError(
            f"{task_id} must be 'running' to dispatch an executor, is '{record.status}'",
            "task-not-running",
        )

    # Consume the generation *before* the launch: a failed attempt still owns its number.
    generation = life.consume_launch_generation(task_id, EXECUTOR_ROLE)
    artifacts = launch_artifacts(run.run_dir, task_id, generation)
    artifacts.directory.mkdir(parents=True, exist_ok=True)

    envelope = build_executor_envelope(
        spec,
        anchors=request.anchors,
        role_grant=request.role_grant,
        execution_mode=request.execution_mode,
        report_path=repo_relative(artifacts.executor_report, run.repo_root),
        plan_path=request.plan_path,
        repair_report_path=request.repair_report_path,
    )
    write_text_atomic(artifacts.prompt_envelope, envelope, repo_root=run.repo_root)

    launch_request = LaunchRequest(
        role=spec.executor,
        task_id=task_id,
        prompt=envelope,
        report_path=artifacts.executor_report,
        working_root=request.working_root,
        role_grant=tuple(request.role_grant),
        allowed_scope=tuple(spec.allowed_scope),
        fresh_session=request.fresh_session,
        # Concrete CLI tool names derived from the one grant that is actually populated. A
        # separate `tools=` pass-through here is what let every real launch run `--tools ""`
        # while the role grant said read/run_checks/write (RDS-13).
        tools=grant_tool_names(request.role_grant),
        timeout=request.timeout,
        envelope_path=artifacts.status_envelope,
    )
    # Runner-owned evidence: content snapshot of the whole worktree immediately before the
    # launch, with the runner's own run/lock/report directory excluded. Subtracting this
    # after the launch attributes exactly this generation's changes, independent of both
    # executor claims and pre-existing workspace dirt.
    before_snapshot = capture_snapshot(run.repo_root, exclude_roots=(run.run_dir,))

    try:
        result = adapter.launch(launch_request)
    except AdapterError as exc:
        return _block_for_failure(
            life, request, artifacts, generation,
            LaunchResult(exit_code=EXIT_ADAPTER_ERROR, stderr=str(exc)),
            None, f"adapter error ({exc.code}): {exc}",
        )

    report_text = _settle_text(artifacts.executor_report, result.stdout, run)

    if result.exit_code != 0:
        return _block_for_failure(
            life, request, artifacts, generation, result, None,
            f"executor launch exited with {result.exit_code}",
        )
    if not result.session_id:
        return _block_for_failure(
            life, request, artifacts, generation, result, None,
            "executor launch returned no session id; the status envelope cannot be settled",
        )

    # The strict JSON status envelope: one same-session, tool-free continuation. Codex cannot
    # resume with its read-only sandbox and resolved anchor grants, so it declares that its
    # continuation is fresh; supply only the runner-parsed status token in that case.
    observed_status = None
    if getattr(adapter, "requires_fresh_envelope_context", False):
        try:
            observed_status = parse_executor_status(report_text)
        except ReportError:
            pass
    envelope_prompt = build_status_envelope_prompt(
        role=EXECUTOR_ROLE,
        task_id=task_id,
        attempt=request.attempt,
        observed_status=observed_status,
    )
    envelope_request = LaunchRequest(
        role=spec.executor,
        task_id=task_id,
        prompt=envelope_prompt,
        report_path=artifacts.status_envelope,
        working_root=request.working_root,
        role_grant=tuple(request.role_grant),
        resume_session_id=result.session_id,
        no_tools=True,
        read_only=True,
        timeout=request.timeout,
    )
    try:
        envelope_result = adapter.launch(envelope_request)
    except AdapterError as exc:
        return _block_for_failure(
            life, request, artifacts, generation, result, None,
            f"status envelope request failed ({exc.code}): {exc}",
        )
    envelope_text = _settle_text(artifacts.status_envelope, envelope_result.stdout, run)
    if envelope_result.exit_code != 0:
        return _block_for_failure(
            life, request, artifacts, generation, result, envelope_result,
            f"status envelope request exited with {envelope_result.exit_code}",
        )

    try:
        resolution = settle_executor_status(
            prose_text=report_text,
            envelope_text=envelope_text,
            role=EXECUTOR_ROLE,
            task_id=task_id,
            attempt=request.attempt,
        )
    except ReportError as exc:
        return _block_for_failure(
            life, request, artifacts, generation, result, envelope_result,
            f"{exc.code}: {exc}",
        )

    if resolution.token == "blocked":
        life.block(task_id, "executor reported blocked")
        return DispatchOutcome(
            task_id, generation, "blocked", "blocked", artifacts, result, envelope_result,
            report_text, resolution.drift,
        )

    # Trusted 'implemented': attribute the executor window, record evidence, then the single
    # legal transition.
    attribution = attribute_executor_window(
        before_snapshot,
        run.repo_root,
        artifacts=artifacts,
        task_id=task_id,
        allowed_scope=spec.allowed_scope,
        attempt=request.attempt,
        generation=generation,
        executor_report=artifacts.executor_report,
        exclude_roots=(run.run_dir,),
    )
    run.record_executor_evidence(
        task_id,
        attempt=request.attempt,
        generation=generation,
        report_path=artifacts.executor_report,
        session_id=result.session_id,
        reserved_manifest=repo_relative(artifacts.implementation_manifest, run.repo_root),
        reserved_diff=repo_relative(artifacts.implementation_diff, run.repo_root),
    )
    run.record_implementation_attribution(
        task_id,
        generation=generation,
        attempt=request.attempt,
        attribution_state=attribution.state,
        manifest=attribution.manifest,
        diff=attribution.diff,
        changed_files=attribution.changed_files,
        reason=attribution.reason,
    )
    if resolution.drift:
        run.record_event(
            f"executor:{task_id}", to=str(generation), note=resolution.drift)
    life.transition(
        task_id, "implemented", actor=ACTOR_EXECUTOR,
        note=f"executor launch-{generation} reported implemented",
    )
    return DispatchOutcome(
        task_id, generation, "implemented", "implemented", artifacts, result, envelope_result,
        report_text, resolution.drift, attribution=attribution,
    )


def _block_for_failure(
    life: RunLifecycle,
    request: DispatchRequest,
    artifacts: LaunchArtifacts,
    generation: int,
    result: LaunchResult,
    envelope_result: LaunchResult | None,
    reason: str,
) -> DispatchOutcome:
    """Persist exit/stdout/stderr/session evidence for a failed launch, then block the task."""
    run = life.run
    task_id = request.spec.id
    write_json_atomic(
        artifacts.launch_failure,
        {
            "task_id": task_id,
            "generation": generation,
            "attempt": request.attempt,
            "stage": EXECUTOR_ROLE,
            "reason": reason,
            "exit_code": result.exit_code,
            "session_id": result.session_id,
            # Raw CLI wrapper (unextracted), preserved for a human debugging a failed launch —
            # `result.stdout` itself is the extracted assistant text (RDS-07).
            "stdout": result.raw_stdout or result.stdout,
            "stderr": result.stderr,
        },
        repo_root=run.repo_root,
    )
    run.record_launch_failure(
        task_id, stage=EXECUTOR_ROLE, generation=generation,
        exit_code=result.exit_code, detail=reason,
    )
    life.block(task_id, f"executor launch-{generation} failed: {reason}")
    return DispatchOutcome(
        task_id, generation, "blocked", None, artifacts, result, envelope_result, None, None,
        reason,
    )


def _settle_text(path: Path, fallback_stdout: str, run) -> str:
    """Read the artifact the adapter wrote (or fall back to its stdout), redact it, return it.

    Both a report file the adapter/CLI wrote and its captured stdout can carry the absolute
    paths the agent saw while working, so whichever we use is rewritten through the redaction
    boundary and read back, keeping the committed artifact and the text the runner reasons
    about the same bytes.
    """
    target = Path(path)
    if target.exists():
        text = target.read_text(encoding="utf-8")
    else:
        text = fallback_stdout or ""
    written = write_text_atomic(target, text, repo_root=run.repo_root)
    return Path(written).read_text(encoding="utf-8")
