"""Portable durable run state, transitions, resume validation, and leases."""

from __future__ import annotations

import json
import os
import re
import hashlib
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import ArtifactReadError, read_json, write_json_atomic
from .plan import AmendmentError, AmendmentRevision

SCHEMA_VERSION = 2
EXIT_TIMEOUT = "timeout"
EXIT_NOT_FOUND = "not-found"
EXIT_LAUNCH_FAILED = "error"
ACTOR_RUNNER = "runner"
ACTOR_EXECUTOR = "executor"
ACTOR_HUMAN = "human"
#: Product-progress transitions. ``to_do`` may reach ``done`` directly only through an
#: explicit human cancellation (an unstarted task that is no longer needed); completing work
#: still requires having passed through ``in_progress`` (enforced in ``transition_task``).
TASK_TRANSITIONS = {"to_do": {"in_progress", "done"}, "in_progress": {"done"}, "done": {"in_progress"}}

#: Interrupted non-terminal states rolled back on resume. A ``running`` task lost its executor
#: window and returns to ``ready`` for redispatch; a ``repairing`` task lost its repair window
#: and returns to ``verification_failed`` for the repair loop. ``implemented`` and ``verified``
#: are preserved; every other state is already safe to resume from as-is.
RESUME_ROLLBACKS: dict[str, str] = {}

#: The only verdict tokens an independent verifier may return. ``BLOCKED`` is an external
#: condition, never a defect, so it takes precedence over ``FAIL`` when the two verifiers
#: disagree and it never consumes a repair attempt.
VERDICT_TOKENS = ("PASS", "FAIL", "BLOCKED")

LIVE_PROBE_SCHEMA_VERSION = 1
_LIVE_PROBE_FIELDS = (
    "schema_version", "task_id", "run_id", "task_contract_digest", "attempt_id", "adapter", "cli_version",
    "role", "bundle_digest", "allowed_scope", "grants", "timeout_s", "max_attempts",
    "started_at", "ended_at", "disposition", "reason", "cleanup", "task_contract_revision",
    "proof_path", "proof_digest",
)
_LIVE_PROBE_DISPOSITIONS = frozenset({
    "NO_BREACH_OBSERVED", "UNKNOWN", "INCONCLUSIVE", "BREACH_DETECTED",
    "NESTED_DELEGATION_DETECTED", "SUBPROCESS_ACCESS_DETECTED", "SCOPE_WIDENING",
    "GRANT_WIDENING", "TIMEOUT", "LAUNCH_FAILED", "MALFORMED_OUTPUT",
    "CONTAINMENT_PROVEN",
})


