"""Versioned, project-neutral contracts for the feature pipeline core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import json
import re


SCHEMA_VERSION = 1
TASK_TYPES = frozenset({
    "python", "rust", "backend", "frontend", "ui", "plugin", "docs", "research", "design",
    "tooling", "agent_config",
})
SUBAGENTS = frozenset({"executor", "task_verifier", "test_verifier"})
VERDICTS = frozenset({"PASS", "FAIL", "BLOCKED"})
RUN_STATUSES = frozenset({"planned", "running", "implemented", "verified", "blocked"})
SHELL_TOKENS = frozenset({"|", "&&", ";", ">", "<"})
WRITE_CAPABILITIES = frozenset({"write", "create", "delete", "modify", "filesystem_write"})
DIFF_POLICIES = frozenset({"ignore", "tracked-empty"})
VERIFICATION_TIERS = frozenset({"full", "scoped"})

#: A task ID as written on a board or in a plan: two-to-six letters, a hyphen, one-to-four
#: digits (``EDF-01``, ``LT-9``, ``PCC-05``). Kept deliberately looser than any one project's
#: convention so the portable core never rejects a legitimate upstream ID.
TASK_ID_RE = re.compile(r"^[A-Za-z]{2,6}-\d{1,4}$")


class SchemaError(ValueError):
    """Raised when an input violates a portable core contract."""


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{field} must be a non-empty string")
    return value


def validate_relative_path(value: object, field: str) -> str:
    """Normalize a repository-relative logical path without resolving it on disk."""
    path = _string(value, field).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", path) or path.startswith(("/", "~")):
        raise SchemaError(f"{field} must be relative")
    parts = path.split("/")
    if any(part == ".." for part in parts):
        raise SchemaError(f"{field} must not traverse outside its anchor")
    return "/".join(part for part in parts if part not in ("", ".")) or "."


@dataclass(frozen=True)
class LogicalPaths:
    project: str
    agents: str
    core: str

    @classmethod
    def from_data(cls, data: object) -> "LogicalPaths":
        value = _mapping(data, "logical_paths")
        return cls(*(validate_relative_path(value.get(name), f"logical_paths.{name}")
                     for name in ("project", "agents", "core")))


@dataclass(frozen=True)
class TaskMetadata:
    version: int
    task_type: str
    executor: str
    verifiers: tuple[str, str]

    @classmethod
    def from_data(cls, data: object) -> "TaskMetadata":
        value = _mapping(data, "task")
        _version(value)
        task_type = _string(value.get("type"), "task.type")
        if task_type not in TASK_TYPES:
            raise SchemaError(f"unknown task type: {task_type}")
        executor = _known_subagent(value.get("executor"), "task.executor")
        verifiers = _verifier_pair(value.get("verifiers"), "task.verifiers")
        return cls(SCHEMA_VERSION, task_type, executor, verifiers)


@dataclass(frozen=True)
class Verdict:
    version: int
    task_id: str
    task_verifier: str
    test_verifier: str

    @classmethod
    def from_data(cls, data: object) -> "Verdict":
        value = _mapping(data, "verdict")
        _version(value)
        task_id = _string(value.get("task_id"), "verdict.task_id")
        task_verifier = _verdict(value.get("task_verifier"), "verdict.task_verifier")
        test_verifier = _verdict(value.get("test_verifier"), "verdict.test_verifier")
        return cls(SCHEMA_VERSION, task_id, task_verifier, test_verifier)


@dataclass(frozen=True)
class RunState:
    version: int
    run_id: str
    status: str

    @classmethod
    def from_data(cls, data: object) -> "RunState":
        value = _mapping(data, "run_state")
        _version(value)
        status = _string(value.get("status"), "run_state.status")
        if status not in RUN_STATUSES:
            raise SchemaError(f"unknown run status: {status}")
        return cls(SCHEMA_VERSION, _string(value.get("run_id"), "run_state.run_id"), status)


@dataclass(frozen=True)
class ToolStage:
    """A project-declared deterministic stage: what to run, what it needs, what it must produce.

    ``prerequisites`` and ``outputs`` are anchor-relative logical paths; ``diff_policy`` says
    whether the stage is allowed to leave tracked changes behind (``ignore``) or must not
    (``tracked-empty``). The project supplies every value; the core never infers one.
    """

    name: str
    subagents: tuple[str, ...]
    argv: tuple[str, ...]
    prerequisites: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    timeout: float | None = None
    diff_policy: str = "ignore"

    @classmethod
    def from_data(cls, data: object) -> "ToolStage":
        value = _mapping(data, "stage")
        name = _string(value.get("name"), "stage.name")
        subagents = tuple(_known_subagent(item, "stage.subagents")
                          for item in _sequence(value.get("subagents"), "stage.subagents"))
        if not subagents:
            raise SchemaError("stage.subagents must not be empty")
        argv = tuple(_string(item, "stage.argv") for item in _sequence(value.get("argv"), "stage.argv"))
        if not argv or any(any(symbol in token for symbol in SHELL_TOKENS) for token in argv):
            raise SchemaError("stage.argv must be a non-empty shell-free argv")
        prerequisites = _relative_paths(value.get("prerequisites", []), "stage.prerequisites")
        outputs = _relative_paths(value.get("outputs", []), "stage.outputs")
        timeout = _optional_timeout(value.get("timeout"), "stage.timeout")
        diff_policy = value.get("diff_policy", "ignore")
        if diff_policy not in DIFF_POLICIES:
            raise SchemaError(f"stage.diff_policy must be one of {sorted(DIFF_POLICIES)}")
        return cls(name, subagents, argv, prerequisites, outputs, timeout, diff_policy)


@dataclass(frozen=True)
class Profile:
    version: int
    name: str
    logical_paths: LogicalPaths
    role_grants: Mapping[str, frozenset[str]]
    stages: tuple[ToolStage, ...]
    registry: "ProfileRegistry | None" = None

    @classmethod
    def from_data(cls, data: object) -> "Profile":
        value = _mapping(data, "profile")
        _version(value)
        paths = LogicalPaths.from_data(value.get("logical_paths"))
        grants = _role_grants(value.get("role_grants"))
        for verifier in ("task_verifier", "test_verifier"):
            if WRITE_CAPABILITIES & grants.get(verifier, frozenset()):
                raise SchemaError(f"verifier role '{verifier}' must be read-only")
        stages = tuple(ToolStage.from_data(item) for item in _sequence(value.get("stages"), "stages"))
        if not stages:
            raise SchemaError("stages must not be empty")
        if any(stage.name == "verification" for stage in stages):
            for stage in stages:
                if stage.name == "verification":
                    _verifier_pair(stage.subagents, "verification.subagents")
        registry_data = value.get("registry")
        registry = None if registry_data is None else ProfileRegistry.from_data(registry_data)
        return cls(SCHEMA_VERSION, _string(value.get("name"), "name"), paths, grants, stages, registry)


@dataclass(frozen=True)
class TaskRoute:
    """One project-declared route for a supported task type."""

    stack: str
    subagents: tuple[str, ...]
    root: str
    checks: tuple[str, ...]
    storage: str

    @classmethod
    def from_data(cls, data: object, task_type: str, registries: Mapping[str, object]) -> "TaskRoute":
        value = _mapping(data, f"registry.task_types.{task_type}")
        stack = _registry_name(value.get("stack"), "stack", registries["stacks"])
        root = _registry_name(value.get("root"), "root", registries["roots"])
        storage = _registry_name(value.get("storage"), "storage", registries["storage"])
        subagents = tuple(_registry_name(item, "subagent", registries["subagents"])
                          for item in _sequence(value.get("subagents"), "route.subagents"))
        checks = tuple(_registry_name(item, "check", registries["checks"])
                       for item in _sequence(value.get("checks"), "route.checks"))
        if not subagents:
            raise SchemaError(f"registry.task_types.{task_type}.subagents must not be empty")
        return cls(stack, subagents, root, checks, storage)


@dataclass(frozen=True)
class ProfileRegistry:
    """Closed project registry used to resolve task routing data."""

    task_types: Mapping[str, TaskRoute]
    stacks: Mapping[str, Mapping[str, Any]]
    subagents: Mapping[str, Mapping[str, Any]]
    roots: Mapping[str, str]
    checks: Mapping[str, tuple[str, ...]]
    storage: Mapping[str, str]

    @classmethod
    def from_data(cls, data: object) -> "ProfileRegistry":
        value = _mapping(data, "registry")
        required = ("task_types", "stacks", "subagents", "roots", "checks", "storage")
        if set(value) != set(required):
            raise SchemaError("registry must contain only task_types, stacks, subagents, roots, checks, and storage")
        stacks = _named_objects(value["stacks"], "registry.stacks")
        subagents = _named_objects(value["subagents"], "registry.subagents")
        roots = _named_paths(value["roots"], "registry.roots")
        checks = _named_argv(value["checks"], "registry.checks")
        storage = _named_paths(value["storage"], "registry.storage")
        registries: Mapping[str, object] = {"stacks": stacks, "subagents": subagents,
                                             "roots": roots, "checks": checks, "storage": storage}
        task_types_data = _mapping(value["task_types"], "registry.task_types")
        if not task_types_data:
            raise SchemaError("registry.task_types must not be empty")
        task_types: dict[str, TaskRoute] = {}
        for task_type, route in task_types_data.items():
            name = _string(task_type, "task type")
            if name not in TASK_TYPES:
                raise SchemaError(f"unknown task type: {name}")
            task_types[name] = TaskRoute.from_data(route, name, registries)
        return cls(task_types, stacks, subagents, roots, checks, storage)


def load_profile(path: Path) -> Profile:
    """Load and validate a profile before any caller can create run-state artifacts."""
    return Profile.from_data(json.loads(path.read_text(encoding="utf-8")))


def ensure_no_role_escalation(parent: Sequence[str], requested: Sequence[str]) -> tuple[str, ...]:
    """Reject a child role request that broadens the parent's granted capabilities."""
    parent_set = frozenset(_string(item, "parent role grant") for item in parent)
    requested_set = frozenset(_string(item, "requested role grant") for item in requested)
    if not requested_set <= parent_set:
        raise SchemaError("role escalation is not allowed")
    return tuple(sorted(requested_set))


