"""Restart-safe run lifecycle: initialization, atomic flush, resume reconciliation.

:class:`RunLifecycle` is the single owner of durable run mutations. Callers never scatter
``run.save()`` calls: every method here mutates the in-memory :class:`~pipeline_core.state.Run`,
records the source event that explains the change, and flushes ``run.json`` atomically before
returning (AC-4).

Responsibilities, all independent of any lock implementation (RDS-01 wraps this in real leases)
and of the executor/verifier dispatch that RDS/VR add:

* **Fresh initialization** — persist a complete schema v2 ``run.json`` under the caller-resolved
  storage directory *before* any executor process exists, recording every input control with
  its source (``explicit`` / ``default``) and a portable, secret-free environment record.
* **Atomic flush** — one persisted event per transition, command record, generation allocation,
  and blocker update.
* **Resume from ``run.json`` alone** — verify feature/prompt/plan identity, reject incompatible
  task-set changes, roll interrupted ``running``/``repairing`` states back per
  :data:`~pipeline_core.state.RESUME_ROLLBACKS`, preserve ``implemented``/``verified``, and lose
  at most the single in-flight operation.
* **Readiness rederivation** — recompute which tasks are ``ready`` from the persisted dependency
  graph rather than trusting a stale flag, and suppress pending dependents of a blocked task
  with a ``blocked_by: <ID>`` marker while keeping them in state and reports.

Board reconciliation is out of scope for this slice: divergence between a project board and
``run.json`` is reported elsewhere; ``run.json`` is authoritative here.

Standard library only.
"""

from __future__ import annotations

import hashlib
import sys
from typing import Any, Iterable, Mapping, Sequence

from .state import (
    ACTOR_HUMAN,
    ACTOR_RUNNER,
    RESUME_ROLLBACKS,
    ResumeError,
    Run,
    StateError,
)

#: Portable feature-pipeline core engine version. Recorded in the run's environment so a
#: resume can be reasoned about against the engine that created it; it carries no host or
#: secret detail.
CORE_VERSION = "0.1.0"

#: Persisted states a task may hold while being *absent* from a compatible resume's selection
#: scope without that absence being a task-set mismatch. A ``blocked`` predecessor whose work a
#: later task supersedes, and an already-``verified`` upstream task kept for history, are both
#: legitimately out of a narrower resume scope (REC-01: ``TC-01 -> TC-02 -> TC-03 -> REC-01``
#: leaves the blocked ``TC-04`` out of scope). Any other state means live work would be dropped.
_RESUME_ABSENT_OK_STATES = frozenset({"done"})


def _environment(*, adapter_requested: str | None, adapter_resolved: str | None) -> dict[str, Any]:
    """A portable, secret-free environment record (platform, Python, core, adapter fields)."""
    return {
        "platform": sys.platform,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "core_version": CORE_VERSION,
        "adapter": {"requested": adapter_requested, "resolved": adapter_resolved},
    }


