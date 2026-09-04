"""Exhaustive, strict schema-v3 DTO readers and writers for durable run state.

**DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``).

Schema v3 is the versioned persistence boundary for ``run.json``. Every field is read
explicitly, with a typed :class:`~feature_pipeline.infrastructure.state.errors.StateSchemaError`
naming the exact ``field`` path on the first violation — a permissive reader would defer
corruption to execution and defeat resume safety (DS-02 Risks). Nothing is coerced: an
integer field given a string fails, an unknown status fails, a nested object given a list
fails.

Two version numbers travel in a v3 document and are deliberately kept apart
(``docs/adr/007`` §3):

* ``schema_version`` — the shape of *this persistence file*. ``3`` here.
* ``contract_version`` — ``feature_pipeline.contracts.SCHEMA_VERSION``, the unrelated
  task-spec / role / profile contract version. Recorded so a reader can tell the two apart
  instead of overloading one integer.

The v3 field set is the schema-v2 field set (the shape ``pipeline_core.state`` serializes)
plus those two explicit version fields and a ``revision`` token
(``docs/adr/005`` — the compare-and-set durability check is DS-03's to enforce; v3 only
reserves the wire field). The stable, well-defined records — task, verification,
implementation attribution, execution evidence, history event, command — are modelled
field-by-field. The open evidence payloads (``controls``, ``environment``, ``promotion``,
``attested_dependencies``, ``maintenance_audit``, ``external_launch_failures``, ``stages``,
``artifacts``) are validated *structurally* (object vs array, arrays are arrays of objects)
and carried through without loss.

**Unknown-field policy** is an explicit caller choice, never a silent default:

* ``"reject"`` (default) — an undefined key is a :class:`UnknownFieldError` at its path.
* ``"ignore"`` — undefined keys are dropped.
* ``"preserve"`` — undefined keys are kept on ``.extra`` and re-emitted by ``to_mapping``.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Mapping, Sequence

from feature_pipeline.contracts import SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION
from feature_pipeline.domain.vocabulary import RunStatus, TaskStatus, Verdict

from .errors import (
    FieldTypeError,
    FieldValueError,
    MissingFieldError,
    SchemaVersionError,
    UnknownFieldError,
)

#: The persistence schema this module reads and writes.
STATE_SCHEMA_VERSION = 3

#: Every ``schema_version`` :func:`load_state` can read. ``2`` is migrated in memory; ``3``
#: is read directly. Anything else fails closed.
SUPPORTED_READ_SCHEMA_VERSIONS: frozenset[int] = frozenset({2, 3})

UnknownFieldPolicy = Literal["reject", "ignore", "preserve"]

_TASK_STATUSES: frozenset[str] = frozenset(TaskStatus.values())
_VERDICTS: frozenset[str] = frozenset(Verdict.values())
#: ``pipeline_core.state.Run`` defaults ``status`` to ``"pending"`` before the first
#: transition, so it is accepted alongside the declared :class:`RunStatus` members.
_RUN_STATUSES: frozenset[str] = frozenset(RunStatus.values()) | {"pending"}


# ==========================================================================================
# Field readers — each raises a StateSchemaError carrying the failing ``field`` path.
# ==========================================================================================


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _index(path: str, i: int) -> str:
    return f"{path}[{i}]"


def _require(data: Mapping[str, object], key: str, path: str) -> object:
    if key not in data:
        raise MissingFieldError(f"required field {key!r} is missing", field=_join(path, key))
    return data[key]


def _str(value: object, field_path: str) -> str:
    if not isinstance(value, str):
        raise FieldTypeError(
            f"expected a string, got {type(value).__name__}", field=field_path
        )
    return value


def _opt_str(value: object, field_path: str) -> str | None:
    return None if value is None else _str(value, field_path)


def _int(value: object, field_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FieldTypeError(
            f"expected an integer, got {type(value).__name__}", field=field_path
        )
    return value


def _number(value: object, field_path: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FieldTypeError(
            f"expected a number, got {type(value).__name__}", field=field_path
        )
    return value


def _mapping(value: object, field_path: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise FieldTypeError(
            f"expected an object, got {type(value).__name__}", field=field_path
        )
    return value


def _list(value: object, field_path: str) -> list[object]:
    if not isinstance(value, list):
        raise FieldTypeError(
            f"expected an array, got {type(value).__name__}", field=field_path
        )
    return value


def _str_list(value: object, field_path: str) -> tuple[str, ...]:
    return tuple(
        _str(item, _index(field_path, i)) for i, item in enumerate(_list(value, field_path))
    )


def _mapping_list(value: object, field_path: str) -> tuple[dict[str, object], ...]:
    return tuple(
        dict(_mapping(item, _index(field_path, i)))
        for i, item in enumerate(_list(value, field_path))
    )


def _enum(value: object, field_path: str, allowed: frozenset[str]) -> str:
    text = _str(value, field_path)
    if text not in allowed:
        raise FieldValueError(
            f"{text!r} is not one of {{{', '.join(sorted(allowed))}}}", field=field_path
        )
    return text


def _exit_code(value: object, field_path: str) -> object:
    if value is None or isinstance(value, (int, str)):
        return value
    raise FieldTypeError(
        f"expected an integer, a string, or null, got {type(value).__name__}",
        field=field_path,
    )


def _resolve_unknown(
    data: Mapping[str, object], known: frozenset[str], path: str, policy: UnknownFieldPolicy
) -> dict[str, object]:
    extra = {key: value for key, value in data.items() if key not in known}
    if not extra:
        return {}
    if policy == "reject":
        key = next(iter(extra))
        raise UnknownFieldError(
            f"unexpected field {key!r}", field=_join(path, key), code="unknown-field"
        )
    if policy == "ignore":
        return {}
    return extra


# ==========================================================================================
# Modelled records.
# ==========================================================================================


@dataclass(frozen=True)
class VerificationV3:
    """The two independent verifier verdicts and when they settled."""

    task_verdict: str | None = None
    test_verdict: str | None = None
    verified_at: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    _KNOWN = frozenset({"task_verdict", "test_verdict", "verified_at"})

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, path: str, policy: UnknownFieldPolicy
    ) -> "VerificationV3":
        def verdict(key: str) -> str | None:
            raw = data.get(key)
            return None if raw is None else _enum(raw, _join(path, key), _VERDICTS)

        return cls(
            task_verdict=verdict("task_verdict"),
            test_verdict=verdict("test_verdict"),
            verified_at=_opt_str(data.get("verified_at"), _join(path, "verified_at")),
            extra=_resolve_unknown(data, cls._KNOWN, path, policy),
        )

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {
            "task_verdict": self.task_verdict,
            "test_verdict": self.test_verdict,
            "verified_at": self.verified_at,
        }
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class ImplementationV3:
    """The executor-window delta attributed to one launch."""

    state: str = "not-attempted"
    changed_files: tuple[str, ...] = ()
    manifest: str | None = None
    diff: str | None = None
    reason: str | None = None
    manifest_reserved: str | None = None
    diff_reserved: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    _CORE = ("state", "changed_files", "manifest", "diff")
    _EXTENDED = ("reason", "manifest_reserved", "diff_reserved")
    _KNOWN = frozenset(_CORE + _EXTENDED)

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, path: str, policy: UnknownFieldPolicy
    ) -> "ImplementationV3":
        return cls(
            state=_str(_require(data, "state", path), _join(path, "state")),
            changed_files=_str_list(
                data.get("changed_files", []), _join(path, "changed_files")
            ),
            manifest=_opt_str(data.get("manifest"), _join(path, "manifest")),
            diff=_opt_str(data.get("diff"), _join(path, "diff")),
            reason=_opt_str(data.get("reason"), _join(path, "reason")),
            manifest_reserved=_opt_str(
                data.get("manifest_reserved"), _join(path, "manifest_reserved")
            ),
            diff_reserved=_opt_str(
                data.get("diff_reserved"), _join(path, "diff_reserved")
            ),
            extra=_resolve_unknown(data, cls._KNOWN, path, policy),
        )

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {
            "state": self.state,
            "changed_files": list(self.changed_files),
            "manifest": self.manifest,
            "diff": self.diff,
        }
        for name in self._EXTENDED:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class EvidenceV3:
    """Execution evidence for a task — unavailable attribution is stated, not implied."""

    attempt: int = 0
    executor_report: str | None = None
    launch_generation: int | None = None
    implementation: ImplementationV3 = field(default_factory=ImplementationV3)
    extra: Mapping[str, object] = field(default_factory=dict)

    _CORE = ("attempt", "executor_report", "implementation")
    _KNOWN = frozenset(_CORE + ("launch_generation",))

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, path: str, policy: UnknownFieldPolicy
    ) -> "EvidenceV3":
        impl_path = _join(path, "implementation")
        launch = data.get("launch_generation")
        return cls(
            attempt=_int(_require(data, "attempt", path), _join(path, "attempt")),
            executor_report=_opt_str(
                _require(data, "executor_report", path), _join(path, "executor_report")
            ),
            launch_generation=None
            if launch is None
            else _int(launch, _join(path, "launch_generation")),
            implementation=ImplementationV3.from_mapping(
                _mapping(_require(data, "implementation", path), impl_path),
                path=impl_path,
                policy=policy,
            ),
            extra=_resolve_unknown(data, cls._KNOWN, path, policy),
        )

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {"attempt": self.attempt}
        if self.launch_generation is not None:
            out["launch_generation"] = self.launch_generation
        out["executor_report"] = self.executor_report
        out["implementation"] = self.implementation.to_mapping()
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class HistoryEventV3:
    """One durable lifecycle event."""

    at: str
    scope: str
    actor: str
    from_: str | None = None
    to: str | None = None
    note: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    _KNOWN = frozenset({"at", "scope", "actor", "from", "to", "note"})

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, path: str, policy: UnknownFieldPolicy
    ) -> "HistoryEventV3":
        return cls(
            at=_str(_require(data, "at", path), _join(path, "at")),
            scope=_str(_require(data, "scope", path), _join(path, "scope")),
            actor=_str(_require(data, "actor", path), _join(path, "actor")),
            from_=_opt_str(data.get("from"), _join(path, "from")),
            to=_opt_str(data.get("to"), _join(path, "to")),
            note=_opt_str(data.get("note"), _join(path, "note")),
            extra=_resolve_unknown(data, cls._KNOWN, path, policy),
        )

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {
            "at": self.at,
            "scope": self.scope,
            "from": self.from_,
            "to": self.to,
            "actor": self.actor,
            "note": self.note,
        }
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class CommandRecordV3:
    """One recorded command invocation — pure evidence, the runner owns capture."""

    id: str
    stage: str
    cwd: str
    argv: tuple[str, ...]
    exit_code: object
    duration: float | int
    stdout: str
    stderr: str
    extra: Mapping[str, object] = field(default_factory=dict)

    _KNOWN = frozenset(
        {"id", "stage", "cwd", "argv", "exit_code", "duration", "stdout", "stderr"}
    )

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, path: str, policy: UnknownFieldPolicy
    ) -> "CommandRecordV3":
        return cls(
            id=_str(_require(data, "id", path), _join(path, "id")),
            stage=_str(_require(data, "stage", path), _join(path, "stage")),
            cwd=_str(_require(data, "cwd", path), _join(path, "cwd")),
            argv=_str_list(_require(data, "argv", path), _join(path, "argv")),
            exit_code=_exit_code(
                _require(data, "exit_code", path), _join(path, "exit_code")
            ),
            duration=_number(_require(data, "duration", path), _join(path, "duration")),
            stdout=_str(_require(data, "stdout", path), _join(path, "stdout")),
            stderr=_str(_require(data, "stderr", path), _join(path, "stderr")),
            extra=_resolve_unknown(data, cls._KNOWN, path, policy),
        )

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {
            "id": self.id,
            "stage": self.stage,
            "cwd": self.cwd,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "duration": self.duration,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class TaskEntryV3:
    """One task's durable identity, execution, and verification facts (schema v3).

    The field set mirrors ``pipeline_core.state.TaskRecord`` exactly; v3 adds strictness and
    the explicit unknown-field policy, not new task fields.
    """

    id: str
    status: str
    depends_on: tuple[str, ...] = ()
    attempts: int = 0
    blocker: str | None = None
    type: str | None = None
    executor: str | None = None
    adapter: str | None = None
    session_id: str | None = None
    verification: VerificationV3 = field(default_factory=VerificationV3)
    execution_evidence: EvidenceV3 = field(default_factory=EvidenceV3)
    changed_files: tuple[str, ...] = ()
    verification_tier: str = "full"
    accepts_scoped: tuple[str, ...] = ()
    promotion: dict[str, object] | None = None
    unblocks: tuple[str, ...] = ()
    maintenance_audit: tuple[dict[str, object], ...] = ()
    external_launch_failures: tuple[dict[str, object], ...] = ()
    attested_dependencies: tuple[dict[str, object], ...] = ()
    next_executor_launch_generation: int = 1
    next_task_verifier_launch_generation: int = 1
    next_test_verifier_launch_generation: int = 1
    extra: Mapping[str, object] = field(default_factory=dict)

    _KNOWN = frozenset(
        {
            "id", "status", "depends_on", "attempts", "blocker", "type", "executor",
            "adapter", "session_id", "verification", "execution_evidence", "changed_files",
            "verification_tier", "accepts_scoped", "promotion", "unblocks",
            "maintenance_audit", "external_launch_failures", "attested_dependencies",
            "next_executor_launch_generation", "next_task_verifier_launch_generation",
            "next_test_verifier_launch_generation",
        }
    )

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, path: str, policy: UnknownFieldPolicy
    ) -> "TaskEntryV3":
        verification_path = _join(path, "verification")
        evidence_path = _join(path, "execution_evidence")

        def generation(key: str) -> int:
            return _int(data.get(key, 1), _join(path, key))

        promotion_raw = data.get("promotion")
        promotion = (
            None
            if promotion_raw is None
            else dict(_mapping(promotion_raw, _join(path, "promotion")))
        )
        return cls(
            id=_str(_require(data, "id", path), _join(path, "id")),
            status=_enum(_require(data, "status", path), _join(path, "status"), _TASK_STATUSES),
            depends_on=_str_list(data.get("depends_on", []), _join(path, "depends_on")),
            attempts=_int(data.get("attempts", 0), _join(path, "attempts")),
            blocker=_opt_str(data.get("blocker"), _join(path, "blocker")),
            type=_opt_str(data.get("type"), _join(path, "type")),
            executor=_opt_str(data.get("executor"), _join(path, "executor")),
            adapter=_opt_str(data.get("adapter"), _join(path, "adapter")),
            session_id=_opt_str(data.get("session_id"), _join(path, "session_id")),
            verification=VerificationV3.from_mapping(
                _mapping(data.get("verification", {}), verification_path),
                path=verification_path,
                policy=policy,
            ),
            execution_evidence=EvidenceV3.from_mapping(
                _mapping(
                    _require(data, "execution_evidence", path), evidence_path
                ),
                path=evidence_path,
                policy=policy,
            ),
            changed_files=_str_list(
                data.get("changed_files", []), _join(path, "changed_files")
            ),
            verification_tier=_str(
                data.get("verification_tier", "full"), _join(path, "verification_tier")
            ),
            accepts_scoped=_str_list(
                data.get("accepts_scoped", []), _join(path, "accepts_scoped")
            ),
            promotion=promotion,
            unblocks=_str_list(data.get("unblocks", []), _join(path, "unblocks")),
            maintenance_audit=_mapping_list(
                data.get("maintenance_audit", []), _join(path, "maintenance_audit")
            ),
            external_launch_failures=_mapping_list(
                data.get("external_launch_failures", []),
                _join(path, "external_launch_failures"),
            ),
            attested_dependencies=_mapping_list(
                data.get("attested_dependencies", []),
                _join(path, "attested_dependencies"),
            ),
            next_executor_launch_generation=generation(
                "next_executor_launch_generation"
            ),
            next_task_verifier_launch_generation=generation(
                "next_task_verifier_launch_generation"
            ),
            next_test_verifier_launch_generation=generation(
                "next_test_verifier_launch_generation"
            ),
            extra=_resolve_unknown(data, cls._KNOWN, path, policy),
        )

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {
            "id": self.id,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "attempts": self.attempts,
            "blocker": self.blocker,
            "type": self.type,
            "executor": self.executor,
            "adapter": self.adapter,
            "session_id": self.session_id,
            "verification": self.verification.to_mapping(),
            "execution_evidence": self.execution_evidence.to_mapping(),
            "changed_files": list(self.changed_files),
            "verification_tier": self.verification_tier,
            "accepts_scoped": list(self.accepts_scoped),
            "promotion": None if self.promotion is None else dict(self.promotion),
            "unblocks": list(self.unblocks),
            "maintenance_audit": [dict(entry) for entry in self.maintenance_audit],
            "external_launch_failures": [
                dict(entry) for entry in self.external_launch_failures
            ],
            "attested_dependencies": [
                dict(entry) for entry in self.attested_dependencies
            ],
            "next_executor_launch_generation": self.next_executor_launch_generation,
            "next_task_verifier_launch_generation": (
                self.next_task_verifier_launch_generation
            ),
            "next_test_verifier_launch_generation": (
                self.next_test_verifier_launch_generation
            ),
        }
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class RunStateV3:
    """A run's complete durable state at schema v3."""

    feature: str
    prompt_path: str
    plan_path: str | None
    run_id: str
    status: str
    contract_version: int
    revision: int = 0
    schema_version: int = STATE_SCHEMA_VERSION
    current_task: str | None = None
    controls: dict[str, object] = field(default_factory=dict)
    environment: dict[str, object] = field(default_factory=dict)
    tasks: tuple[TaskEntryV3, ...] = ()
    history: tuple[HistoryEventV3, ...] = ()
    commands: tuple[CommandRecordV3, ...] = ()
    stages: dict[str, object] = field(default_factory=dict)
    artifacts: dict[str, object] = field(default_factory=dict)
    extra: Mapping[str, object] = field(default_factory=dict)

    _KNOWN = frozenset(
        {
            "schema_version", "contract_version", "revision", "feature", "prompt_path",
            "plan_path", "run_id", "status", "current_task", "controls", "environment",
            "tasks", "history", "commands", "stages", "artifacts",
        }
    )

    # -- reading ---------------------------------------------------------------------------

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, object],
        *,
        unknown_fields: UnknownFieldPolicy = "reject",
    ) -> "RunStateV3":
        version = _require(data, "schema_version", "")
        if isinstance(version, bool) or not isinstance(version, int):
            raise SchemaVersionError(
                f"schema_version must be an integer, got {type(version).__name__}",
                field="schema_version",
            )
        if version != STATE_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"this reader parses schema_version {STATE_SCHEMA_VERSION}; got {version}",
                field="schema_version",
            )
        tasks = tuple(
            TaskEntryV3.from_mapping(
                _mapping(entry, _index("tasks", i)),
                path=_index("tasks", i),
                policy=unknown_fields,
            )
            for i, entry in enumerate(_list(_require(data, "tasks", ""), "tasks"))
        )
        history = tuple(
            HistoryEventV3.from_mapping(
                _mapping(entry, _index("history", i)),
                path=_index("history", i),
                policy=unknown_fields,
            )
            for i, entry in enumerate(_list(data.get("history", []), "history"))
        )
        commands = tuple(
            CommandRecordV3.from_mapping(
                _mapping(entry, _index("commands", i)),
                path=_index("commands", i),
                policy=unknown_fields,
            )
            for i, entry in enumerate(_list(data.get("commands", []), "commands"))
        )
        return cls(
            feature=_str(_require(data, "feature", ""), "feature"),
            prompt_path=_str(_require(data, "prompt_path", ""), "prompt_path"),
            plan_path=_opt_str(_require(data, "plan_path", ""), "plan_path"),
            run_id=_str(_require(data, "run_id", ""), "run_id"),
            status=_enum(_require(data, "status", ""), "status", _RUN_STATUSES),
            contract_version=_int(
                _require(data, "contract_version", ""), "contract_version"
            ),
            revision=_int(_require(data, "revision", ""), "revision"),
            schema_version=version,
            current_task=_opt_str(data.get("current_task"), "current_task"),
            controls=dict(_mapping(data.get("controls", {}), "controls")),
            environment=dict(_mapping(data.get("environment", {}), "environment")),
            tasks=tasks,
            history=history,
            commands=commands,
            stages=dict(_mapping(data.get("stages", {}), "stages")),
            artifacts=dict(_mapping(data.get("artifacts", {}), "artifacts")),
            extra=_resolve_unknown(data, cls._KNOWN, "", unknown_fields),
        )

    # -- writing --------------------------------------------------------------------------

    def to_mapping(self) -> dict[str, object]:
        out: dict[str, object] = {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "revision": self.revision,
            "feature": self.feature,
            "prompt_path": self.prompt_path,
            "plan_path": self.plan_path,
            "run_id": self.run_id,
            "status": self.status,
            "current_task": self.current_task,
            "controls": dict(self.controls),
            "environment": dict(self.environment),
            "tasks": [task.to_mapping() for task in self.tasks],
            "history": [event.to_mapping() for event in self.history],
            "commands": [command.to_mapping() for command in self.commands],
            "stages": dict(self.stages),
            "artifacts": dict(self.artifacts),
        }
        out.update(self.extra)
        return out

    # -- convenience --------------------------------------------------------------------

    def task(self, task_id: str) -> TaskEntryV3:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    def with_revision(self, revision: int) -> "RunStateV3":
        return replace(self, revision=revision)


__all__ = [
    "STATE_SCHEMA_VERSION",
    "SUPPORTED_READ_SCHEMA_VERSIONS",
    "CONTRACT_SCHEMA_VERSION",
    "UnknownFieldPolicy",
    "VerificationV3",
    "ImplementationV3",
    "EvidenceV3",
    "HistoryEventV3",
    "CommandRecordV3",
    "TaskEntryV3",
    "RunStateV3",
]