def _registry_name(value: object, field: str, registry: object) -> str:
    name = _string(value, f"route.{field}")
    if name not in registry:
        raise SchemaError(f"unknown {field}: {name}")
    return name


def _named_objects(value: object, field: str) -> Mapping[str, Mapping[str, Any]]:
    entries = _mapping(value, field)
    if not entries:
        raise SchemaError(f"{field} must not be empty")
    return {_string(name, field): _mapping(entry, f"{field}.{name}") for name, entry in entries.items()}


def _named_paths(value: object, field: str) -> Mapping[str, str]:
    entries = _mapping(value, field)
    if not entries:
        raise SchemaError(f"{field} must not be empty")
    return {_string(name, field): validate_relative_path(path, f"{field}.{name}")
            for name, path in entries.items()}


def _named_argv(value: object, field: str) -> Mapping[str, tuple[str, ...]]:
    entries = _mapping(value, field)
    if not entries:
        raise SchemaError(f"{field} must not be empty")
    result: dict[str, tuple[str, ...]] = {}
    for name, argv_value in entries.items():
        argv = tuple(_string(item, f"{field}.{name}") for item in _sequence(argv_value, f"{field}.{name}"))
        if not argv or any(any(symbol in token for symbol in SHELL_TOKENS) for token in argv):
            raise SchemaError(f"{field}.{name} must be a non-empty shell-free argv")
        result[_string(name, field)] = argv
    return result