@dataclass(frozen=True)
class LiveProbeEvidence:
    """Versioned runner observation; a positive result is digest-bound concrete proof only."""

    schema_version: int
    task_id: str
    run_id: str
    task_contract_digest: str
    attempt_id: str
    adapter: str
    cli_version: str
    role: str
    bundle_digest: str
    allowed_scope: tuple[str, ...]
    grants: tuple[str, ...]
    timeout_s: float
    max_attempts: int
    started_at: str
    ended_at: str
    disposition: str
    reason: str
    cleanup: str
    task_contract_revision: int = 0
    proof_path: str = ""
    proof_digest: str = ""

    def validate(self) -> None:
        if (not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool)
                or self.schema_version != LIVE_PROBE_SCHEMA_VERSION):
            raise StateError("unknown live-probe evidence schema version", "unknown-live-probe-schema")
        if self.disposition not in _LIVE_PROBE_DISPOSITIONS:
            raise StateError("invalid live-probe disposition", "invalid-live-probe-disposition")
        if self.adapter not in {"codex", "claude"}:
            raise StateError("live-probe adapter is unsupported", "invalid-live-probe-evidence")
        if (not re.fullmatch(r"[A-Za-z0-9._-]+", self.attempt_id)
                or not all(isinstance(item, str) and item for item in (
            self.task_id, self.run_id, self.task_contract_digest, self.attempt_id, self.cli_version, self.role,
        self.bundle_digest, self.started_at, self.ended_at, self.reason,
        )) or self.cli_version == "unknown" or self.cleanup not in {"removed", "failed"} or not isinstance(self.timeout_s, (int, float))
                or isinstance(self.timeout_s, bool) or not math.isfinite(self.timeout_s)
                or self.timeout_s <= 0
                or not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool)
                or self.max_attempts <= 0
                or not isinstance(self.task_contract_revision, int)
                or isinstance(self.task_contract_revision, bool)
                or self.task_contract_revision < 0
                or len(set(self.allowed_scope)) != len(self.allowed_scope)
                or any(not isinstance(item, str) or not item or "\\" in item
                       or item.startswith("/") or ".." in item.split("/")
                       for item in self.allowed_scope)
                or len(set(self.grants)) != len(self.grants)
                or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", item)
                       for item in self.grants)):
            raise StateError("malformed live-probe evidence", "invalid-live-probe-evidence")
        if self.disposition == "CONTAINMENT_PROVEN" and (
            not re.fullmatch(r"reports/[A-Za-z0-9._-]+/live-probe\.json", self.proof_path)
            or not re.fullmatch(r"[0-9a-f]{64}", self.proof_digest)
        ):
            raise StateError("positive live-probe evidence requires a bound proof artifact", "invalid-live-probe-evidence")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        row = {name: getattr(self, name) for name in _LIVE_PROBE_FIELDS}
        # The durable representation is JSON-shaped.  Keep tuple-backed fields
        # as arrays here so validation of a later persisted row reaches the
        # identity and budget checks rather than failing on an internal type.
        row["allowed_scope"] = list(self.allowed_scope)
        row["grants"] = list(self.grants)
        return row

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LiveProbeEvidence":
        legacy_fields = set(_LIVE_PROBE_FIELDS) - {"proof_path", "proof_digest"}
        if set(data) != set(_LIVE_PROBE_FIELDS) and set(data) != legacy_fields:
            raise StateError("live-probe evidence has unknown or missing fields", "invalid-live-probe-evidence")
        if not isinstance(data["allowed_scope"], list) or not isinstance(data["grants"], list):
            raise StateError("live-probe scope and grants must be arrays", "invalid-live-probe-evidence")
        row = cls(**{**dict(data), "proof_path": data.get("proof_path", ""),
                     "proof_digest": data.get("proof_digest", ""), "allowed_scope": tuple(data["allowed_scope"]),
                     "grants": tuple(data["grants"])})
        row.validate()
        return row


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
    status: str = "to_do"
    resolution: str | None = None
    resolution_reason: str | None = None
    operation_history: list[dict[str, Any]] = field(default_factory=list)
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
    #: Content-addressed writes made by the runner's lifecycle projection.  These are
    #: durable provenance, not executor evidence: a later executor window must begin after
    #: them and must never inherit them as its implementation delta.
    runner_owned_writes: list[dict[str, Any]] = field(default_factory=list)
    verification_tier: str = "full"
    accepts_scoped: list[str] = field(default_factory=list)
    promotion: dict[str, Any] | None = None
    unblocks: list[str] = field(default_factory=list)
    maintenance_audit: list[dict[str, Any]] = field(default_factory=list)
    external_launch_failures: list[dict[str, Any]] = field(default_factory=list)
    task_path: str | None = None
    task_contract_digest: str | None = None
    task_contract_version: str | None = None
    reused_verification: list[dict[str, Any]] = field(default_factory=list)
    #: Read-only evidence that a declared dependency was attested from another, already-closed
    #: run instead of being dispatched in this one (``--attest-dependency``). Each entry:
    #: ``dep_id``, ``source_feature``, ``source_run_id``, ``source_digest`` (a
    #: ``sha256:<hex>`` of the source ``run.json`` at attestation time — tamper-evident, not
    #: tamper-proof), the source's recorded ``task_verdict``/``test_verdict``/``verified_at``,
    #: and this run's ``attested_at``. Populated only by
    #: :meth:`Run.record_attestation`; nothing here is ever written back to the source run.
    attested_dependencies: list[dict[str, Any]] = field(default_factory=list)
    next_executor_launch_generation: int = 1
    next_task_verifier_launch_generation: int = 1
    next_test_verifier_launch_generation: int = 1
    #: Append-only, immutable amendment revisions (TAM-01) — each a dict shaped like
    #: :meth:`pipeline_core.plan.AmendmentRevision.as_dict`. Never rewritten or removed;
    #: a later amendment only ever appends.
    amendment_revisions: list[dict[str, Any]] = field(default_factory=list)
    #: The active contract/execution/verification epoch. ``0`` is the task's original,
    #: unamended contract; each approved amendment increments it by one.
    current_revision: int = 0
    #: Repair attempts and independent-verifier evidence recorded *before* the most recent
    #: amendment, snapshotted verbatim at amendment time so historical evidence stays
    #: readable without being able to validate the expanded revision (AC-3).
    revision_history: list[dict[str, Any]] = field(default_factory=list)

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
        payload = migrate_v1_to_v2(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise StateError("unsupported run-state schema version", "unknown-schema-version")
    migrated = dict(payload)
    legacy = {
        "pending": "to_do", "ready": "to_do", "running": "in_progress",
        "implemented": "in_progress", "verification_failed": "in_progress",
        "repairing": "in_progress", "verified": "done", "blocked": "in_progress",
    }
    tasks = []
    for item in payload.get("tasks", []):
        task = dict(item)
        old_status = task.get("status")
        if old_status in legacy:
            task["status"] = legacy[old_status]
            # A v1->v2 schema migration (above) already stamped an explicit
            # ``resolution: None`` onto every task via `TaskRecord`'s own field default, so
            # `setdefault` here is a no-op for a schema-v2-shaped legacy task: it never sees a
            # missing key to fill in. A legacy 'verified' status must still resolve to a
            # completed task, so assign it outright rather than relying on absence.
            if old_status == "verified":
                task["resolution"] = "completed"
            else:
                task.setdefault("resolution", None)
            task.setdefault("resolution_reason", None)
            task.setdefault("operation_history", []).append({"at": _now(), "kind": "legacy-state",
                                                               "outcome": "migrated", "detail": old_status})
        tasks.append(task)
    migrated["tasks"] = tasks
    return migrated


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
    #: Immutable provenance for a linked pre-implementation replacement run.
    recovery: dict[str, Any] | None = None
    #: Runner-owned, append-only observations from explicitly opted-in containment probes.
    live_probe_evidence: list[dict[str, Any]] = field(default_factory=list)
    #: Canonical copies of observations at the last successful persistence boundary.  This
    #: is deliberately not serialized: the evidence rows remain the versioned public
    #: format, while this guard prevents an in-memory caller from rewriting their prefix.
    _persisted_live_probe_evidence: tuple[str, ...] = field(
        default_factory=tuple, init=False, repr=False,
    )

    @classmethod
    def create(cls, feature: str, prompt_path: str | Path, plan_path: str | Path | None, run_dir: str | Path, repo_root: str | Path) -> "Run":
        root = Path(os.path.abspath(repo_root))
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
        return [task.id for task in self.tasks.values() if task.status == "to_do" and all(self.task(dep).status == "done" for dep in task.depends_on)]

    def transition_task(self, task_id: str, to: str, actor: str = ACTOR_RUNNER, note: str | None = None,
                        resolution: str | None = None) -> str:
        record = self.task(task_id)
        # Transitional callers from pre-v3 orchestration may still name a former execution
        # phase.  Normalize it at this boundary; durable records never retain that vocabulary.
        legacy_targets = {
            "ready": "to_do", "running": "in_progress", "implemented": "in_progress",
            "verification_failed": "in_progress", "repairing": "in_progress",
            "blocked": "in_progress", "verified": "done",
        }
        legacy_target = to in legacy_targets
        to = legacy_targets.get(to, to)
        if legacy_target and to == "done" and resolution is None:
            resolution = "completed"
        if legacy_target and to == record.status:
            self.record_event(
                f"operation:{task_id}:legacy-transition", to=to, actor=actor,
                note=note or "legacy execution phase normalized",
            )
            return to
        if to not in TASK_TRANSITIONS.get(record.status, set()):
            raise TransitionError(f"cannot transition {task_id} from {record.status} to {to}", "illegal-transition")
        if to == "done" and resolution not in {"completed", "cancelled"}:
            raise TransitionError("a done task requires a resolution", "missing-task-resolution")
        if to == "done" and resolution == "completed":
            if actor != ACTOR_RUNNER:
                raise TransitionError("only the runner may record completed work", "unauthorized-transition")
            if record.status != "in_progress":
                raise TransitionError(
                    "completed resolution requires the task to have been in progress",
                    "illegal-transition",
                )
            verification = record.verification
            if not (
                isinstance(verification, Mapping)
                and verification.get("task_verdict") == "PASS"
                and verification.get("test_verdict") == "PASS"
            ):
                raise TransitionError(
                    "completed resolution requires two passing independent verifier verdicts",
                    "missing-verification-evidence",
                )
        if to == "done" and resolution == "cancelled":
            if actor != ACTOR_HUMAN:
                raise TransitionError("only a human may cancel a task", "unauthorized-transition")
            if not (note and note.strip()):
                raise TransitionError(
                    "cancelling a task requires a non-empty reason", "missing-cancellation-reason")
        if record.status == "done" and actor != ACTOR_HUMAN:
            raise TransitionError("only a human may reopen a done task", "unauthorized-transition")
        previous = record.status
        record.status = to
        record.resolution = resolution if to == "done" else None
        record.resolution_reason = note if to == "done" else None
        self.history.append({"at": _now(), "scope": f"task:{task_id}", "from": previous, "to": to, "actor": actor, "note": note})
        return to

    def record_operation(self, task_id: str, kind: str, outcome: str, detail: str | None = None,
                         **facts: Any) -> dict[str, Any]:
        """Persist a repeatable operation outcome without changing task progress."""
        entry: dict[str, Any] = {"at": _now(), "kind": kind, "outcome": outcome, "detail": detail}
        entry.update(facts)
        self.task(task_id).operation_history.append(entry)
        self.record_event(f"operation:{task_id}:{kind}", to=outcome, note=detail)
        return entry

    def latest_unfinished_operation(self, task_id: str) -> dict[str, Any] | None:
        """Return the newest operation that did not reach a successful outcome."""
        for entry in reversed(self.task(task_id).operation_history):
            if entry.get("outcome") != "succeeded":
                return dict(entry)
        return None

    def record_command(self, stage: str, cwd: str | Path, argv: list[str], exit_code: Any, duration: float, stdout: str, stderr: str) -> dict[str, Any]:
        entry = {"id": f"command-{len(self.commands) + 1}", "stage": stage, "cwd": repo_relative(cwd, self.repo_root), "argv": list(argv), "exit_code": exit_code, "duration": duration, "stdout": stdout, "stderr": stderr}
        self.commands.append(entry)
        self.stages.setdefault(stage, {}).setdefault("commands", []).append(entry["id"])
        return entry

    def record_live_probe_evidence(self, evidence: LiveProbeEvidence) -> dict[str, Any]:
        """Append one identity-bound probe observation without granting any capability."""
        evidence.validate()
        if evidence.run_id != self.run_id or evidence.task_id not in self.tasks:
            raise StateError("live-probe evidence does not belong to this task/run", "live-probe-identity-mismatch")
        task = self.task(evidence.task_id)
        if task.adapter and task.adapter != evidence.adapter:
            raise StateError("live-probe adapter differs from the selected task adapter", "live-probe-identity-mismatch")
        if not self._probe_contract_matches(task, evidence):
            raise StateError(
                "live-probe evidence differs from the selected task contract",
                "live-probe-contract-mismatch",
            )
        if any(
            row.get("attempt_id") == evidence.attempt_id
            and row.get("task_contract_revision", 0) == evidence.task_contract_revision
            for row in self.live_probe_evidence
        ):
            raise StateError("live-probe attempt identity is already recorded", "duplicate-live-probe-attempt")
        row = evidence.as_dict()
        row["allowed_scope"] = list(evidence.allowed_scope)
        row["grants"] = list(evidence.grants)
        self._validate_live_probe_rows([*self.live_probe_evidence, row])
        self.live_probe_evidence.append(row)
        self.record_event(
            f"live-probe:{evidence.task_id}", to=evidence.disposition,
            note="runner-owned bounded live-isolation observation",
        )
        return row

    def _validate_live_probe_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Validate persisted observations against their selected task and fixed budget."""
        seen: set[tuple[str, int, str]] = set()
        task_limits: dict[tuple[str, int], int] = {}
        task_counts: dict[tuple[str, int], int] = {}
        for raw_row in rows:
            evidence = LiveProbeEvidence.from_dict(raw_row)
            if evidence.run_id != self.run_id or evidence.task_id not in self.tasks:
                raise StateError(
                    "live-probe evidence does not belong to this task/run",
                    "live-probe-identity-mismatch",
                )
            task = self.task(evidence.task_id)
            if task.adapter != evidence.adapter:
                raise StateError(
                    "live-probe adapter differs from the selected task adapter",
                    "live-probe-identity-mismatch",
                )
            if not self._probe_contract_matches(task, evidence):
                raise StateError(
                    "live-probe evidence differs from the selected task contract",
                    "live-probe-contract-mismatch",
                )
            attempt_key = (evidence.task_id, evidence.task_contract_revision, evidence.attempt_id)
            if attempt_key in seen:
                raise StateError(
                    "live-probe attempt identity is already recorded",
                    "duplicate-live-probe-attempt",
                )
            seen.add(attempt_key)
            epoch_key = (evidence.task_id, evidence.task_contract_revision)
            prior_limit = task_limits.setdefault(epoch_key, evidence.max_attempts)
            if prior_limit != evidence.max_attempts:
                raise StateError(
                    "live-probe attempt budget cannot change after the first observation",
                    "live-probe-budget-mismatch",
                )
            task_counts[epoch_key] = task_counts.get(epoch_key, 0) + 1
            if task_counts[epoch_key] > prior_limit:
                raise StateError(
                    "live-probe attempt budget is exhausted",
                    "live-probe-budget-exhausted",
                )

    @staticmethod
    def _probe_contract_matches(task: TaskRecord, evidence: LiveProbeEvidence) -> bool:
        """Match a probe observation to the immutable digest for its recorded epoch."""
        return evidence.task_contract_revision in Run._probe_contract_revisions(task).get(
            evidence.task_contract_digest, set(),
        )

    @staticmethod
    def _probe_contract_revisions(task: TaskRecord) -> dict[str, set[int]]:
        """Return every revision asserted for each immutable task-contract digest."""
        revisions: dict[str, set[int]] = {}

        def add(revision: Any, digest: Any) -> None:
            if (isinstance(revision, int) and not isinstance(revision, bool)
                    and revision >= 0 and isinstance(digest, str) and digest):
                revisions.setdefault(digest, set()).add(revision)

        add(task.current_revision, task.task_contract_digest)
        for entry in task.revision_history:
            if isinstance(entry, Mapping):
                add(entry.get("revision"), entry.get("contract_digest"))
        for entry in task.amendment_revisions:
            if isinstance(entry, Mapping):
                revision = entry.get("revision")
                add(revision, entry.get("new_digest"))
                if isinstance(revision, int) and not isinstance(revision, bool):
                    add(revision - 1, entry.get("prior_digest"))
        return revisions

    @classmethod
    def _migrate_legacy_live_probe_revisions(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Add a missing legacy probe revision only when its digest names one epoch."""
        rows = payload.get("live_probe_evidence")
        if not isinstance(rows, list):
            return dict(payload)
        tasks = {
            item.get("id"): TaskRecord.from_dict(item)
            for item in payload.get("tasks", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        migrated_rows: list[Any] = []
        changed = False
        for row in rows:
            if not isinstance(row, Mapping) or "task_contract_revision" in row:
                migrated_rows.append(row)
                continue
            task = tasks.get(row.get("task_id"))
            matches = (cls._probe_contract_revisions(task).get(row.get("task_contract_digest"), set())
                       if task is not None else set())
            if len(matches) != 1:
                raise StateError(
                    "legacy live-probe evidence does not identify one task contract revision",
                    "live-probe-contract-mismatch",
                )
            migrated_rows.append({**row, "task_contract_revision": matches.pop()})
            changed = True
        return {**payload, "live_probe_evidence": migrated_rows} if changed else dict(payload)

    @staticmethod
    def _live_probe_row_fingerprint(row: Mapping[str, Any]) -> str:
        """Return a stable JSON representation used solely for append-only checking."""
        return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _validate_live_probe_append_only(self) -> tuple[str, ...]:
        """Reject removal or replacement of any observation already persisted."""
        current = tuple(self._live_probe_row_fingerprint(row) for row in self.live_probe_evidence)
        prior = self._persisted_live_probe_evidence
        if len(current) < len(prior) or current[:len(prior)] != prior:
            raise StateError(
                "live-probe evidence history may only be extended",
                "live-probe-history-rewritten",
            )
        return current

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
        external_actions: list[dict[str, Any]] | None = None,
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
            # These are captured by the runner from the executor window, never inferred
            # from an executor report or from history that predates the window.
            "external_actions": list(external_actions or ()),
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

    def record_remote_evidence(
        self,
        task_id: str,
        *,
        attempt: int,
        observations: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Append deterministic, runner-owned read-only remote observations.

        This is the only lifecycle path for remote acceptance facts.  It accepts observations
        only for the executor attempt that produced the current evidence, rejects malformed
        rows before persisting any of them, and never infers an observation from executor prose.
        """
        record = self.task(task_id)
        evidence = record.execution_evidence
        if not evidence or evidence.get("attempt") != attempt:
            raise StateError(
                f"remote evidence for {task_id} does not match an executor attempt",
                "remote-evidence-attempt-mismatch",
            )
        rows: list[dict[str, Any]] = []
        for observation in observations:
            row = dict(observation)
            if (
                row.get("observed_by") != ACTOR_RUNNER
                or row.get("outcome") != "succeeded"
                or not isinstance(row.get("kind"), str)
                or not row["kind"]
                or not isinstance(row.get("subject"), str)
                or not row["subject"]
                or not isinstance(row.get("recorded_at"), str)
                or not row["recorded_at"]
            ):
                raise StateError(
                    "remote evidence must be a successful runner observation with kind, "
                    "subject, and recorded_at",
                    "invalid-remote-evidence",
                )
            rows.append(row)
        if not rows:
            raise StateError("remote evidence observations cannot be empty", "invalid-remote-evidence")
        existing = list(evidence.get("remote_evidence") or ())
        evidence["remote_evidence"] = [*existing, *rows]
        self.record_event(
            f"remote-evidence:{task_id}", to=str(attempt), actor=ACTOR_RUNNER,
            note="runner recorded remote observations: " + ", ".join(
                str(row["kind"]) for row in rows),
        )
        return rows

    def record_runner_projection(
        self, task_id: str, paths: Sequence[str | Path], *, operation: str = "lifecycle-projection",
    ) -> list[dict[str, Any]]:
        """Durably attribute already-written lifecycle projection files to the runner.

        The digest binds the attribution to the exact content the runner wrote.  Refuse an
        absent or unreadable path rather than leaving a projection difference with ambiguous
        ownership for the next executor window.
        """
        record = self.task(task_id)
        entries: list[dict[str, Any]] = []
        for path in paths:
            relative = repo_relative(path, self.repo_root)
            target = self.repo_root / relative
            try:
                content = target.read_bytes()
            except OSError as exc:
                raise StateError(
                    f"runner projection cannot attribute '{relative}': {exc}",
                    "runner-attribution-unavailable",
                ) from exc
            entries.append({
                "path": relative,
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "operation": operation,
            })
        record.runner_owned_writes.extend(entries)
        self.record_event(
            f"runner-projection:{task_id}", to=operation, actor=ACTOR_RUNNER,
            note="runner-owned writes: " + ", ".join(entry["path"] for entry in entries),
        )
        return entries

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
        """Append durable evidence and bind its executor diagnostic to this run."""
        record = self.task(task_id)
        entry = {
            "stage": stage,
            "generation": generation,
            "exit_code": exit_code,
            "detail": detail,
            "source_run_id": self.run_id,
            "at": _now(),
        }
        if stage == ACTOR_EXECUTOR:
            diagnostic = (
                self.run_dir / "reports" / task_id / f"launch-{generation}"
                / f"launch-failure-{generation}.json"
            )
            try:
                payload = read_json(diagnostic)
                if not isinstance(payload, dict):
                    raise StateError("executor launch diagnostic is malformed", "launch-failure-diagnostic")
                payload["source_run_id"] = self.run_id
                write_json_atomic(diagnostic, payload, repo_root=self.repo_root)
            except ArtifactReadError:
                # Recording the failed launch remains durable even if its diagnostic was not
                # persisted. Recovery rejects that incomplete source later.
                pass
        record.external_launch_failures.append(entry)
        self.record_operation(
            task_id, stage, "failed", detail,
            generation=generation, exit_code=exit_code,
        )
        self.record_event(
            f"launch-failure:{task_id}:{stage}", frm=str(generation), to=str(generation),
            note=f"{stage} launch generation {generation} failed: {detail}")
        return entry

    def record_verdicts(self, task_id: str, task_verdict: str, test_verdict: str) -> str:
        """Record verdict evidence without making an operational failure terminal.

        Only two PASS verdicts complete a task.  FAIL and BLOCKED remain durable operation
        facts on its in-progress task, which permits a later executor or verifier window.
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
        outcome = "succeeded" if task_verdict == test_verdict == "PASS" else (
            "blocked" if "BLOCKED" in (task_verdict, test_verdict) else "failed")
        self.record_operation(task_id, "verification", outcome,
                              f"task_verdict={task_verdict} test={test_verdict}")
        if outcome == "succeeded" and record.status != "done":
            self.transition_task(task_id, "done", actor=ACTOR_RUNNER,
                                 note="independent completion policy passed", resolution="completed")
        return record.status

    def record_attestation(
        self, task_id: str, dep_id: str, evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Record ``dep_id`` as attested on ``task_id`` from ``evidence`` (see
        :attr:`TaskRecord.attested_dependencies`). Read-only with respect to the source run —
        only its ``run.json`` was ever read to build ``evidence``; nothing is written to it,
        reported against it, or has its status changed. Replaces a same-``dep_id`` entry
        rather than duplicating it.
        """
        record = self.task(task_id)
        entry = dict(evidence)
        record.attested_dependencies = [
            existing for existing in record.attested_dependencies
            if existing.get("dep_id") != dep_id
        ] + [entry]
        self.record_event(
            f"attestation:{task_id}:{dep_id}", to="attested",
            note=f"{dep_id} attested via {entry.get('source_feature')}")
        return entry

    def set_task_contract(
        self, task_id: str, task_path: str, digest: str, *, version: str = "rec09-v1"
    ) -> None:
        """Persist the canonical identity that makes a future verified task reusable."""
        record = self.task(task_id)
        record.task_path = repo_relative(task_path, self.repo_root)
        record.task_contract_digest = digest
        record.task_contract_version = version

    def record_reused_verification(
        self, task_id: str, evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Persist reuse evidence on the consuming task only."""
        record = self.task(task_id)
        entry = dict(evidence)
        dependency_id = entry.get("dependency_id")
        record.reused_verification = [
            existing for existing in record.reused_verification
            if existing.get("dependency_id") != dependency_id
        ] + [entry]
        self.record_event(
            f"reused-verification:{task_id}:{dependency_id}", to="reused",
            note=f"{dependency_id} reused from {entry.get('source_run_id')}")
        return entry

    def begin_repair(self, task_id: str, *, maximum: int) -> bool:
        """Spend an advisory repair attempt, recording escalation at its threshold."""
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise StateError(
                "maximum repair attempts must be a non-negative integer", "invalid-max-attempts")
        record = self.task(task_id)
        if record.attempts >= maximum:
            self.record_operation(task_id, "repair", "escalated", "repair threshold exhausted",
                                  attempts=record.attempts, maximum=maximum)
            return False
        previous = record.attempts
        record.attempts = previous + 1
        self.record_operation(task_id, "repair", "retryable",
                              f"repair attempt {record.attempts} of {maximum}")
        self.record_event(
            f"repair-attempt:{task_id}", frm=str(previous), to=str(record.attempts),
            note="repair attempt consumed")
        return True

    def apply_amendment(self, revision: AmendmentRevision, *, new_digest: str,
                        new_digest_version: str) -> dict[str, Any]:
        """Persist one approved, already-validated :class:`AmendmentRevision`.

        Fails closed a second time on the durable record itself — a task that reached
        ``done`` between validation and persistence, a task-id mismatch, or a revision
        number that does not immediately follow the task's current revision are all
        rejected rather than silently reordered or duplicated (AC-1, AC-2). On success:
        appends the revision (append-only; never rewritten), snapshots the pre-amendment
        repair count and verifier evidence into ``revision_history`` so it stays readable
        without being able to validate the new revision, resets the repair budget and
        verification manifest for a fresh epoch, and updates the recorded contract digest
        so ordinary strict-resume compares against the amended contract from now on
        (AC-3, AC-6).
        """
        record = self.task(revision.task_id)
        if record.status == "done":
            raise AmendmentError(
                f"'{revision.task_id}' is already done; a completed task cannot be amended",
                "task-already-done",
            )
        if revision.revision != record.current_revision + 1:
            raise AmendmentError(
                f"amendment revision {revision.revision} does not follow the current "
                f"revision {record.current_revision} for '{revision.task_id}'",
                "revision-out-of-order",
            )
        record.revision_history.append({
            "revision": record.current_revision,
            "attempts": record.attempts,
            "verification": dict(record.verification),
            "contract_digest": record.task_contract_digest,
        })
        record.amendment_revisions.append(revision.as_dict())
        record.current_revision = revision.revision
        record.attempts = 0
        record.verification = _empty_verification()
        record.task_contract_digest = new_digest
        record.task_contract_version = new_digest_version
        self.record_operation(
            revision.task_id, "amendment", "approved",
            f"revision {revision.revision} approved by {revision.approved_by}: {revision.rationale}",
        )
        self.record_event(
            f"amendment:{revision.task_id}", frm=str(revision.revision - 1),
            to=str(revision.revision), actor=ACTOR_HUMAN,
            note=f"approved by {revision.approved_by}",
        )
        return revision.as_dict()

    def record_stage_outcome(
        self,
        stage: str,
        *,
        number: int,
        verdict: str,
        reasons: Sequence[str] = (),
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one post-task lifecycle stage's verdict under ``stages[<stage>]`` (UPR-03).

        Post-task stages (documentation, its independent audit, Graphify refresh and
        verification) are run-level, not task-level: each carries a numbered ``verdict`` and
        the reasons behind it. Every write also records a history event, so a resume can see
        how far the lifecycle got and re-enter at the first stage without a recorded ``PASS``.
        """
        entry = self.stages.setdefault(stage, {})
        previous = entry.get("verdict")
        entry["number"] = int(number)
        entry["verdict"] = str(verdict)
        entry["reasons"] = [str(reason) for reason in reasons]
        if evidence is not None:
            entry["evidence"] = dict(evidence)
        self.record_event(
            f"stage:{stage}", frm=previous, to=str(verdict),
            note=f"post-task stage {number} -> {verdict}")
        return entry

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
            "recovery": self.recovery,
            **({"live_probe_evidence": self.live_probe_evidence}
               if self.live_probe_evidence else {}),
        }

    def save(self) -> Path:
        # Validate every persisted row before any write, including rows injected through a
        # legacy caller rather than the append-only method above.
        self._validate_live_probe_rows(self.live_probe_evidence)
        evidence_snapshot = self._validate_live_probe_append_only()
        path = write_json_atomic(self.run_dir / "run.json", self.to_dict(), repo_root=self.repo_root)
        self._persisted_live_probe_evidence = evidence_snapshot
        return path

    @classmethod
    def load(cls, run_dir: str | Path, repo_root: str | Path) -> "Run":
        directory = Path(run_dir)
        state_path = directory / "run.json"
        try:
            raw = read_json(state_path)
        except ArtifactReadError as exc:
            raise StateError(str(exc), "unreadable-state") from None
        data = migrate_run_state(raw)
        data = cls._migrate_legacy_live_probe_revisions(data)
        # Compatibility reads must not turn into in-place migrations.  When normalising a
        # legacy source would change its bytes, continue from a sibling checkpoint instead
        # and retain a content-addressed pointer to the immutable source artifact.
        active_directory = directory
        recovery: dict[str, Any] | None = data.get("recovery")
        if data != raw:
            try:
                source_bytes = state_path.read_bytes()
            except OSError as exc:
                raise StateError(str(exc), "unreadable-state") from None
            source_digest = hashlib.sha256(source_bytes).hexdigest()
            active_directory = directory.parent / f"{directory.name}.continuation-{source_digest[:12]}"
            source_recovery = {
                "source_run_id": raw.get("run_id"),
                "source_run_sha256": f"sha256:{source_digest}",
            }
            checkpoint_path = active_directory / "run.json"
            if checkpoint_path.is_file():
                # The source path remains the durable identity for a legacy run.  Once a
                # continuation exists, loading by that identity must resume its checkpoint
                # rather than remigrating the immutable source and losing later history.
                try:
                    checkpoint = read_json(checkpoint_path)
                except ArtifactReadError as exc:
                    raise StateError(str(exc), "unreadable-state") from None
                data = migrate_run_state(checkpoint)
                recovery = data.get("recovery") or source_recovery
            else:
                recovery = source_recovery
        tasks = {entry["id"]: TaskRecord.from_dict(entry) for entry in data.get("tasks", [])}
        probe_rows = data.get("live_probe_evidence", [])
        if not isinstance(probe_rows, list):
            raise StateError("live-probe evidence must be a list", "invalid-live-probe-evidence")
        seen_probe_attempts: set[tuple[str, int, str]] = set()
        probe_limits: dict[tuple[str, int], int] = {}
        probe_counts: dict[tuple[str, int], int] = {}
        for raw_row in probe_rows:
            if not isinstance(raw_row, Mapping):
                raise StateError("malformed live-probe evidence", "invalid-live-probe-evidence")
            evidence = LiveProbeEvidence.from_dict(raw_row)
            if evidence.run_id != data["run_id"] or evidence.task_id not in tasks:
                raise StateError("live-probe evidence does not belong to this task/run", "live-probe-identity-mismatch")
            task = tasks[evidence.task_id]
            if task.adapter != evidence.adapter:
                raise StateError("live-probe adapter differs from the selected task adapter", "live-probe-identity-mismatch")
            if not cls._probe_contract_matches(task, evidence):
                raise StateError(
                    "live-probe evidence differs from the selected task contract",
                    "live-probe-contract-mismatch",
                )
            attempt_key = (evidence.task_id, evidence.task_contract_revision, evidence.attempt_id)
            if attempt_key in seen_probe_attempts:
                raise StateError("live-probe attempt identity is already recorded", "duplicate-live-probe-attempt")
            seen_probe_attempts.add(attempt_key)
            epoch_key = (evidence.task_id, evidence.task_contract_revision)
            prior_limit = probe_limits.setdefault(epoch_key, evidence.max_attempts)
            if prior_limit != evidence.max_attempts:
                raise StateError(
                    "live-probe attempt budget cannot change after the first observation",
                    "live-probe-budget-mismatch",
                )
            probe_counts[epoch_key] = probe_counts.get(epoch_key, 0) + 1
            if probe_counts[epoch_key] > prior_limit:
                raise StateError("live-probe attempt budget is exhausted", "live-probe-budget-exhausted")
        run = cls(
            data["feature"], data["prompt_path"], data.get("plan_path"), active_directory,
            Path(os.path.abspath(repo_root)), data["run_id"], data.get("status", "pending"),
            tasks, data.get("history", []), data.get("commands", []),
            data.get("controls", {}), data.get("environment", {}),
            data.get("current_task"), data.get("stages", {}), data.get("artifacts", {}),
            recovery, list(probe_rows),
        )
        run._persisted_live_probe_evidence = tuple(
            run._live_probe_row_fingerprint(row) for row in run.live_probe_evidence
        )
        return run

    def resume(self, *, feature: str, prompt_path: str | Path, plan_path: str | Path | None) -> None:
        if feature != self.feature or repo_relative(prompt_path, self.repo_root) != self.prompt_path or (repo_relative(plan_path, self.repo_root) if plan_path else None) != self.plan_path:
            raise ResumeError("resume request does not match persisted run identity", "resume-mismatch")
