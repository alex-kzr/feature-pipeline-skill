"""The typed compiled profile — profiles own task-type routes (CP-01).

``pipeline_core`` reads the generated project profile
(``tools/feature-pipeline/config/pipeline.profile.json`` + its sibling ``checks.json``)
through :func:`pipeline_core.project_profile.load_runnable_profile`, which *synthesises* a
native :class:`feature_pipeline.contracts.Profile`. CP-01 instead parses the same document into
small typed value
objects the plan compiler consumes directly, so route resolution lives in one place and every
rejection is a :class:`~feature_pipeline.domain.errors.DomainError` raised before any run
artifact exists.

The route synthesis mirrors ``project_profile._from_project_profile`` exactly: every declared
task type is routed to its ``working_root`` and its own explicitly declared ``stack``; the
neutral ``executor`` subagent runs every route; each route lists the checks the profile's
canonical ``stacks[]`` binding (``{id, role, checks}``, TC-08) claims for that stack, plus every
check explicitly marked ``required`` (a repository-wide gate no route may silently drop);
storage is the single ``run_state`` key bound to ``run_state_path``. Parity against the shipped
:func:`pipeline_core.profiles.resolve_route` is pinned by ``tests/test_execution_plan_compiler.py``.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.paths import RelativePath

#: The only project-profile ``schema_version`` this core understands.
SUPPORTED_SCHEMA_VERSION = 1

#: Keys a ``schema_version`` project profile must carry. ``stacks`` is the canonical
#: ``{id, role, checks}`` binding introduced in TC-08; a project profile that predates it is a
#: legacy document rejected with an explicit diagnostic (see :class:`InvalidProfile`'s message
#: below), never silently synthesised from a route or a task type.
_REQUIRED_KEYS = ("project", "anchors", "task_routing", "stacks", "run_state_path", "roles")

#: Keys that only ever appear in a native core profile — their presence is ambiguity.
_NATIVE_ONLY_KEYS = ("version", "logical_paths", "stages", "registry")

#: The neutral executor subagent every synthesised route runs through.
_EXECUTOR = "executor"

#: The single storage key the synthesised registry binds to ``run_state_path``.
_STORAGE_KEY = "run_state"


class InvalidProfile(DomainError):
    """The generated project profile is ambiguous, unknown-version, or half-converted."""

    code = "invalid-profile"


class UnknownRoute(DomainError):
    """A task type with no declared route in the profile registry."""

    code = "unknown-route"


class UnknownCheck(DomainError):
    """A route names a check the profile does not declare."""

    code = "unknown-check"


class UnknownStack(DomainError):
    """A route or check names a stack id no ``stacks[]`` entry declares."""

    code = "unknown-stack"


@dataclass(frozen=True)
class CheckCommand:
    """One repository-declared verification command.

    ``required`` marks a check that every route must carry regardless of its own stack (a
    repository-wide gate such as a whitespace check) — it is never a substitute for a route's
    own stack-matched checks, only an addition to them.
    """

    name: str
    stack: str
    argv: tuple[str, ...]
    cwd: RelativePath
    required: bool = False


@dataclass(frozen=True)
class StackBinding:
    """One profile-declared ``stacks[]`` entry: the canonical ``{id, role, checks}`` binding a
    route's ``stack`` resolves through (TC-08). ``role`` is the one semantic role this stack is
    bound to (a name from the profile's own ``roles[]``, never inferred), and ``check_names`` is
    exactly the set of checks this stack owns — a route's resolved checks are this set plus any
    check explicitly marked ``required`` elsewhere.
    """

    id: str
    role: str
    check_names: tuple[str, ...]


@dataclass(frozen=True)
class RoutePolicy:
    """One project-declared route for a supported task type."""

    task_type: str
    working_root: RelativePath
    stack: str
    subagents: tuple[str, ...]
    check_names: tuple[str, ...]
    storage_key: str


@dataclass(frozen=True)
class CompiledProfile:
    """The typed generated project profile: anchors, routes, role grants, checks, storage."""

    project: str
    agents_root: RelativePath
    core_root: RelativePath
    run_state_path: RelativePath
    routes: Mapping[str, RoutePolicy]
    role_grants: Mapping[str, tuple[str, ...]]
    checks: Mapping[str, CheckCommand]
    storage: Mapping[str, RelativePath]
    #: The canonical ``stacks[]`` binding (TC-08). Defaults to empty for profiles built
    #: directly (not through :meth:`from_mapping`) by hand-authored test fixtures that predate
    #: this concept; :meth:`from_mapping` always populates it from the generated document.
    stacks: Mapping[str, StackBinding] = field(default_factory=dict)

    # -- lookups (fail closed) --------------------------------------------------------

    def route_for(self, task_type: str) -> RoutePolicy:
        try:
            return self.routes[task_type]
        except KeyError:
            raise UnknownRoute(f"unregistered task type: {task_type}") from None

    def check_command(self, name: str) -> CheckCommand:
        try:
            return self.checks[name]
        except KeyError:
            raise UnknownCheck(f"unknown check: {name}") from None

    def stack_for(self, stack_id: str) -> StackBinding:
        try:
            return self.stacks[stack_id]
        except KeyError:
            raise UnknownStack(f"unregistered stack id: {stack_id}") from None

    def storage_root(self, key: str) -> RelativePath:
        try:
            return self.storage[key]
        except KeyError:
            raise InvalidProfile(f"unknown storage key: {key}")

    # -- construction --------------------------------------------------------------

    @classmethod
    def from_path(cls, profile_path: Path | str) -> "CompiledProfile":
        """Load ``profile_path`` and its sibling ``checks.json`` from disk."""
        path = Path(profile_path)
        raw = _json_object(path.read_text(encoding="utf-8"), "profile")
        checks_path = path.parent / "checks.json"
        checks_doc = (
            _json_object(checks_path.read_text(encoding="utf-8"), "checks.json")
            if checks_path.is_file()
            else None
        )
        return cls.from_mapping(raw, checks_doc)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        checks_doc: Mapping[str, Any] | None = None,
    ) -> "CompiledProfile":
        """Convert the generated ``schema_version`` document (plus its checks) to typed data."""
        if not isinstance(raw, Mapping):
            raise InvalidProfile("profile must be an object")
        if "version" in raw:
            raise InvalidProfile(
                "profile carries a native-core 'version' key; a generated project profile "
                "declares 'schema_version'"
            )
        version = raw.get("schema_version")
        if version != SUPPORTED_SCHEMA_VERSION:
            raise InvalidProfile(
                f"unknown project profile schema_version: {version!r} "
                f"(this core understands {SUPPORTED_SCHEMA_VERSION})"
            )
        for key in _REQUIRED_KEYS:
            if key not in raw:
                if key == "stacks":
                    raise InvalidProfile(
                        "project profile is missing required key 'stacks' — this profile "
                        "predates the explicit {id, role, checks} stack binding (TC-08); "
                        "regenerate it with the current feature-pipeline-project-setup "
                        "generator rather than hand-patching a stacks array onto it"
                    )
                raise InvalidProfile(f"project profile is missing required key {key!r}")
        for key in _NATIVE_ONLY_KEYS:
            if key in raw:
                raise InvalidProfile(
                    f"project profile carries native-core key {key!r}; a generated profile "
                    "declares anchors, task_routing, roles and run_state_path only"
                )

        project = _non_empty_str(raw.get("project"), "project")
        anchors = _mapping(raw.get("anchors"), "anchors")
        agents_root = _relative(anchors.get("agents_root"), "anchors.agents_root")
        core_root = _relative(anchors.get("core_root"), "anchors.core_root")
        run_state_path = _relative(raw.get("run_state_path"), "run_state_path")

        checks = _parse_checks(checks_doc)
        required_checks = frozenset(name for name, check in checks.items() if check.required)

        roles = _sequence(raw.get("roles"), "roles")
        if not roles:
            raise InvalidProfile("roles must not be empty")
        role_grants: dict[str, tuple[str, ...]] = {}
        for index, entry in enumerate(roles):
            obj = _mapping(entry, f"roles[{index}]")
            role = _non_empty_str(obj.get("role"), f"roles[{index}].role")
            grants = tuple(
                _non_empty_str(grant, f"roles[{index}].min_grants[]")
                for grant in _sequence(obj.get("min_grants"), f"roles[{index}].min_grants")
            )
            if not grants:
                raise InvalidProfile(f"roles[{index}].min_grants must not be empty")
            role_grants[role] = grants

        stacks = _parse_stacks(raw.get("stacks"), checks, frozenset(role_grants))

        routing = _sequence(raw.get("task_routing"), "task_routing")
        if not routing:
            raise InvalidProfile("task_routing must not be empty")
        routes: dict[str, RoutePolicy] = {}
        for index, entry in enumerate(routing):
            obj = _mapping(entry, f"task_routing[{index}]")
            task_type = _non_empty_str(
                obj.get("task_type"), f"task_routing[{index}].task_type"
            )
            working_root = _relative(
                obj.get("working_root"), f"task_routing[{index}].working_root"
            )
            stack = _non_empty_str(obj.get("stack"), f"task_routing[{index}].stack")
            if task_type in routes:
                raise InvalidProfile(f"task_routing has a duplicate task type: {task_type!r}")
            try:
                binding = stacks[stack]
            except KeyError:
                raise InvalidProfile(
                    f"task_routing[{index}] stack {stack!r} is not declared in this "
                    "profile's stacks[]"
                ) from None
            route_checks = tuple(sorted(set(binding.check_names) | required_checks))
            if not route_checks:
                raise InvalidProfile(
                    f"task_routing[{index}] stack {stack!r} resolves no declared checks — "
                    "declare a check under its stacks[] entry or mark a check 'required'"
                )
            routes[task_type] = RoutePolicy(
                task_type=task_type,
                working_root=working_root,
                stack=stack,
                subagents=(_EXECUTOR,),
                check_names=route_checks,
                storage_key=_STORAGE_KEY,
            )

        return cls(
            project=project,
            agents_root=agents_root,
            core_root=core_root,
            run_state_path=run_state_path,
            routes=routes,
            role_grants=role_grants,
            checks=checks,
            storage={_STORAGE_KEY: run_state_path},
            stacks=stacks,
        )


# --- parsing helpers -----------------------------------------------------------------------


def _parse_stacks(
    value: object,
    checks: Mapping[str, CheckCommand],
    declared_roles: frozenset[str],
) -> dict[str, StackBinding]:
    """Parse and cross-validate the canonical ``stacks[]`` binding.

    Rejects a duplicate stack id, an unknown role (not one of the profile's own declared
    ``roles[]`` names), a stack that claims a check ``checks.json`` does not declare, and a
    stack/check-id mismatch (the check's own declared ``stack`` disagrees with the stacks[]
    entry claiming it). Every ``checks.json`` check must also be claimed by exactly one stack —
    an orphaned check is as much a mismatch as a wrongly claimed one.
    """
    entries = _sequence(value, "stacks")
    if not entries:
        raise InvalidProfile("stacks must not be empty")
    stacks: dict[str, StackBinding] = {}
    claimed_by: dict[str, str] = {}
    for index, entry in enumerate(entries):
        obj = _mapping(entry, f"stacks[{index}]")
        stack_id = _non_empty_str(obj.get("id"), f"stacks[{index}].id")
        role = _non_empty_str(obj.get("role"), f"stacks[{index}].role")
        if role not in declared_roles:
            raise InvalidProfile(
                f"stacks[{index}].role {role!r} is not one of the declared roles: "
                f"{sorted(declared_roles)}"
            )
        if stack_id in stacks:
            raise InvalidProfile(f"stacks has a duplicate id: {stack_id!r}")
        check_names = tuple(
            _non_empty_str(name, f"stacks[{index}].checks[]")
            for name in _sequence(obj.get("checks"), f"stacks[{index}].checks")
        )
        for name in check_names:
            check = checks.get(name)
            if check is None:
                raise InvalidProfile(
                    f"stacks[{index}] ({stack_id!r}) claims check {name!r}, which is not "
                    "declared in checks.json"
                )
            if check.stack != stack_id:
                raise InvalidProfile(
                    f"checks.json check {name!r} declares stack {check.stack!r}, but "
                    f"stacks[{index}] claims it under {stack_id!r}"
                )
            prior_owner = claimed_by.get(name)
            if prior_owner is not None and prior_owner != stack_id:
                raise InvalidProfile(
                    f"check {name!r} is claimed by both stacks {prior_owner!r} and "
                    f"{stack_id!r}"
                )
            claimed_by[name] = stack_id
        stacks[stack_id] = StackBinding(id=stack_id, role=role, check_names=check_names)

    orphaned = sorted(set(checks) - set(claimed_by))
    if orphaned:
        raise InvalidProfile(
            f"checks.json declares check(s) not claimed by any stacks[] entry: {orphaned}"
        )
    return stacks


def _parse_checks(checks_doc: Mapping[str, Any] | None) -> dict[str, CheckCommand]:
    if checks_doc is not None:
        if not isinstance(checks_doc, Mapping):
            raise InvalidProfile("checks.json must be an object")
        if checks_doc.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            raise InvalidProfile(
                f"checks.json schema_version must be {SUPPORTED_SCHEMA_VERSION}, "
                f"got {checks_doc.get('schema_version')!r}"
            )
    entries = _sequence((checks_doc or {}).get("checks", []), "checks.json checks")
    checks: dict[str, CheckCommand] = {}
    for index, entry in enumerate(entries):
        obj = _mapping(entry, f"checks.json checks[{index}]")
        name = _non_empty_str(obj.get("name"), f"checks.json checks[{index}].name")
        stack = _non_empty_str(obj.get("stack"), f"checks.json checks[{index}].stack")
        argv = tuple(
            _non_empty_str(token, f"checks.json checks[{index}].argv[]")
            for token in _sequence(obj.get("argv"), f"checks.json checks[{index}].argv")
        )
        if not argv:
            raise InvalidProfile(f"checks.json checks[{index}].argv must not be empty")
        cwd = _relative(obj.get("cwd", "."), f"checks.json checks[{index}].cwd")
        required = bool(obj.get("required", False))
        checks[name] = CheckCommand(name=name, stack=stack, argv=argv, cwd=cwd, required=required)
    if not checks:
        raise InvalidProfile(
            "a generated project profile needs at least one check in checks.json to build a "
            "runnable task route"
        )
    return checks


def _json_object(text: str, field: str) -> Mapping[str, Any]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidProfile(f"{field} is not valid JSON: {exc}") from None
    return _mapping(doc, field)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidProfile(f"{field} must be an object")
    return value


def _sequence(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise InvalidProfile(f"{field} must be a list")
    return value


def _non_empty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidProfile(f"{field} must be a non-empty string")
    return value


def _relative(value: object, field: str) -> RelativePath:
    """A repository-relative path — an absolute, drive-qualified, ``~`` or ``..`` value is an
    unsafe anchor and is rejected here (via :class:`RelativePath`, a ``DomainError``)."""
    return RelativePath.parse(_non_empty_str(value, field))


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "InvalidProfile",
    "UnknownRoute",
    "UnknownCheck",
    "UnknownStack",
    "CheckCommand",
    "StackBinding",
    "RoutePolicy",
    "CompiledProfile",
]