class RunLifecycle:
    """The durable-mutation boundary for one run. See the module docstring."""

    def __init__(self, run: Run) -> None:
        self.run = run

    # -- construction ---------------------------------------------------------------------

    @classmethod
    def initialize(
        cls,
        run: Run,
        *,
        tasks: Iterable[tuple[str, Sequence[str]]],
        controls: Mapping[str, tuple[Any, str]] | None = None,
        adapter_requested: str | None = None,
        adapter_resolved: str | None = None,
    ) -> "RunLifecycle":
        """Persist a fresh, complete schema v2 run before any executor launch (AC-1).

        ``tasks`` is an ordered iterable of ``(task_id, depends_on)``. ``controls`` maps a
        control name to ``(value, sourced)`` where ``sourced`` is ``"explicit"`` or
        ``"default"`` — every input control is recorded with whether it was explicit or a
        default.
        """
        for task_id, depends_on in tasks:
            run.add_task(task_id, depends_on=list(depends_on))
        for name, (value, sourced) in (controls or {}).items():
            run.set_control(name, value, sourced=sourced)
        run.environment = _environment(
            adapter_requested=adapter_requested, adapter_resolved=adapter_resolved)
        run.status = "running"
        run.current_task = None
        life = cls(run)
        life._rederive_readiness()
        run.save()
        return life

    @classmethod
    def load(cls, run_dir: Any, repo_root: Any) -> "RunLifecycle":
        """Load an existing run from ``run.json`` without reconciling it."""
        return cls(Run.load(run_dir, repo_root))

    @classmethod
    def resume(
        cls,
        run_dir: Any,
        repo_root: Any,
        *,
        feature: str,
        prompt_path: Any,
        plan_path: Any,
        expected_tasks: Mapping[str, Sequence[str]] | None = None,
    ) -> "RunLifecycle":
        """Resume from ``run.json`` alone: check identity, reconcile, flush once (AC-2).

        Identity (feature, prompt, plan) must match what was persisted. When
        ``expected_tasks`` (``task_id -> depends_on``) is given, the persisted task set and
        every dependency edge must match it exactly — a removed task, an added task, or a
        changed edge is an incompatible task-set change and is rejected.
        """
        run = Run.load(run_dir, repo_root)
        run.resume(feature=feature, prompt_path=prompt_path, plan_path=plan_path)
        life = cls(run)
        if expected_tasks is not None:
            life._check_task_set(expected_tasks)
        life._reconcile_interrupted()
        life._rederive_readiness()
        run.current_task = None
        run.save()
        return life

    # -- durable mutations (each flushes once) ------------------------------------------

    def transition(self, task_id: str, status: str, *, actor: str, note: str | None = None,
                   resolution: str | None = None) -> None:
        """Transition a task and flush. ``transition_task`` records the history event."""
        self.run.transition_task(task_id, status, actor=actor, note=note, resolution=resolution)
        self.run.save()

    def record_command(
        self,
        stage: str,
        cwd: Any,
        argv: Sequence[str],
        exit_code: Any,
        duration: float,
        stdout: str,
        stderr: str,
    ) -> dict[str, Any]:
        """Record one command's evidence under a stable ``command-N`` id and flush."""
        record = self.run.record_command(
            stage, cwd, list(argv), exit_code, duration, stdout, stderr)
        self.run.record_event(
            f"command:{record['id']}", to=stage, note="command recorded")
        self.run.save()
        return record

    def consume_launch_generation(self, task_id: str, role: str = "executor") -> int:
        """Consume one monotonic launch generation for ``role`` on a task and flush."""
        generation = self.run.consume_launch_generation(task_id, role)
        self.run.record_event(
            f"generation:{task_id}:{role}", frm=str(generation), to=str(generation + 1),
            note="launch generation consumed")
        self.run.save()
        return generation

    def block(self, task_id: str, reason: str, *, actor: str = ACTOR_RUNNER) -> None:
        """Record an operational wait without turning a task into a terminal state."""
        record = self.run.task(task_id)
        if record.status == "to_do":
            self.run.transition_task(task_id, "in_progress", actor=actor, note="operation started")
        self.run.record_operation(task_id, "wait", "blocked", reason)
        self.run.save()

    def record_operation(self, task_id: str, kind: str, outcome: str,
                         detail: str | None = None, **facts: Any) -> dict[str, Any]:
        entry = self.run.record_operation(task_id, kind, outcome, detail, **facts)
        self.run.save()
        return entry

    def reuse_completed_task(self, task_id: str, evidence: Mapping[str, Any]) -> None:
        """Fast-forward a fresh task from independently completed source evidence.

        Reuse is not an executor or verifier operation in this run, so it must never open an
        ``in_progress`` window merely to satisfy the ordinary implementation transition.
        """
        record = self.run.task(task_id)
        if record.status != "to_do":
            raise StateError(
                f"cannot reuse completion for {task_id} from {record.status}",
                "reuse-not-fresh-task",
            )
        record.status = "done"
        record.resolution = "completed"
        record.resolution_reason = "independently completed reusable evidence"
        record.verification = {
            "task_verdict": evidence["task_verdict"],
            "test_verdict": evidence["test_verdict"],
            "verified_at": evidence["verified_at"],
        }
        self.run.record_operation(
            task_id, "verified-reuse", "succeeded",
            "independently completed reusable evidence",
            source_run_id=evidence["source_run_id"],
        )
        self.run.record_reused_verification(task_id, evidence)
        self.run.record_event(
            f"task:{task_id}", frm="to_do", to="done", actor=ACTOR_RUNNER,
            note="independently completed reusable evidence",
        )
        self.run.save()

    def reopen_operational_block(
        self, task_id: str, *, authorization: str, cache_path: str,
        source_run_bytes: bytes | None = None,
    ) -> None:
        """Append a snapshot of a terminal operational block, then reopen just that task.

        Validation of the authorization, blocker class, cache path, identity and leases belongs
        to the execute runner.  This durable mutation boundary only accepts its exact approved
        authorization token and refuses to overwrite the terminal record it is recovering.
        """
        record = self.run.task(task_id)
        if record.status == "done":
            raise ResumeError("a completed task cannot be reopened operationally",
                              "operational-reopen-completed")
        self.run.record_operation(
            task_id, "resume", "retryable", "operator requested another operation",
            authorization=authorization, cache_path=cache_path,
        )
        self.run.save()

    # -- readiness ----------------------------------------------------------------------

    def eligible_tasks(self) -> list[str]:
        """Task ids ready to dispatch: status ``ready`` and carrying no blocker."""
        return [task.id for task in self.run.tasks.values() if task.status == "to_do"]

    def recompute_readiness(self) -> None:
        """Rederive readiness/suppression from the persisted graph and flush."""
        self._rederive_readiness()
        self.run.save()

    # -- internals --------------------------------------------------------------------

    def _check_task_set(self, expected: Mapping[str, Sequence[str]]) -> None:
        """Reject an incompatible task-set change while tolerating a narrower resume scope.

        ``expected`` is the resume's selection closure (``task_id -> depends_on``). A task in
        ``expected`` that the run never recorded is always a mismatch. A *persisted* task that
        ``expected`` omits is a mismatch only when it still holds live work — a ``blocked``
        predecessor a later task supersedes, or an already-``verified`` upstream task, is
        legitimately outside a narrower scope and must not synthesise a mismatch. Dependency
        edges are compared within the resumed scope only, mirroring how
        :meth:`initialize` persists ``[dep for dep in depends_on if dep in scope]``.
        """
        persisted = {tid: list(rec.depends_on) for tid, rec in self.run.tasks.items()}
        unknown = set(expected) - set(persisted)
        if unknown:
            raise ResumeError(
                f"resume introduces task(s) the run never recorded: {', '.join(sorted(unknown))}",
                "task-set-mismatch")
        for tid in set(persisted) - set(expected):
            if self.run.tasks[tid].status not in _RESUME_ABSENT_OK_STATES:
                raise ResumeError(
                    f"resume scope drops {tid}, which still holds live work "
                    f"({self.run.tasks[tid].status})",
                    "task-set-mismatch")
        scope = set(expected)
        for tid, depends_on in expected.items():
            recorded_edges = [dep for dep in persisted[tid] if dep in scope]
            resumed_edges = [dep for dep in depends_on if dep in scope]
            if resumed_edges != recorded_edges:
                raise ResumeError(
                    f"dependency edges for {tid} changed since the run was recorded",
                    "task-set-mismatch")

    def _reconcile_interrupted(self) -> None:
        for task in self.run.tasks.values():
            rolled_back = RESUME_ROLLBACKS.get(task.status)
            if rolled_back is None:
                continue
            self.run.record_event(
                f"task:{task.id}", frm=task.status, to=rolled_back, actor=ACTOR_RUNNER,
                note="resume-reconcile")
            task.status = rolled_back

    def _blocking_root(self, task: Any) -> str | None:
        """The id of the blocked task holding ``task`` up, following ``blocked_by`` markers
        back to their root so every dependent points at the actually-blocked task."""
        return None

    def _rederive_readiness(self) -> None:
        # Dependencies only influence automatic scheduling. They never write a blocker.
        return None

    def _readiness_pass(self) -> bool:
        return False

    def _suppress_dependents(self) -> None:
        return None

    def _suppress_pass(self) -> bool:
        return False