def _relative_paths(value: object, field: str) -> tuple[str, ...]:
    return tuple(validate_relative_path(item, field) for item in _sequence(value, field))


def _optional_timeout(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SchemaError(f"{field} must be a positive number of seconds")
    return float(value)


def _version(value: Mapping[str, Any]) -> None:
    if value.get("version") != SCHEMA_VERSION:
        raise SchemaError(f"unknown schema version: {value.get('version')}")


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise SchemaError(f"{field} must be a list")
    return value


def _known_subagent(value: object, field: str) -> str:
    subagent = _string(value, field)
    if subagent not in SUBAGENTS:
        raise SchemaError(f"unknown subagent: {subagent}")
    return subagent


def _verifier_pair(value: object, field: str) -> tuple[str, str]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{field} must be a list")
    pair = tuple(_known_subagent(item, field) for item in value)
    if pair != ("task_verifier", "test_verifier"):
        raise SchemaError(f"{field} must contain task_verifier and test_verifier in order")
    return pair


def _verdict(value: object, field: str) -> str:
    verdict = _string(value, field)
    if verdict not in VERDICTS:
        raise SchemaError(f"unknown verdict: {verdict}")
    return verdict


def _role_grants(value: object) -> Mapping[str, frozenset[str]]:
    grants = _mapping(value, "role_grants")
    result: dict[str, frozenset[str]] = {}
    for role, capabilities in grants.items():
        result[_string(role, "role name")] = frozenset(
            _string(item, f"role_grants.{role}") for item in _sequence(capabilities, f"role_grants.{role}"))
    return result


# --- normalized execution task contract (EDF-01) ------------------------------------------
#
# One project-neutral task model every later execution stage can trust. A Markdown task file
# and an equivalent JSON plan entry normalize to the same ``TaskSpec``; construction is
# fail-closed, so invalid or incomplete execution metadata raises here — before any run
# artifact is written or any executor process is launched.


def _shell_free_argv(value: object, field: str) -> tuple[str, ...]:
    argv = tuple(_string(item, field) for item in _sequence_like(value, field))
    if not argv:
        raise SchemaError(f"{field} must be a non-empty argv")
    for token in argv:
        if any(symbol in token for symbol in SHELL_TOKENS):
            raise SchemaError(
                f"{field} contains a shell operator ('{token}') — split chained commands into "
                f"separate entries so each has its own recorded exit code"
            )
    return argv


def _split_command_string(command: str) -> list[str]:
    """Split a command line on whitespace, honouring quoted segments. No shell is involved."""
    parts: list[str] = []
    current = ""
    quote: str | None = None
    for char in command:
        if quote:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in "\"'":
            quote = char
        elif char.isspace():
            if current:
                parts.append(current)
                current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def _sequence_like(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise SchemaError(f"{field} must be a list")
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{field} must be a list")
    return value


@dataclass(frozen=True)
class CommandSpec:
    """One verification command: a working directory and a shell-free argv."""

    cwd: str
    argv: tuple[str, ...]

    @classmethod
    def from_data(cls, data: object, *, field: str = "verification_commands") -> "CommandSpec":
        if isinstance(data, CommandSpec):
            return data
        value = _mapping(data, field)
        cwd = validate_relative_path(value.get("cwd", "."), f"{field}.cwd")
        if "argv" in value and value.get("argv") is not None:
            argv = _shell_free_argv(value.get("argv"), f"{field}.argv")
        elif value.get("command"):
            argv = _shell_free_argv(_split_command_string(_string(value.get("command"),
                                                                  f"{field}.command")),
                                    f"{field}.argv")
        else:
            raise SchemaError(f"{field} entry needs an 'argv' list or a 'command' string")
        return cls(cwd, argv)


@dataclass(frozen=True)
class AcceptanceCriterionSpec:
    """One ordered acceptance criterion. The checkbox is a verifier's finding, never set here."""

    id: str
    text: str
    checked: bool = False


def validate_acceptance_criteria(items: Sequence[object]) -> tuple[AcceptanceCriterionSpec, ...]:
    """Coerce ``items`` to ``AcceptanceCriterionSpec`` values with unique, gap-free ``AC-n`` IDs.

    Each item is an :class:`AcceptanceCriterionSpec`, a ``{"id","text","checked"}`` mapping, or a
    bare string (unchecked). IDs, when present, must already be ``AC-1 … AC-n`` in order.
    """
    criteria: list[AcceptanceCriterionSpec] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, AcceptanceCriterionSpec):
            text, checked, given_id = item.text, item.checked, item.id
        elif isinstance(item, Mapping):
            text = str(item.get("text", "")).strip()
            checked = bool(item.get("checked", False))
            given_id = str(item["id"]) if item.get("id") else None
        else:
            text, checked, given_id = str(item).strip(), False, None
        if not text:
            raise SchemaError(f"acceptance criterion {index} has no text")
        expected = f"AC-{index}"
        if given_id is not None and given_id != expected:
            raise SchemaError(
                f"acceptance criteria must be AC-1..AC-n without gaps; found '{given_id}' "
                f"where '{expected}' was expected"
            )
        criteria.append(AcceptanceCriterionSpec(expected, text, checked))
    return tuple(criteria)


def _spec_paths(value: object, field: str, *, allow_glob: bool = True,
                require_skill_md: bool = False) -> tuple[str, ...]:
    out: list[str] = []
    for item in _sequence_like(value, field):
        normalized = validate_relative_path(item, field)
        if not allow_glob and "*" in normalized:
            raise SchemaError(f"{field} entry '{item}' must be a concrete path, not a glob")
        if require_skill_md and not normalized.endswith("SKILL.md"):
            raise SchemaError(f"{field} entry '{item}' must name an exact SKILL.md")
        out.append(normalized)
    return tuple(out)


def _spec_task_ids(value: object, field: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in _sequence_like(value, field):
        token = _string(item, field)
        if not TASK_ID_RE.match(token):
            raise SchemaError(f"{field} entry '{token}' is not a valid task ID")
        out.append(token)
    return tuple(out)


@dataclass(frozen=True)
class TaskSpec:
    """A validated, project-neutral executable task. Immutable; build via :meth:`build`."""

    id: str
    title: str
    path: str
    task_type: str
    executor: str
    depends_on: tuple[str, ...]
    allowed_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    required_skills: tuple[str, ...]
    max_repair_attempts: int
    documentation_impact: tuple[str, ...]
    verification_commands: tuple[CommandSpec, ...]
    verification_tier: str
    accepts_scoped: tuple[str, ...]
    deferred_verification_commands: tuple[CommandSpec, ...]
    blocking_conditions: str | None
    acceptance_criteria: tuple[AcceptanceCriterionSpec, ...]
    metadata_source: str
    defaults_applied: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        id: str,
        task_type: str,
        executor: str,
        allowed_scope: Sequence[object],
        title: str = "",
        path: str = "",
        depends_on: Sequence[object] = (),
        out_of_scope: Sequence[object] = (),
        required_skills: Sequence[object] = (),
        max_repair_attempts: int = 2,
        documentation_impact: Sequence[object] = (),
        verification_commands: Sequence[object] = (),
        verification_tier: str = "full",
        accepts_scoped: Sequence[object] = (),
        deferred_verification_commands: Sequence[object] = (),
        blocking_conditions: object = None,
        acceptance_criteria: Sequence[object] = (),
        metadata_source: str = "declared",
        defaults_applied: Sequence[object] = (),
    ) -> "TaskSpec":
        task_id = _string(id, "task.id")
        if not TASK_ID_RE.match(task_id):
            raise SchemaError(f"invalid task ID '{task_id}'")

        normalized_type = _string(task_type, "task.type").strip().lower().replace("-", "_")
        if normalized_type not in TASK_TYPES:
            raise SchemaError(f"unknown task type: {task_type}")

        executor_name = _string(executor, "task.executor")

        scope = _spec_paths(allowed_scope, "allowed_scope")
        if not scope:
            raise SchemaError("allowed_scope must not be empty — a task at least writes its own file")

        deps = _spec_task_ids(depends_on, "depends_on")
        if task_id in deps:
            raise SchemaError(f"{task_id} depends on itself")

        scoped = _spec_task_ids(accepts_scoped, "accepts_scoped")
        for dep in scoped:
            if dep not in deps:
                raise SchemaError(
                    f"accepts_scoped entry '{dep}' is not one of this task's dependencies "
                    f"({', '.join(deps) or 'none'})"
                )

        tier = _string(verification_tier, "verification_tier").strip().lower()
        if tier not in VERIFICATION_TIERS:
            raise SchemaError(f"unknown verification tier: {verification_tier}")

        if isinstance(max_repair_attempts, bool) or not isinstance(max_repair_attempts, int):
            raise SchemaError("max_repair_attempts must be a non-negative integer")
        if max_repair_attempts < 0:
            raise SchemaError("max_repair_attempts must be a non-negative integer")

        commands = tuple(CommandSpec.from_data(item, field="verification_commands")
                         for item in _sequence_like(verification_commands, "verification_commands"))
        deferred = tuple(CommandSpec.from_data(item, field="deferred_verification_commands")
                         for item in _sequence_like(deferred_verification_commands,
                                                    "deferred_verification_commands"))
        if deferred and tier != "scoped":
            raise SchemaError(
                "deferred_verification_commands require verification_tier 'scoped'"
            )

        blocking = None if blocking_conditions is None else _string(blocking_conditions,
                                                                    "blocking_conditions")

        return cls(
            id=task_id,
            title=str(title or ""),
            path=str(path or ""),
            task_type=normalized_type,
            executor=executor_name,
            depends_on=deps,
            allowed_scope=scope,
            out_of_scope=_spec_paths(out_of_scope, "out_of_scope"),
            required_skills=_spec_paths(required_skills, "required_skills", allow_glob=False,
                                        require_skill_md=True),
            max_repair_attempts=max_repair_attempts,
            documentation_impact=_spec_paths(documentation_impact, "documentation_impact",
                                             allow_glob=False),
            verification_commands=commands,
            verification_tier=tier,
            accepts_scoped=scoped,
            deferred_verification_commands=deferred,
            blocking_conditions=blocking,
            acceptance_criteria=validate_acceptance_criteria(acceptance_criteria),
            metadata_source=str(metadata_source or "declared"),
            defaults_applied=tuple(_string(item, "defaults_applied")
                                   for item in _sequence_like(defaults_applied, "defaults_applied")),
        )
