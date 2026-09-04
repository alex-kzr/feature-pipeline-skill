"""The typed compiled profile — profiles own task-type routes (CP-01).

``pipeline_core`` reads the generated project profile
(``tools/feature-pipeline/config/pipeline.profile.json`` + its sibling ``checks.json``)
through :func:`pipeline_core.project_profile.load_runnable_profile`, which *synthesises* a
native :class:`schemas.Profile`. CP-01 instead parses the same document into small typed value
objects the plan compiler consumes directly, so route resolution lives in one place and every
rejection is a :class:`~feature_pipeline.domain.errors.DomainError` raised before any run
artifact exists.

The route synthesis mirrors ``project_profile._from_project_profile`` exactly: every declared
task type is routed to its ``working_root``; the neutral ``executor`` subagent runs every
route; each route lists *all* declared check names; storage is the single ``run_state`` key
bound to ``run_state_path``. Parity against the shipped
:func:`pipeline_core.profiles.resolve_route` is pinned by
``tests/test_execution_plan_compiler.py``.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.paths import RelativePath

#: The only project-profile ``schema_version`` this core understands.
SUPPORTED_SCHEMA_VERSION = 1

#: Keys a ``schema_version`` project profile must carry.
_REQUIRED_KEYS = ("project", "anchors", "task_routing", "run_state_path", "roles")

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


@dataclass(frozen=True)
class CheckCommand:
    """One repository-declared verification command."""

    name: str
    stack: str
    argv: tuple[str, ...]
    cwd: RelativePath


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
        stacks = sorted({check.stack for check in checks.values()})
        check_names = tuple(sorted(checks))

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
            if task_type in routes:
                raise InvalidProfile(f"task_routing has a duplicate task type: {task_type!r}")
            routes[task_type] = RoutePolicy(
                task_type=task_type,
                working_root=working_root,
                stack=stacks[0],
                subagents=(_EXECUTOR,),
                check_names=check_names,
                storage_key=_STORAGE_KEY,
            )

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

        return cls(
            project=project,
            agents_root=agents_root,
            core_root=core_root,
            run_state_path=run_state_path,
            routes=routes,
            role_grants=role_grants,
            checks=checks,
            storage={_STORAGE_KEY: run_state_path},
        )


# --- parsing helpers -----------------------------------------------------------------------


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
        checks[name] = CheckCommand(name=name, stack=stack, argv=argv, cwd=cwd)
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
    "CheckCommand",
    "RoutePolicy",
    "CompiledProfile",
]
