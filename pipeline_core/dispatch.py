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

import json
from dataclasses import dataclass
from pathlib import Path

from feature_pipeline.contracts import TaskSpec

from .adapters import (
    Adapter,
    AdapterError,
    LaunchRequest,
    LaunchResult,
    parse_codex_final_result,
    grant_tool_names,
)
from .artifacts import write_json_atomic, write_text_atomic
from .lifecycle import RunLifecycle
from .prompt_envelope import EnvelopeAnchors, build_executor_envelope
from .reports import (
    LaunchArtifacts,
    ReportError,
    build_status_envelope_prompt,
    capture_recovery_evidence,
    launch_artifacts,
    parse_executor_status,
    settle_executor_status,
)
from .state import ACTOR_EXECUTOR, repo_relative
from .worktree import AttributionResult, attribute_executor_window, capture_snapshot
from .git_port import GitPort, GitSafetyError

EXECUTOR_ROLE = "executor"

#: Recorded as the exit code when the adapter refuses to build or start a launch at all.
EXIT_ADAPTER_ERROR = "adapter-error"


@dataclass(frozen=True)
class _GitMutationBoundary:
    """Read-only Git state at one edge of an executor window.

    Ref names and object IDs are sufficient to identify commits and tags made during the
    window without treating the repository's earlier history as executor activity.
    """

    head: str | None
    refs: dict[str, str]


def _capture_git_mutation_boundary(repo_root: str | Path) -> _GitMutationBoundary | None:
    """Capture the local HEAD and refs using the portable read-only Git port."""
    port = GitPort(repo_root)
    try:
        head_result = port.run(("rev-parse", "--verify", "HEAD"))
        refs_result = port.run(("for-each-ref", "--format=%(refname)%09%(objectname)"))
    except (GitSafetyError, OSError):
        return None
    if refs_result.returncode != 0:
        return None
    refs: dict[str, str] = {}
    for line in refs_result.stdout.splitlines():
        ref, separator, object_id = line.partition("\t")
        if separator and ref and object_id:
            refs[ref] = object_id
    return _GitMutationBoundary(
        head=head_result.stdout.strip() if head_result.returncode == 0 else None,
        refs=refs,
    )


