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

import sys
from typing import Any, Iterable, Mapping, Sequence

from .state import (
    ACTOR_RUNNER,
    RESUME_ROLLBACKS,
    ResumeError,
    Run,
)

#: Portable feature-pipeline core engine version. Recorded in the run's environment so a
#: resume can be reasoned about against the engine that created it; it carries no host or
#: secret detail.
CORE_VERSION = "0.1.0"

_BLOCKED_BY_PREFIX = "blocked_by: "


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

    def transition(self, task_id: str, status: str, *, actor: str, note: str | None = None) -> None:
        """Transition a task and flush. ``transition_task`` records the history event."""
        self.run.transition_task(task_id, status, actor=actor, note=note)
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
        """Block a task, record the blocker, suppress its dependents, and flush once."""
        record = self.run.task(task_id)
        if record.status != "blocked":
            self.run.transition_task(task_id, "blocked", actor=actor, note=reason)
        previous = record.blocker
        record.blocker = reason
        self.run.record_event(
            f"blocker:{task_id}", frm=previous, to=reason, actor=actor, note="blocker set")
        self._suppress_dependents()
        self.run.save()

    # -- readiness ----------------------------------------------------------------------

    def eligible_tasks(self) -> list[str]:
        """Task ids ready to dispatch: status ``ready`` and carrying no blocker."""
        return [task.id for task in self.run.tasks.values()
                if task.status == "ready" and not task.blocker]

    def recompute_readiness(self) -> None:
        """Rederive readiness/suppression from the persisted graph and flush."""
        self._rederive_readiness()
        self.run.save()

    # -- internals --------------------------------------------------------------------

    def _check_task_set(self, expected: Mapping[str, Sequence[str]]) -> None:
        persisted = {tid: list(rec.depends_on) for tid, rec in self.run.tasks.items()}
        if set(expected) != set(persisted):
            raise ResumeError(
                "resume task set does not match the persisted run", "task-set-mismatch")
        for tid, depends_on in expected.items():
            if list(depends_on) != persisted[tid]:
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
        for dep_id in task.depends_on:
            dep = self.run.task(dep_id)
            if dep.status == "blocked":
                return dep.id
            marker = dep.blocker or ""
            if marker.startswith(_BLOCKED_BY_PREFIX):
                return marker[len(_BLOCKED_BY_PREFIX):]
        return None

    def _rederive_readiness(self) -> None:
        # A blocked_by marker set on one task can suppress a further dependent, so iterate to
        # a fixed point (bounded by the task count).
        for _ in range(len(self.run.tasks) + 1):
            if not self._readiness_pass():
                break

    def _readiness_pass(self) -> bool:
        changed = False
        for task in self.run.tasks.values():
            if task.status not in {"pending", "ready"}:
                continue
            deps = [self.run.task(dep) for dep in task.depends_on]
            blocking = self._blocking_root(task)
            if blocking is not None:
                marker = f"{_BLOCKED_BY_PREFIX}{blocking}"
                if task.blocker != marker:
                    self.run.record_event(
                        f"blocker:{task.id}", frm=task.blocker, to=marker, actor=ACTOR_RUNNER,
                        note="dependent-suppressed")
                    task.blocker = marker
                    changed = True
                if task.status == "ready":
                    self.run.record_event(
                        f"task:{task.id}", frm="ready", to="pending", actor=ACTOR_RUNNER,
                        note="readiness-rederived")
                    task.status = "pending"
                    changed = True
                continue
            if (task.blocker or "").startswith(_BLOCKED_BY_PREFIX):
                self.run.record_event(
                    f"blocker:{task.id}", frm=task.blocker, to=None, actor=ACTOR_RUNNER,
                    note="dependent-unsuppressed")
                task.blocker = None
                changed = True
            # A dependency the task itself has attested (--attest-dependency) counts as
            # satisfied without ever being dispatched here; an unattested sibling dependency
            # still needs a real 'verified' status — attestation never widens beyond its own
            # named dep_id.
            attested = {entry.get("dep_id") for entry in task.attested_dependencies}
            deps_verified = all(
                dep.status == "verified" or dep.id in attested for dep in deps)
            if task.status == "pending" and deps_verified:
                self.run.transition_task(
                    task.id, "ready", actor=ACTOR_RUNNER, note="readiness-rederived")
                changed = True
            elif task.status == "ready" and not deps_verified:
                self.run.record_event(
                    f"task:{task.id}", frm="ready", to="pending", actor=ACTOR_RUNNER,
                    note="readiness-rederived")
                task.status = "pending"
                changed = True
        return changed

    def _suppress_dependents(self) -> None:
        for _ in range(len(self.run.tasks) + 1):
            if not self._suppress_pass():
                break

    def _suppress_pass(self) -> bool:
        changed = False
        for task in self.run.tasks.values():
            if task.status not in {"pending", "ready"}:
                continue
            blocking = self._blocking_root(task)
            if blocking is None:
                continue
            marker = f"{_BLOCKED_BY_PREFIX}{blocking}"
            if task.blocker != marker:
                self.run.record_event(
                    f"blocker:{task.id}", frm=task.blocker, to=marker, actor=ACTOR_RUNNER,
                    note="dependent-suppressed")
                task.blocker = marker
                changed = True
        return changed
