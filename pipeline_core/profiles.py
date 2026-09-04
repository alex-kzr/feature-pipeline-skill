"""Explicit-anchor profile resolution and closed-registry routing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from schemas import Profile, SchemaError, TaskRoute


@dataclass(frozen=True)
class Anchors:
    """Filesystem roots supplied by the launcher, never inferred by the core."""

    project_root: Path
    agents_root: Path
    core_root: Path


@dataclass(frozen=True)
class ResolvedPaths:
    project: Path
    agents: Path
    core: Path


@dataclass(frozen=True)
class ResolvedRoute:
    """A task route resolved entirely through the profile's closed registry."""

    task_type: str
    route: TaskRoute
    root: Path
    storage: Path
    checks: tuple[tuple[str, ...], ...]


def resolve_paths(profile: Profile, anchors: Anchors) -> ResolvedPaths:
    """Resolve each logical path below its corresponding explicit anchor."""
    return ResolvedPaths(
        project=_under_anchor(anchors.project_root, profile.logical_paths.project, "project"),
        agents=_under_anchor(anchors.agents_root, profile.logical_paths.agents, "agents"),
        core=_under_anchor(anchors.core_root, profile.logical_paths.core, "core"),
    )


def resolve_route(profile: Profile, task_type: str, anchors: Anchors) -> ResolvedRoute:
    """Return a declared route or fail closed before callers create state."""
    if profile.registry is None:
        raise SchemaError("profile registry is required before dispatch")
    try:
        route = profile.registry.task_types[task_type]
    except KeyError:
        raise SchemaError(f"unregistered task type: {task_type}") from None
    paths = resolve_paths(profile, anchors)
    return ResolvedRoute(
        task_type=task_type,
        route=route,
        root=_under_anchor(paths.project, profile.registry.roots[route.root], "route root"),
        storage=_under_anchor(paths.project, profile.registry.storage[route.storage], "storage"),
        checks=tuple(profile.registry.checks[name] for name in route.checks),
    )


def _under_anchor(anchor: Path, logical_path: str, field: str) -> Path:
    root = Path(os.path.abspath(anchor))
    resolved_root = root.resolve()
    candidate = root / logical_path
    try:
        candidate.resolve().relative_to(resolved_root)
    except ValueError:
        raise SchemaError(f"{field} escapes its explicit anchor") from None
    return candidate