def _git_mutation_actions(
    before: _GitMutationBoundary | None, after: _GitMutationBoundary | None,
) -> list[dict[str, str]]:
    """Describe only ref changes between two runner-captured window boundaries."""
    if before is None or after is None:
        return []
    actions: list[dict[str, str]] = []
    if before.head != after.head:
        actions.append({
            "action": "commit",
            "before": before.head or "<unborn>",
            "after": after.head or "<unborn>",
        })
    for ref in sorted(set(before.refs) | set(after.refs)):
        previous, current = before.refs.get(ref), after.refs.get(ref)
        if previous == current:
            continue
        if ref.startswith("refs/tags/"):
            action = "tag"
        elif ref.startswith("refs/remotes/"):
            action = "remote-ref-mutation"
        else:
            action = "git-ref-mutation"
        actions.append({
            "action": action,
            "ref": ref,
            "before": previous or "<absent>",
            "after": current or "<deleted>",
        })
    return actions


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
    status: str  # 'implemented' | 'blocked' | 'retryable'
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
    runner_evidence_satisfied = False
    if spec.runner_evidence == "reverse-diff-and-restore":
        try:
            capture_recovery_evidence(
                artifacts, repo_root=run.repo_root, allowed_scope=spec.allowed_scope
            )
        except ReportError as exc:
            raise DispatchError(str(exc), exc.code) from None
        runner_evidence_satisfied = True

    envelope = build_executor_envelope(
        spec,
        anchors=request.anchors,
        role_grant=request.role_grant,
        execution_mode=request.execution_mode,
        report_path=repo_relative(artifacts.executor_report, run.repo_root),
        attempt=request.attempt,
        plan_path=request.plan_path,
        repair_report_path=request.repair_report_path,
        runner_evidence_satisfied=runner_evidence_satisfied,
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
    git_boundary = _capture_git_mutation_boundary(run.repo_root)

    try:
        result = adapter.launch(launch_request)
    except AdapterError as exc:
        return _retryable_failure(
            life, request, artifacts, generation,
            LaunchResult(exit_code=EXIT_ADAPTER_ERROR, stderr=str(exc)),
            None, f"adapter error ({exc.code}): {exc}",
        )

    report_text = _settle_text(
        artifacts.executor_report,
        result.raw_stdout if getattr(adapter, "name", None) == "codex" else result.stdout,
        run,
    )

    if result.exit_code != 0:
        return _retryable_failure(
            life, request, artifacts, generation, result, None,
            f"executor launch exited with {result.exit_code}",
        )
    if not result.session_id:
        return _retryable_failure(
            life, request, artifacts, generation, result, None,
            "executor launch returned no session id; the status envelope cannot be settled",
        )

    if getattr(adapter, "name", None) == "codex":
        return _settle_codex_final_result(
            life, request, artifacts, generation, result, before_snapshot, git_boundary)

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
        return _retryable_failure(
            life, request, artifacts, generation, result, None,
            f"status envelope request failed ({exc.code}): {exc}",
        )
    envelope_text = _settle_text(artifacts.status_envelope, envelope_result.stdout, run)
    if envelope_result.exit_code != 0:
        return _retryable_failure(
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
        return _retryable_failure(
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
        external_actions=_git_mutation_actions(
            git_boundary, _capture_git_mutation_boundary(run.repo_root)),
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


def _retryable_failure(
    life: RunLifecycle, request: DispatchRequest, artifacts: LaunchArtifacts, generation: int,
    result: LaunchResult, envelope_result: LaunchResult | None, reason: str,
    *, protocol: bool = False, envelope_stdout: str | None = None,
) -> DispatchOutcome:
    """Persist a launch/protocol diagnostic without manufacturing a task outcome."""
    run = life.run
    path = artifacts.result_protocol_invalid if protocol else artifacts.launch_failure
    write_json_atomic(path, {
        "task_id": request.spec.id, "generation": generation, "attempt": request.attempt,
        "stage": EXECUTOR_ROLE, "reason": reason, "exit_code": result.exit_code,
        "session_id": result.session_id, "stdout": result.raw_stdout or result.stdout,
        "stderr": result.stderr,
        "envelope_stdout": envelope_stdout if envelope_stdout is not None else (
            (envelope_result.raw_stdout or envelope_result.stdout)
            if envelope_result else None),
    }, repo_root=run.repo_root)
    run.record_launch_failure(request.spec.id, stage=EXECUTOR_ROLE, generation=generation,
                              exit_code=result.exit_code, detail=reason)
    run.save()
    return DispatchOutcome(request.spec.id, generation, "retryable", None, artifacts, result,
                           envelope_result, None, None, reason)


def _settle_codex_final_result(
    life: RunLifecycle, request: DispatchRequest, artifacts: LaunchArtifacts, generation: int,
    result: LaunchResult, before_snapshot, git_boundary: _GitMutationBoundary | None,
) -> DispatchOutcome:
    """Accept only Codex's one canonical event; prose never supplies a status."""
    run = life.run
    try:
        final = parse_codex_final_result(result.raw_stdout or result.stdout,
                                         task_id=request.spec.id, attempt=request.attempt)
    except AdapterError as exc:
        return _retryable_failure(life, request, artifacts, generation, result, None,
                                  f"{exc.code}: {exc}", protocol=True)
    # A Codex adapter may have generated an envelope while launching.  It is not executor
    # evidence (the executor artifact is always runner-captured output), but a conflicting
    # generated envelope must still fail closed before the canonical payload overwrites it.
    existing = (
        artifacts.status_envelope.read_text(encoding="utf-8")
        if artifacts.status_envelope.exists() else ""
    )
    if existing and not _same_codex_final_payload(existing, final.raw_payload):
        return _retryable_failure(life, request, artifacts, generation, result, None,
                                  "result-protocol-invalid: generated envelope contradicts canonical final result",
                                  protocol=True, envelope_stdout=existing)
    write_text_atomic(artifacts.status_envelope, final.raw_payload, repo_root=run.repo_root)
    report_text = _settle_text(artifacts.executor_report, result.raw_stdout or result.stdout, run)
    if final.status == "blocked":
        life.block(request.spec.id, final.reason or "")
        return DispatchOutcome(request.spec.id, generation, "blocked", "blocked", artifacts,
                               result, None, report_text)
    attribution = attribute_executor_window(
        before_snapshot, run.repo_root, artifacts=artifacts, task_id=request.spec.id,
        allowed_scope=request.spec.allowed_scope, attempt=request.attempt, generation=generation,
        executor_report=artifacts.executor_report, exclude_roots=(run.run_dir,))
    run.record_executor_evidence(request.spec.id, attempt=request.attempt, generation=generation,
        report_path=artifacts.executor_report, session_id=result.session_id,
        reserved_manifest=repo_relative(artifacts.implementation_manifest, run.repo_root),
        reserved_diff=repo_relative(artifacts.implementation_diff, run.repo_root),
        external_actions=_git_mutation_actions(
            git_boundary, _capture_git_mutation_boundary(run.repo_root)))
    run.record_implementation_attribution(request.spec.id, generation=generation,
        attempt=request.attempt, attribution_state=attribution.state, manifest=attribution.manifest,
        diff=attribution.diff, changed_files=attribution.changed_files, reason=attribution.reason)
    life.transition(request.spec.id, "implemented", actor=ACTOR_EXECUTOR,
                    note=f"executor launch-{generation} reported implemented")
    return DispatchOutcome(request.spec.id, generation, "implemented", "implemented", artifacts,
                           result, None, report_text, attribution=attribution)


def _same_codex_final_payload(generated: str, canonical: str) -> bool:
    """Compare persisted and streamed envelopes as JSON values, never as presentation bytes."""
    try:
        return json.loads(generated) == json.loads(canonical)
    except (TypeError, json.JSONDecodeError):
        return False


def _settle_text(path: Path, fallback_stdout: str, run) -> str:
    """Persist runner-captured adapter output as an artifact, redact it, and return it.

    The executor must never own the evidence channel.  In particular, a pre-existing artifact
    at ``path`` could have been written by an adapter or executor, so it is deliberately
    overwritten with captured output rather than read.  The runner then reasons about the
    same redacted bytes it persisted.
    """
    target = Path(path)
    text = fallback_stdout or ""
    written = write_text_atomic(target, text, repo_root=run.repo_root)
    return Path(written).read_text(encoding="utf-8")
