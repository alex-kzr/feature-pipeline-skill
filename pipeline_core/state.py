"""Portable durable run state, transitions, resume validation, and leases."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ArtifactReadError, read_json, write_json_atomic

SCHEMA_VERSION = 2
EXIT_TIMEOUT = "timeout"
EXIT_NOT_FOUND = "not-found"
EXIT_LAUNCH_FAILED = "error"
ACTOR_RUNNER = "runner"
ACTOR_EXECUTOR = "executor"
ACTOR_HUMAN = "human"
TASK_TRANSITIONS = {
    "pending": {"ready", "blocked"}, "ready": {"running", "blocked"},
    "running": {"implemented", "blocked"},
    "implemented": {"verified", "verification_failed", "blocked"},
    "verification_failed": {"repairing", "implemented", "blocked"},
    # ``repairing -> running`` lets the runner reopen a consumed repair attempt as a fresh
    # executor window so the one dispatch path — which owns launch generations, status
    # settlement, and diff attribution — is reused unchanged for a repair redispatch.
    "repairing": {"implemented", "blocked", "running"}, "verified": set(),
    "blocked": {"ready", "implemented"},
}

#: Interrupted non-terminal states rolled back on resume. A ``running`` task lost its executor
#: window and returns to ``ready`` for redispatch; a ``repairing`` task lost its repair window
#: and returns to ``verification_failed`` for the repair loop. ``implemented`` and ``verified``
#: are preserved; every other state is already safe to resume from as-is.
RESUME_ROLLBACKS = {"running": "ready", "repairing": "verification_failed"}

#: The only verdict tokens an independent verifier may return. ``BLOCKED`` is an external
#: condition, never a defect, so it takes precedence over ``FAIL`` when the two verifiers
#: disagree and it never consumes a repair attempt.
VERDICT_TOKENS = ("PASS", "FAIL", "BLOCKED")


class StateError(Exception):
    """A state failure with a stable code."""

    def __init__(self, message: str, code: str = "state-error") -> None:
        super().__init__(message)
        self.code = code


class TransitionError(StateError):
    """Raised for a disallowed state transition."""


class ResumeError(StateError):
    """Raised when a resume request does not match persisted identity."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_relative(path: str | Path, repo_root: str | Path) -> str:
    """Return a POSIX path inside the repository or reject a path escape."""
    raw = str(path).replace("\\", "/")
    if raw.startswith("~") or ".." in raw.split("/"):
        raise StateError(f"path '{path}' escapes the repository root", "path-escape")
    root = Path(repo_root).resolve()
    candidate = Path(raw)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:/", raw):
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            raise StateError(f"path '{path}' resolves outside the repository root", "path-escape") from None
    return raw.removeprefix("./")


def write_lease(path: str | Path, run_id: str, pid: int, task_id: str | None = None) -> Path:
    """Write lease details for a single write-capable runner."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"run_id": run_id, "pid": pid, "task_id": task_id, "started_at": _now()}, indent=2) + "\n", encoding="utf-8")
    return target


def read_lease(path: str | Path) -> dict[str, Any] | None:
    """Read a lease; malformed leases fail closed."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {"unreadable": True, "pid": None}
    return data if isinstance(data, dict) else {"unreadable": True, "pid": None}


def pid_alive(pid: int | None) -> bool:
    """Probe liveness without signalling a process."""
    if pid is None:
        return True
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: Independent launch-generation counters. Each executor or verifier launch attempt consumes
#: one generation for its role; the count never resets and survives resume (AC-3).
LAUNCH_ROLES = ("executor", "task_verifier", "test_verifier")
_GENERATION_FIELD = {role: f"next_{role}_launch_generation" for role in LAUNCH_ROLES}


def _empty_verification() -> dict[str, Any]:
    """An explicit 'not yet verified' state — never an empty/implicit manifest."""
    return {"task_verdict": None, "test_verdict": None, "verified_at": None}


