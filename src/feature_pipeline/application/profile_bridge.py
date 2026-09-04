"""Bridge a native ``schemas.Profile`` into the typed compiler inputs (CP-02).

CP-01's :func:`feature_pipeline.application.compile_plan.compile_run_plan` consumes a
:class:`feature_pipeline.inputs.profile.CompiledProfile`, which parses the *generated*
``schema_version`` project profile and rejects a native ``version`` + ``registry`` document.
But ``pipeline_core.runner_cli`` — and every compatibility golden — still feeds the native
:class:`feature_pipeline.contracts.Profile` (a top-level ``version`` with a closed
``registry``). This module is the second entrance to the same typed value objects: it maps a
resolved native profile's per-route data (stack, working root, subagents, per-route checks +
argv, storage) into a :class:`CompiledProfile` the one compiler can consume, so route
resolution stays in exactly one place.

It reads the profile's closed registry directly — the same lookups
``pipeline_core.profiles.resolve_route`` performs — without touching the filesystem: the
renderer needs the registry's *logical* relative strings (they redact to ``<project_root>/…``
tokens), not resolved host paths.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable

from feature_pipeline.contracts import TASK_TYPES, Profile
from feature_pipeline.domain.paths import RelativePath
from feature_pipeline.inputs.profile import (
    CheckCommand,
    CompiledProfile,
    RoutePolicy,
)

#: Task types the core recognises but deliberately leaves unrouted — a ``design`` task needs
#: an explicit executor and never gets a default route (mirrors
#: ``pipeline_core.runner_cli.UNROUTED_TASK_TYPES``).
UNROUTED_TASK_TYPES = frozenset({"design"})

#: The reasons a selected task type does not resolve to a route, byte-identical to the
#: strings ``pipeline_core.runner_cli._route`` returns today.
UNKNOWN_TASK_TYPE = "unknown-task-type"
UNRESOLVED_EXECUTOR = "unresolved-executor"

__all__ = [
    "UNROUTED_TASK_TYPES",
    "UNKNOWN_TASK_TYPE",
    "UNRESOLVED_EXECUTOR",
    "route_reasons",
    "compiled_profile_from_core",
]


def route_reasons(profile: Profile, task_types: Iterable[str]) -> dict[str, str]:
    """Classify each task type that does **not** resolve to a route.

    Returns ``{task_type: reason}`` for the unroutable ones only — an empty mapping means
    every type routes and :func:`compiled_profile_from_core` can build a full profile. The
    reason strings match ``runner_cli._route`` exactly so the CLI keeps its recorded
    fail-closed output.
    """
    registry = profile.registry
    reasons: dict[str, str] = {}
    for task_type in dict.fromkeys(task_types):
        if task_type not in TASK_TYPES:
            reasons[task_type] = UNKNOWN_TASK_TYPE
        elif task_type in UNROUTED_TASK_TYPES:
            reasons[task_type] = UNRESOLVED_EXECUTOR
        elif registry is None or task_type not in registry.task_types:
            reasons[task_type] = UNRESOLVED_EXECUTOR
    return reasons


def compiled_profile_from_core(profile: Profile) -> CompiledProfile:
    """Map a native ``schemas.Profile`` (with a closed registry) to a :class:`CompiledProfile`.

    Every declared task type becomes a :class:`RoutePolicy` carrying that route's own stack,
    working root, subagents and check names; every registry check becomes a
    :class:`CheckCommand` with its real argv. The caller is responsible for having already
    rejected an unroutable selection via :func:`route_reasons`.
    """
    registry = profile.registry
    if registry is None:
        raise ValueError("profile has no registry; call route_reasons() first")

    checks: dict[str, CheckCommand] = {
        name: CheckCommand(
            name=name,
            stack="",
            argv=tuple(argv),
            cwd=RelativePath.parse("."),
        )
        for name, argv in registry.checks.items()
    }

    routes: dict[str, RoutePolicy] = {}
    for task_type, route in registry.task_types.items():
        routes[task_type] = RoutePolicy(
            task_type=task_type,
            working_root=RelativePath.parse(registry.roots[route.root]),
            stack=route.stack,
            subagents=tuple(route.subagents),
            check_names=tuple(route.checks),
            storage_key=route.storage,
        )

    storage = {
        key: RelativePath.parse(value) for key, value in registry.storage.items()
    }
    run_state_path = next(iter(storage.values()), RelativePath.parse("."))

    return CompiledProfile(
        project=profile.name,
        agents_root=RelativePath.parse(profile.logical_paths.agents),
        core_root=RelativePath.parse(profile.logical_paths.core),
        run_state_path=run_state_path,
        routes=routes,
        role_grants={
            role: tuple(sorted(grants)) for role, grants in profile.role_grants.items()
        },
        checks=checks,
        storage=storage,
    )
