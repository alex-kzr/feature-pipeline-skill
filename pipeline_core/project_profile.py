"""One versioned project-profile boundary for the portable core runner.

The portable core's internal model is :class:`schemas.Profile` (``version`` /
``logical_paths`` / ``role_grants`` / ``stages`` / ``registry``). A repository that
is configured by the ``feature-pipeline-project-setup`` skill instead carries a
*project profile* — ``tools/feature-pipeline/config/pipeline.profile.json`` — whose
shape is the setup contract's ``schema_version`` document: explicit ``anchors``,
``task_routing``, ``roles``, ``run_state_path``, and a sibling ``checks.json``.

:func:`load_runnable_profile` is the single loader the runner CLI uses. It accepts
exactly two inputs and nothing in between:

* a **native core profile** (a top-level ``version``) — passed straight to
  :meth:`schemas.Profile.from_data`, unchanged;
* a **generated project profile** (a top-level ``schema_version``) — converted here,
  once, to an equivalent :class:`schemas.Profile` so the generated file is directly
  runnable.

Every other file is rejected fail-closed: a document carrying both discriminators,
neither, an unknown ``schema_version``, or a ``schema_version`` document missing a
required generated key (a half-converted file) raises :class:`schemas.SchemaError`
before any run artifact can be created. This mirrors the translation the setup
skill's smoke harness documents, but as the real, tested core path.

Standard library only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from schemas import Profile, SchemaError

#: The only project-profile ``schema_version`` this core understands. A different
#: value is an unknown schema and is refused, never best-effort parsed.
SUPPORTED_PROJECT_PROFILE_SCHEMA = 1

#: Keys a ``schema_version`` project profile must carry. A file that names the
#: schema but omits one of these is a partially converted file, not a profile.
_REQUIRED_PROJECT_KEYS = ("project", "anchors", "task_routing", "run_state_path", "roles")

#: Keys that only ever appear in a native core profile. Their presence alongside
#: ``schema_version`` means the file is ambiguous — two schemas at once.
_NATIVE_ONLY_KEYS = ("version", "logical_paths", "stages", "registry")

#: The neutral executor subagent the synthesized registry routes every task type
#: through; the real per-task commands are the project's declared checks.
_EXECUTOR = "executor"


def load_runnable_profile(profile_path: Path) -> Profile:
    """Load ``profile_path`` as a runnable :class:`schemas.Profile`.

    Raises :class:`schemas.SchemaError` for an ambiguous, unknown-version, or
    half-converted document. Propagates :class:`FileNotFoundError` and
    :class:`json.JSONDecodeError` for the caller to map to its own exit code.
    """
    raw = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SchemaError("profile must be an object")

    has_native = "version" in raw
    has_project = "schema_version" in raw
    if has_native and has_project:
        raise SchemaError(
            "profile carries both 'version' and 'schema_version' — it must be exactly "
            "one of a native core profile or a generated project profile"
        )
    if has_project:
        checks_doc = _load_sibling_checks(Path(profile_path).parent / "checks.json")
        return _from_project_profile(raw, checks_doc)
    if has_native:
        return Profile.from_data(raw)
    raise SchemaError("profile has neither a 'version' nor a 'schema_version'")


def _load_sibling_checks(checks_path: Path) -> Mapping[str, Any] | None:
    """Read the generated ``checks.json`` beside the profile, if it exists."""
    if not checks_path.is_file():
        return None
    try:
        doc = json.loads(checks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"checks.json beside the profile is not valid JSON: {exc}") from None
    if not isinstance(doc, Mapping):
        raise SchemaError("checks.json must be an object")
    if doc.get("schema_version") != SUPPORTED_PROJECT_PROFILE_SCHEMA:
        raise SchemaError(
            f"checks.json schema_version must be {SUPPORTED_PROJECT_PROFILE_SCHEMA}, "
            f"got {doc.get('schema_version')!r}"
        )
    return doc


def _from_project_profile(
    raw: Mapping[str, Any], checks_doc: Mapping[str, Any] | None
) -> Profile:
    """Convert a generated ``schema_version`` project profile to a core profile."""
    version = raw.get("schema_version")
    if version != SUPPORTED_PROJECT_PROFILE_SCHEMA:
        raise SchemaError(
            f"unknown project profile schema_version: {version!r} "
            f"(this core understands {SUPPORTED_PROJECT_PROFILE_SCHEMA})"
        )
    for key in _REQUIRED_PROJECT_KEYS:
        if key not in raw:
            raise SchemaError(f"project profile is missing required key {key!r}")
    for key in _NATIVE_ONLY_KEYS:
        if key in raw:
            raise SchemaError(
                f"project profile carries native-core key {key!r}; a generated profile "
                "declares anchors, task_routing, roles and run_state_path only"
            )

    name = _non_empty_str(raw.get("project"), "project")
    anchors = _mapping(raw.get("anchors"), "anchors")
    agents_root = _non_empty_str(anchors.get("agents_root"), "anchors.agents_root")
    core_root = _non_empty_str(anchors.get("core_root"), "anchors.core_root")
    run_state_path = _non_empty_str(raw.get("run_state_path"), "run_state_path")

    routing = _sequence(raw.get("task_routing"), "task_routing")
    if not routing:
        raise SchemaError("task_routing must not be empty")
    roles = _sequence(raw.get("roles"), "roles")
    if not roles:
        raise SchemaError("roles must not be empty")

    check_entries = _sequence((checks_doc or {}).get("checks", []), "checks.json checks")
    checks_registry: dict[str, list[str]] = {}
    stack_names: list[str] = []
    for index, entry in enumerate(check_entries):
        obj = _mapping(entry, f"checks.json checks[{index}]")
        check_name = _non_empty_str(obj.get("name"), f"checks.json checks[{index}].name")
        stack = _non_empty_str(obj.get("stack"), f"checks.json checks[{index}].stack")
        argv = [_non_empty_str(tok, f"checks.json checks[{index}].argv[]")
                for tok in _sequence(obj.get("argv"), f"checks.json checks[{index}].argv")]
        if not argv:
            raise SchemaError(f"checks.json checks[{index}].argv must not be empty")
        checks_registry[check_name] = argv
        if stack not in stack_names:
            stack_names.append(stack)
    if not checks_registry:
        raise SchemaError(
            "a generated project profile needs at least one check in checks.json to "
            "build a runnable task route"
        )
    stack_names.sort()
    check_names = sorted(checks_registry)

    roots_registry: dict[str, str] = {}
    task_types: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(routing):
        obj = _mapping(entry, f"task_routing[{index}]")
        task_type = _non_empty_str(obj.get("task_type"), f"task_routing[{index}].task_type")
        working_root = _non_empty_str(
            obj.get("working_root"), f"task_routing[{index}].working_root")
        if task_type in roots_registry:
            raise SchemaError(f"task_routing has a duplicate task type: {task_type!r}")
        roots_registry[task_type] = working_root
        task_types[task_type] = {
            "stack": stack_names[0],
            "subagents": [_EXECUTOR],
            "root": task_type,
            "checks": check_names,
            "storage": "run_state",
        }

    role_grants: dict[str, list[str]] = {}
    for index, entry in enumerate(roles):
        obj = _mapping(entry, f"roles[{index}]")
        role = _non_empty_str(obj.get("role"), f"roles[{index}].role")
        grants = [_non_empty_str(g, f"roles[{index}].min_grants[]")
                  for g in _sequence(obj.get("min_grants"), f"roles[{index}].min_grants")]
        if not grants:
            raise SchemaError(f"roles[{index}].min_grants must not be empty")
        role_grants[role] = grants

    core_profile = {
        "version": 1,
        "name": name,
        "logical_paths": {"project": ".", "agents": agents_root, "core": core_root},
        "role_grants": role_grants,
        # The generated schema has no stage concept; a single non-verification
        # placeholder keeps the core's "stages must not be empty" contract while
        # the meaningful per-task commands stay the project's declared checks.
        "stages": [{"name": "implement", "subagents": [_EXECUTOR], "argv": ["plan-only"]}],
        "registry": {
            "task_types": task_types,
            "stacks": {stack: {"runtime": stack} for stack in stack_names},
            "subagents": {_EXECUTOR: {"grant": _EXECUTOR}},
            "roots": roots_registry,
            "checks": checks_registry,
            "storage": {"run_state": run_state_path},
        },
    }
    return Profile.from_data(core_profile)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{field} must be an object")
    return value


def _sequence(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise SchemaError(f"{field} must be a list")
    return value


def _non_empty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{field} must be a non-empty string")
    return value
