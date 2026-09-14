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
import shutil
import tempfile
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.application.work_items import WorkItemError, require_active_work_item

from .adapters import (
    Adapter,
    AdapterError,
    LaunchRequest,
    LaunchResult,
    check_command_allowances,
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
    model: str | None = None
    effort: str | None = None


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
    #: Runner-owned attribution of the implementation diff to this launch window.  It is also
    #: retained for a scope/provenance block so the block names the executor-owned delta.
    attribution: AttributionResult | None = None


def _scope_or_provenance_block(attribution: AttributionResult) -> str | None:
    """Return the fail-closed mechanical verdict for one executor window.

    The manifest is the sole authority for executor scope: ambient worktree differences and
    runner lifecycle projections pre-dating the snapshot are deliberately irrelevant.  A
    window whose attribution is unavailable is unknown executor activity and cannot proceed.
    """
    # A missing repository boundary has no observed executor change to charge (and remains
    # explicit evidence for the verifier).  An unavailable close *with candidates*, however,
    # means an executor-window change could not be attributed; fail closed before verifiers.
    if attribution.state == "unavailable" and attribution.changed_files:
        return "attribution-unavailable: executor window ownership cannot be proven"
    outside = [
        str(row.get("path", "<unknown>"))
        for row in attribution.changed_files
        if row.get("classification") == "out_of_scope"
    ]
    unsafe = [path for path in outside if _unsafe_scope_amendment_path(path)]
    if unsafe:
        return "scope-safety-violation: unsafe executor-owned path: " + ", ".join(unsafe)
    return None


def _unsafe_scope_amendment_path(path: str) -> bool:
    """Keep closed safety controls separate from a reviewable scope amendment."""
    normalized = path.replace("\\", "/").casefold()
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or normalized.startswith(("/", "../", "~")):
        return True
    if ":" in parts[0]:
        return True
    return (
        ".git" in parts
        or any(part in {".env", "secrets", "credentials"} or "secret" in part for part in parts)
    )


def _record_scope_amendment(run: Run, spec: TaskSpec, attribution: AttributionResult,
                            *, reverted_paths: Sequence[str] = ()) -> None:
    """Attach reviewable expansion facts without silently changing the task contract.

    This is deliberately an observation, not an approval: only the amendment lifecycle can
    create an approved revision.  It gives both independent verifiers the complete observed
    attribution and the original estimate they must assess.
    """
    observed_changes = [
        dict(row)
        for row in attribution.changed_files
        if row.get("classification") == "out_of_scope"
    ]
    if not observed_changes:
        return
    implementation = run.task(spec.id).execution_evidence["implementation"]
    implementation["scope_amendment"] = {
        "present": True,
        "observed_paths": [str(row["path"]) for row in observed_changes],
        "observed_changes": observed_changes,
        "reverted_paths": list(reverted_paths),
        "original_allowed_scope": list(spec.allowed_scope),
        "original_out_of_scope": list(spec.out_of_scope),
        "original_acceptance_criteria": [
            {"id": getattr(criterion, "id", ""), "text": getattr(criterion, "text", str(criterion))}
            for criterion in spec.acceptance_criteria
        ],
        "rationale": (
            "executor-owned paths outside the initial estimate require independent "
            "amendment-justification review"
        ),
        "approval": "pending-independent-verification",
    }


def _revert_worktree_path(repo_root: Path, before_snapshot, path: str) -> bool:
    """Restore one repository-relative path to its pre-window content, or remove it if it
    did not exist before the window opened. Returns ``True`` on a successful revert."""
    absolute = Path(repo_root) / path
    entry = before_snapshot.files.get(path) if before_snapshot is not None else None
    if entry is not None:
        if entry.unreadable:
            return False
        if entry.data is None:
            try:
                absolute.unlink()
            except FileNotFoundError:
                pass
            return True
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(entry.data)
        return True
    # Not in the bounded before-snapshot: the path was clean and tracked, unchanged at
    # window open, so its pre-window content is exactly Git's committed blob.
    try:
        shown = GitPort(repo_root).run(["show", f"HEAD:{path}"])
    except GitSafetyError:
        return False
    if shown.returncode != 0:
        try:
            absolute.unlink()
        except FileNotFoundError:
            pass
        return True
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_text(shown.stdout, encoding="utf-8", errors="surrogateescape")
    return True


def _enforce_scope_boundary(
    repo_root: Path, before_snapshot, attribution: AttributionResult,
) -> list[str]:
    """Prevent every out-of-scope executor write from reaching the primary worktree
    (TAM-01 AC-5). Historical evidence (the manifest and diff already persisted) is never
    edited — only the live worktree bytes for an out-of-scope path are reverted, restoring
    a pre-existing dirty file byte-for-byte or removing a wholly new out-of-scope addition.
    """
    reverted: list[str] = []
    for row in attribution.changed_files:
        classification = (
            row.get("classification") if isinstance(row, dict) else row.classification
        )
        if classification != "out_of_scope":
            continue
        path = str(row["path"] if isinstance(row, dict) else row.path)
        if _revert_worktree_path(repo_root, before_snapshot, path):
            reverted.append(path)
    return reverted


def _isolated_workspace(repo_root: Path) -> Path:
    """Copy the repository into a disposable executor-only worktree.

    The runner's ``.pipeline`` control plane is intentionally absent.  Executor output is
    attributed in this copy and only an allowed delta is later promoted to ``repo_root``.
    """
    target = Path(tempfile.mkdtemp(prefix="feature-pipeline-executor-")) / "workspace"
    shutil.copytree(
        repo_root, target,
        ignore=shutil.ignore_patterns(".agents", ".pipeline", "__pycache__"),
    )
    return target


def _copy_repair_report_to_workspace(
    primary: Path, workspace: Path, repair_report_path: str | None,
) -> None:
    """Make the one runner-owned repair report named in the prompt readable to the worker.

    Isolated workspaces omit the runner's ``.pipeline`` control plane.  A repair prompt still
    names its immutable report there, so copy that single pre-existing file before the snapshot
    opens.  It is baseline context, never executor output eligible for attribution or promotion.
    """
    if repair_report_path is None:
        return
    relative = repo_relative(repair_report_path, primary)
    source = primary / relative
    if source.is_symlink() or not source.is_file():
        raise DispatchError(
            f"repair report is unavailable: {relative}", "repair-report-unavailable"
        )
    destination = workspace / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _workspace_artifacts(artifacts: LaunchArtifacts, root: Path) -> LaunchArtifacts:
    """Mirror attribution artifacts inside an isolated workspace before promotion."""
    directory = root / ".pipeline-artifacts" / artifacts.task_id / f"launch-{artifacts.generation}"
    def at(path: Path) -> Path:
        return directory / path.relative_to(artifacts.directory)
    return replace(
        artifacts, directory=directory, executor_report=at(artifacts.executor_report),
        status_envelope=at(artifacts.status_envelope), prompt_envelope=at(artifacts.prompt_envelope),
        implementation_manifest=at(artifacts.implementation_manifest),
        implementation_diff=at(artifacts.implementation_diff), launch_failure=at(artifacts.launch_failure),
        result_protocol_invalid=at(artifacts.result_protocol_invalid), recovery_patch=at(artifacts.recovery_patch),
        recovery_proof=at(artifacts.recovery_proof),
    )


def _promote_reviewable_paths(
    primary: Path,
    workspace: Path,
    attribution: AttributionResult,
    before_snapshot,
) -> list[str]:
    """Apply every non-safety executor delta so independent verification sees the work.

    Allowed scope is an initial estimate, rather than a pre-verification write boundary.
    Callers must run :func:`_scope_or_provenance_block` first; it alone rejects unsafe paths.
    """
    promoted: list[str] = []
    for row in attribution.changed_files:
        relative = Path(str(row["path"]))
        # The disposable workspace starts as a copy of the primary worktree. A path in the
        # opening snapshot was already dirty or untracked, so promotion must preserve it.
        if relative.as_posix() in before_snapshot.files:
            continue
        source, destination = workspace / relative, primary / relative
        if row.get("status") == "deleted":
            destination.unlink(missing_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        promoted.append(relative.as_posix())
    return promoted


def _attribute_isolated_window(
    *, workspace: Path, primary: Path, before_snapshot, artifacts: LaunchArtifacts,
    task_id: str, allowed_scope: Sequence[str], attempt: int, generation: int,
) -> AttributionResult:
    """Persist isolated attribution without changing the primary worktree yet."""
    isolated_artifacts = _workspace_artifacts(artifacts, workspace)
    isolated_artifacts.directory.mkdir(parents=True, exist_ok=True)
    if artifacts.executor_report.exists():
        shutil.copyfile(artifacts.executor_report, isolated_artifacts.executor_report)
    attribution = attribute_executor_window(
        before_snapshot, workspace, artifacts=isolated_artifacts, task_id=task_id,
        allowed_scope=allowed_scope, attempt=attempt, generation=generation,
        executor_report=isolated_artifacts.executor_report,
        # These are copied into the disposable workspace only after the executor returns so
        # attribution can persist its evidence there. They are runner output, never executor
        # changes eligible for scope review or promotion.
        exclude_roots=(workspace / ".pipeline-artifacts",),
    )
    unsafe = [
        str(row.get("path", "<unknown>"))
        for row in attribution.changed_files
        if row.get("classification") == "out_of_scope"
        and _unsafe_scope_amendment_path(str(row.get("path", "")))
    ]
    if unsafe:
        # Do not copy an executor-controlled diff containing a secret into runner reports.
        # The stable path-only diagnostic is sufficient to explain the refusal.
        write_json_atomic(artifacts.implementation_manifest, {
            "state": attribution.state,
            "reason": "scope-safety-violation: unsafe executor-owned path: " + ", ".join(unsafe),
            "changed_files": attribution.changed_files,
        }, repo_root=primary)
        write_text_atomic(
            artifacts.implementation_diff,
            "# attribution: unsafe-path-redacted\n"
            "# executor-controlled content was excluded from runner evidence\n",
            repo_root=primary,
        )
    else:
        shutil.copyfile(isolated_artifacts.implementation_manifest, artifacts.implementation_manifest)
        shutil.copyfile(isolated_artifacts.implementation_diff, artifacts.implementation_diff)
    return replace(
        attribution,
        manifest=repo_relative(artifacts.implementation_manifest, primary),
        diff=repo_relative(artifacts.implementation_diff, primary),
    )


def dispatch_executor(
    life: RunLifecycle, request: DispatchRequest, adapter: Adapter
) -> DispatchOutcome:
    """Launch the executor for ``request.spec`` once, settling its status against the contract."""
    run = life.run
    spec = request.spec
    task_id = spec.id

    try:
        require_active_work_item(run, task_id)
    except WorkItemError as exc:
        raise DispatchError(str(exc), exc.code) from None

    record = run.task(task_id)
    if record.status != "in_progress":
        raise DispatchError(
            f"{task_id} must be 'in_progress' to dispatch an executor, is '{record.status}'",
            "task-not-running",
        )

    # Consume the generation *before* the launch: a failed attempt still owns its number.
    generation = life.consume_launch_generation(task_id, EXECUTOR_ROLE)
    life.record_operation(task_id, "executor", "started", "executor window opened",
                          generation=generation, attempt=request.attempt)
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

    primary_root = Path(run.repo_root)
    repair_report_path = request.repair_report_path
    if repair_report_path is not None:
        try:
            available = (primary_root / repo_relative(repair_report_path, primary_root)).is_file()
        except ValueError:
            available = False
        if not available:
            repair_report_path = None
    envelope = build_executor_envelope(
        spec,
        anchors=request.anchors,
        role_grant=request.role_grant,
        execution_mode=request.execution_mode,
        report_path=repo_relative(artifacts.executor_report, run.repo_root),
        attempt=request.attempt,
        plan_path=request.plan_path,
        repair_report_path=repair_report_path,
        runner_evidence_satisfied=runner_evidence_satisfied,
    )
    write_text_atomic(artifacts.prompt_envelope, envelope, repo_root=run.repo_root)

    primary_root = Path(run.repo_root)
    # This is a security boundary, not an adapter capability.  Every executor runs in a
    # disposable copy; an adapter flag would let a new or test adapter silently write the
    # primary worktree before attribution had decided what is promotable.
    workspace = _isolated_workspace(primary_root)
    _copy_repair_report_to_workspace(primary_root, workspace, repair_report_path)
    repair_input_dirs: tuple[str, ...] = ()
    if repair_report_path is not None:
        repair_input_dirs = (str((workspace / repo_relative(repair_report_path, primary_root)).parent),)
    launch_working_root = str(workspace / request.working_root)

    launch_request = LaunchRequest(
        role=spec.executor,
        task_id=task_id,
        prompt=envelope,
        report_path=artifacts.executor_report,
        working_root=launch_working_root,
        role_grant=tuple(request.role_grant),
        allowed_scope=tuple(spec.allowed_scope),
        fresh_session=request.fresh_session,
        # Concrete CLI tool names derived from the one grant that is actually populated. A
        # separate `tools=` pass-through here is what let every real launch run `--tools ""`
        # while the role grant said read/run_checks/write (RDS-13).
        tools=grant_tool_names(request.role_grant),
        # Claude's Bash capability remains inert unless its individual commands are also
        # explicitly approved.  Derive those approvals from this task's parsed, shell-free
        # declarations only; sibling-task commands and checks rooted elsewhere never cross
        # this executor window (RLC-01 AC-1).
        allowed_tools=check_command_allowances(
            tuple((command.cwd, command.argv) for command in spec.verification_commands),
            role_grant=request.role_grant,
            working_root=request.working_root,
        ),
        timeout=request.timeout,
        envelope_path=artifacts.status_envelope,
        model=request.model,
        effort=request.effort,
        # A repair report is runner-owned evidence copied into the disposable workspace.
        # Its prompt path remains .pipeline/...; grant only its copied parent so Claude can
        # read the diagnosis without access to the primary runner control plane.
        required_input_dirs=repair_input_dirs,
    )
    # Runner-owned evidence: content snapshot of the whole worktree immediately before the
    # launch, with the runner's own run/lock/report directory excluded. Subtracting this
    # after the launch attributes exactly this generation's changes, independent of both
    # executor claims and pre-existing workspace dirt.
    before_snapshot = capture_snapshot(
        workspace, exclude_roots=(workspace / ".pipeline-artifacts",))
    git_boundary = _capture_git_mutation_boundary(workspace)

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
            life, request, artifacts, generation, result, before_snapshot, git_boundary,
            workspace=workspace)

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
        working_root=launch_working_root,
        role_grant=tuple(request.role_grant),
        resume_session_id=result.session_id,
        no_tools=True,
        read_only=True,
        timeout=request.timeout,
        model=request.model,
        effort=request.effort,
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
            # Claude opts into the RLC-01 reason-bearing envelope protocol. Generic and
            # legacy adapters retain their four-key blocked-envelope compatibility.
            require_reason=getattr(adapter, "requires_blocked_envelope_reason", False),
        )
    except ReportError as exc:
        return _retryable_failure(
            life, request, artifacts, generation, result, envelope_result,
            f"{exc.code}: {exc}",
        )

    if resolution.token == "blocked":
        # A non-empty envelope-supplied reason is preserved verbatim (RLC-01 AC-2); when the
        # executor supplied none, the generic fact stands rather than inventing detail it
        # never gave.
        reason = resolution.reason or "executor reported blocked"
        life.block(task_id, reason)
        return DispatchOutcome(
            task_id, generation, "blocked", "blocked", artifacts, result, envelope_result,
            report_text, resolution.drift, reason,
        )

    # Trusted 'implemented': attribute the executor window, record evidence, then the single
    # legal transition.
    attribution = (
        _attribute_isolated_window(
            workspace=workspace, primary=primary_root, before_snapshot=before_snapshot,
            artifacts=artifacts, task_id=task_id, allowed_scope=spec.allowed_scope,
            attempt=request.attempt, generation=generation,
        )
        if workspace != primary_root else attribute_executor_window(
            before_snapshot, run.repo_root, artifacts=artifacts, task_id=task_id,
            allowed_scope=spec.allowed_scope, attempt=request.attempt, generation=generation,
            executor_report=artifacts.executor_report, exclude_roots=(run.run_dir,),
        )
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
            git_boundary, _capture_git_mutation_boundary(workspace)),
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
    reverted_paths = [] if workspace != primary_root else _enforce_scope_boundary(
        primary_root, before_snapshot, attribution)
    _record_scope_amendment(run, spec, attribution, reverted_paths=reverted_paths)
    scope_block = _scope_or_provenance_block(attribution)
    if scope_block:
        life.block(task_id, scope_block)
        return DispatchOutcome(
            task_id, generation, "blocked", "blocked", artifacts, result, envelope_result,
            report_text, resolution.drift, scope_block, attribution,
        )
    if workspace != primary_root:
        _promote_reviewable_paths(primary_root, workspace, attribution, before_snapshot)
    if resolution.drift:
        run.record_event(
            f"executor:{task_id}", to=str(generation), note=resolution.drift)
    life.record_operation(
        task_id, "executor", "succeeded",
        f"executor launch-{generation} reported implemented", generation=generation,
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
    *, workspace: Path,
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
    primary_root = Path(run.repo_root)
    attribution = (
        _attribute_isolated_window(
            workspace=workspace, primary=primary_root, before_snapshot=before_snapshot,
            artifacts=artifacts, task_id=request.spec.id,
            allowed_scope=request.spec.allowed_scope, attempt=request.attempt, generation=generation,
        )
        if workspace != primary_root else attribute_executor_window(
            before_snapshot, run.repo_root, artifacts=artifacts, task_id=request.spec.id,
            allowed_scope=request.spec.allowed_scope, attempt=request.attempt, generation=generation,
            executor_report=artifacts.executor_report, exclude_roots=(run.run_dir,))
    )
    run.record_executor_evidence(request.spec.id, attempt=request.attempt, generation=generation,
        report_path=artifacts.executor_report, session_id=result.session_id,
        reserved_manifest=repo_relative(artifacts.implementation_manifest, run.repo_root),
        reserved_diff=repo_relative(artifacts.implementation_diff, run.repo_root),
        external_actions=_git_mutation_actions(
            git_boundary, _capture_git_mutation_boundary(workspace)))
    run.record_implementation_attribution(request.spec.id, generation=generation,
        attempt=request.attempt, attribution_state=attribution.state, manifest=attribution.manifest,
        diff=attribution.diff, changed_files=attribution.changed_files, reason=attribution.reason)
    reverted_paths = [] if workspace != primary_root else _enforce_scope_boundary(
        primary_root, before_snapshot, attribution)
    _record_scope_amendment(run, request.spec, attribution, reverted_paths=reverted_paths)
    scope_block = _scope_or_provenance_block(attribution)
    if scope_block:
        life.block(request.spec.id, scope_block)
        return DispatchOutcome(request.spec.id, generation, "blocked", "blocked", artifacts,
                               result, None, report_text, failure=scope_block,
                               attribution=attribution)
    if workspace != primary_root:
        _promote_reviewable_paths(primary_root, workspace, attribution, before_snapshot)
    life.record_operation(request.spec.id, "executor", "succeeded",
                          f"executor launch-{generation} reported implemented",
                          generation=generation)
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