def _empty_execution_evidence() -> dict[str, Any]:
    """Execution evidence with unavailable attribution stated, not implied by emptiness."""
    return {
        "attempt": 0,
        "executor_report": None,
        "implementation": {
            "state": "not-attempted",
            "changed_files": [],
            "manifest": None,
            "diff": None,
        },
    }


@dataclass
class TaskRecord:
    """A portable task's durable identity, execution, and verification facts (schema v2)."""
    id: str
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    attempts: int = 0
    blocker: str | None = None
    # --- schema v2: execution + adapter facts ------------------------------------------
    type: str | None = None
    executor: str | None = None
    adapter: str | None = None
    session_id: str | None = None
    verification: dict[str, Any] = field(default_factory=_empty_verification)
    execution_evidence: dict[str, Any] = field(default_factory=_empty_execution_evidence)
    changed_files: list[str] = field(default_factory=list)
    verification_tier: str = "full"
    accepts_scoped: list[str] = field(default_factory=list)
    promotion: dict[str, Any] | None = None
    unblocks: list[str] = field(default_factory=list)
    maintenance_audit: list[dict[str, Any]] = field(default_factory=list)
    external_launch_failures: list[dict[str, Any]] = field(default_factory=list)
    attested_dependencies: list[str] = field(default_factory=list)
    next_executor_launch_generation: int = 1
    next_task_verifier_launch_generation: int = 1
    next_test_verifier_launch_generation: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskRecord":
        """Build a record from a persisted (already schema-v2) task mapping, tolerating
        absent optional keys by falling back to the field default."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def _migrate_task_v1_to_v2(task: Mapping[str, Any]) -> dict[str, Any]:
    """Carry a schema v1 task's identity and state forward, adding v2 defaults.

    Order, status (including terminal states), attempts, blockers, and dependencies are
    preserved verbatim; the new execution/verification/generation fields start empty but
    explicit (never an implicit blank manifest).
    """
    return asdict(TaskRecord.from_dict(dict(task)))


def migrate_v1_to_v2(payload: Mapping[str, object]) -> dict[str, object]:
    """Deterministic, in-memory-only v1 -> v2 run-state migration."""
    migrated = dict(payload)
    migrated["schema_version"] = 2
    migrated["tasks"] = [_migrate_task_v1_to_v2(task) for task in payload.get("tasks", [])]
    migrated.setdefault("current_task", None)
    migrated.setdefault("controls", {})
    migrated.setdefault("environment", {})
    migrated.setdefault("stages", {})
    migrated.setdefault("artifacts", {})
    migrated.setdefault("history", [])
    migrated.setdefault("commands", [])
    return migrated


def migrate_run_state(payload: Mapping[str, object]) -> dict[str, object]:
    """Normalize a persisted run-state payload to the current schema, or fail closed.

    A v1 payload is migrated; a payload already at :data:`SCHEMA_VERSION` is returned as a
    shallow copy; any other version (including an unknown future one) raises without ever
    rewriting the source file — callers only read here.
    """
    if payload.get("schema_version") == 1:
        return migrate_v1_to_v2(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise StateError("unsupported run-state schema version", "unknown-schema-version")
    return dict(payload)


@dataclass
class Run:
    """A JSON-persisted run with explicit repository anchors (schema v2)."""
    feature: str
    prompt_path: str
    plan_path: str | None
    run_dir: Path
    repo_root: Path
    run_id: str
    status: str = "pending"
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    history: list[dict[str, str | None]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    # --- schema v2: fully sourced controls, environment, and per-stage state ------------
    controls: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    current_task: str | None = None
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, feature: str, prompt_path: str | Path, plan_path: str | Path | None, run_dir: str | Path, repo_root: str | Path) -> "Run":
        root = Path(repo_root).resolve()
        return cls(feature, repo_relative(prompt_path, root), repo_relative(plan_path, root) if plan_path else None, Path(run_dir), root, f"{_now().replace(':', '-')}-{feature}")

    def set_control(self, name: str, value: Any, *, sourced: str = "explicit") -> None:
        """Record an input control and whether it was explicit or a default (gap §3.1)."""
        self.controls[name] = {"value": value, "sourced": sourced}

    def command(self, command_id: str) -> dict[str, Any]:
        """Resolve a stable ``command-N`` reference back to its recorded evidence."""
        for entry in self.commands:
            if entry["id"] == command_id:
                return entry
        raise StateError(f"unknown command '{command_id}'", "unknown-command")

    def stage_command_ids(self, stage: str) -> list[str]:
        """The ``command-N`` ids recorded under a stage, in execution order."""
        return list(self.stages.get(stage, {}).get("commands", []))

    def consume_launch_generation(self, task_id: str, role: str = "executor") -> int:
        """Return the current launch generation for ``role`` and advance it by one.

        The counter is consumed on every launch *attempt* (even a failed one) and never
        resets, so generations stay monotonic across resume (AC-3).
        """
        if role not in _GENERATION_FIELD:
            raise StateError(f"unknown launch role '{role}'", "unknown-launch-role")
        record = self.task(task_id)
        attribute = _GENERATION_FIELD[role]
        current = getattr(record, attribute)
        setattr(record, attribute, current + 1)
        return current

    def record_event(self, scope: str, *, frm: str | None = None, to: str | None = None,
                     actor: str = ACTOR_RUNNER, note: str | None = None) -> dict[str, str | None]:
        """Append one durable history event and return it.

        Every lifecycle mutation that is not itself a task transition (a command record, a
        launch-generation allocation, a blocker update, a resume reconciliation) records its
        source event here so the persisted history explains each change (AC-4).
        """
        entry = {"at": _now(), "scope": scope, "from": frm, "to": to, "actor": actor, "note": note}
        self.history.append(entry)
        return entry

    def add_task(self, task_id: str, *, depends_on: list[str] | None = None) -> TaskRecord:
        record = TaskRecord(task_id, depends_on=list(depends_on or []))
        self.tasks[task_id] = record
        return record

    def task(self, task_id: str) -> TaskRecord:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise StateError(f"unknown task '{task_id}'", "unknown-task") from None

    def ready_tasks(self) -> list[str]:
        return [task.id for task in self.tasks.values() if task.status == "pending" and all(self.task(dep).status == "verified" for dep in task.depends_on)]

    def transition_task(self, task_id: str, to: str, actor: str = ACTOR_RUNNER, note: str | None = None) -> str:
        record = self.task(task_id)
        if to not in TASK_TRANSITIONS.get(record.status, set()):
            raise TransitionError(f"cannot transition {task_id} from {record.status} to {to}", "illegal-transition")
        if to == "implemented" and actor not in {ACTOR_RUNNER, ACTOR_EXECUTOR}:
            raise TransitionError("only an executor or runner may mark implemented", "unauthorized-transition")
        if to == "verified" and actor != ACTOR_RUNNER:
            raise TransitionError("only the runner may mark verified", "unauthorized-transition")
        if record.status == "blocked" and actor != ACTOR_HUMAN:
            raise TransitionError("only a human may unblock a task", "unauthorized-transition")
        previous = record.status
        record.status = to
        self.history.append({"at": _now(), "scope": f"task:{task_id}", "from": previous, "to": to, "actor": actor, "note": note})
        return to

    def record_command(self, stage: str, cwd: str | Path, argv: list[str], exit_code: Any, duration: float, stdout: str, stderr: str) -> dict[str, Any]:
        entry = {"id": f"command-{len(self.commands) + 1}", "stage": stage, "cwd": repo_relative(cwd, self.repo_root), "argv": list(argv), "exit_code": exit_code, "duration": duration, "stdout": stdout, "stderr": stderr}
        self.commands.append(entry)
        self.stages.setdefault(stage, {}).setdefault("commands", []).append(entry["id"])
        return entry

    def record_executor_evidence(
        self,
        task_id: str,
        *,
        attempt: int,
        generation: int,
        report_path: str | Path,
        session_id: str | None = None,
        reserved_manifest: str | None = None,
        reserved_diff: str | None = None,
    ) -> dict[str, Any]:
        """Record a trusted executor report as this task's execution evidence.

        The implementation delta itself is RDS-05's to attribute, so ``manifest`` / ``diff``
        stay ``None`` here; the reserved artifact paths are carried so the diff step has a
        fixed, non-overwriting home. Never sets any verification field.
        """
        record = self.task(task_id)
        if session_id:
            record.session_id = session_id
        evidence = {
            "attempt": attempt,
            "launch_generation": generation,
            "executor_report": repo_relative(report_path, self.repo_root),
            "implementation": {
                "state": "pending-attribution",
                "changed_files": [],
                "manifest": None,
                "diff": None,
                "manifest_reserved": reserved_manifest,
                "diff_reserved": reserved_diff,
            },
        }
        record.execution_evidence = evidence
        self.record_event(
            f"execution-evidence:{task_id}", to=str(generation),
            actor=ACTOR_EXECUTOR, note="executor report recorded as execution evidence")
        return evidence

    def record_implementation_attribution(
        self,
        task_id: str,
        *,
        generation: int,
        attempt: int,
        attribution_state: str,
        manifest: str,
        diff: str,
        changed_files: list[dict[str, Any]],
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Fill in the runner-owned executor-window delta on this task's execution evidence.

        Called after :meth:`record_executor_evidence` for a trusted ``implemented`` launch:
        it replaces the ``pending-attribution`` implementation block with the persisted
        manifest/diff refs, the attributed change rows, and the distinct attribution state
        (``known`` / ``known-empty`` / ``unavailable``). Never touches any verification field.
        """
        record = self.task(task_id)
        evidence = dict(record.execution_evidence)
        implementation = dict(evidence.get("implementation", {}))
        implementation.update(
            {
                "state": attribution_state,
                "reason": reason,
                "changed_files": list(changed_files),
                "manifest": manifest,
                "diff": diff,
            }
        )
        evidence["implementation"] = implementation
        record.execution_evidence = evidence
        record.changed_files = [str(entry["path"]) for entry in changed_files]
        self.record_event(
            f"implementation-attribution:{task_id}", to=str(generation),
            actor=ACTOR_EXECUTOR,
            note=f"executor launch-{generation} diff attributed ({attribution_state})",
        )
        return evidence

    def record_launch_failure(
        self,
        task_id: str,
        *,
        stage: str,
        generation: int,
        exit_code: Any,
        detail: str,
    ) -> dict[str, Any]:
        """Append durable evidence that one launch attempt failed and consumed its generation."""
        record = self.task(task_id)
        entry = {
            "stage": stage,
            "generation": generation,
            "exit_code": exit_code,
            "detail": detail,
            "at": _now(),
        }
        record.external_launch_failures.append(entry)
        self.record_event(
            f"launch-failure:{task_id}:{stage}", frm=str(generation), to=str(generation),
            note=f"{stage} launch generation {generation} failed: {detail}")
        return entry

    def record_verdicts(self, task_id: str, task_verdict: str, test_verdict: str) -> str:
        """Record two independent verifier verdicts and derive the task's next state.

        The sole producer of ``verified``: it takes ``PASS`` from *both* the task verifier
        and the test verifier. When the two verdicts differ, precedence is fail-closed — any
        ``BLOCKED`` blocks the task (an external cause; no repair attempt is consumed) and,
        failing that, any ``FAIL`` sends it to ``verification_failed`` for the repair loop.
        Both verdicts and a UTC timestamp are persisted whatever the outcome, so a resume can
        see exactly what the verifiers said (AC-3).
        """
        for verdict in (task_verdict, test_verdict):
            if verdict not in VERDICT_TOKENS:
                raise StateError(
                    f"'{verdict}' is not a verdict token; expected one of "
                    f"{', '.join(VERDICT_TOKENS)}",
                    "unknown-verdict",
                )
        record = self.task(task_id)
        record.verification = {
            "task_verdict": task_verdict,
            "test_verdict": test_verdict,
            "verified_at": _now(),
        }
        if "BLOCKED" in (task_verdict, test_verdict):
            reason = "a verifier returned BLOCKED — external cause"
            if record.status != "blocked":
                self.transition_task(task_id, "blocked", actor=ACTOR_RUNNER, note=reason)
            record.blocker = reason
            self.record_event(
                f"verdicts:{task_id}", to="blocked",
                note=f"task_verdict={task_verdict} test_verdict={test_verdict}")
            return record.status
        target = "verified" if task_verdict == test_verdict == "PASS" else "verification_failed"
        self.transition_task(
            task_id, target, actor=ACTOR_RUNNER,
            note=f"independent verdicts task={task_verdict} test={test_verdict}")
        self.record_event(
            f"verdicts:{task_id}", to=target,
            note=f"task_verdict={task_verdict} test_verdict={test_verdict}")
        return target

    def begin_repair(self, task_id: str, *, maximum: int) -> bool:
        """Spend one repair attempt on a ``verification_failed`` task.

        When repair budget remains this increments ``attempts`` and moves the task
        ``verification_failed -> repairing`` in one step, returning ``True``. When ``attempts``
        has already reached ``maximum`` nothing is mutated and ``False`` is returned, so the
        caller can block the task at the limit. An external ``BLOCKED`` outcome is settled by
        :meth:`record_verdicts` before this is ever reached, so an attempt is only spent on a
        real verification ``FAIL``.
        """
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise StateError(
                "maximum repair attempts must be a non-negative integer", "invalid-max-attempts")
        record = self.task(task_id)
        if record.status != "verification_failed":
            raise TransitionError(
                f"cannot begin a repair for {task_id} from {record.status}", "illegal-transition")
        if record.attempts >= maximum:
            return False
        previous = record.attempts
        record.attempts = previous + 1
        self.transition_task(
            task_id, "repairing", actor=ACTOR_RUNNER,
            note=f"repair attempt {record.attempts} of {maximum}")
        self.record_event(
            f"repair-attempt:{task_id}", frm=str(previous), to=str(record.attempts),
            note="repair attempt consumed")
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "feature": self.feature,
            "prompt_path": self.prompt_path,
            "plan_path": self.plan_path,
            "run_id": self.run_id,
            "status": self.status,
            "current_task": self.current_task,
            "controls": self.controls,
            "environment": self.environment,
            "tasks": [asdict(task) for task in self.tasks.values()],
            "history": self.history,
            "commands": self.commands,
            "stages": self.stages,
            "artifacts": self.artifacts,
        }

    def save(self) -> Path:
        return write_json_atomic(self.run_dir / "run.json", self.to_dict(), repo_root=self.repo_root)

    @classmethod
    def load(cls, run_dir: str | Path, repo_root: str | Path) -> "Run":
        directory = Path(run_dir)
        try:
            raw = read_json(directory / "run.json")
        except ArtifactReadError as exc:
            raise StateError(str(exc), "unreadable-state") from None
        data = migrate_run_state(raw)
        tasks = {entry["id"]: TaskRecord.from_dict(entry) for entry in data.get("tasks", [])}
        return cls(
            data["feature"], data["prompt_path"], data.get("plan_path"), directory,
            Path(repo_root).resolve(), data["run_id"], data.get("status", "pending"),
            tasks, data.get("history", []), data.get("commands", []),
            data.get("controls", {}), data.get("environment", {}),
            data.get("current_task"), data.get("stages", {}), data.get("artifacts", {}),
        )

    def resume(self, *, feature: str, prompt_path: str | Path, plan_path: str | Path | None) -> None:
        if feature != self.feature or repo_relative(prompt_path, self.repo_root) != self.prompt_path or (repo_relative(plan_path, self.repo_root) if plan_path else None) != self.plan_path:
            raise ResumeError("resume request does not match persisted run identity", "resume-mismatch")
