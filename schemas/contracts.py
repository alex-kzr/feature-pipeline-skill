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
    name: str
    subagents: tuple[str, ...]
    argv: tuple[str, ...]

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
        return cls(name, subagents, argv)


@dataclass(frozen=True)
class Profile:
    version: int
    name: str
    logical_paths: LogicalPaths
    role_grants: Mapping[str, frozenset[str]]
    stages: tuple[ToolStage, ...]

    @classmethod
    def from_data(cls, data: object) -> "Profile":
        value = _mapping(data, "profile")
        _version(value)
        paths = LogicalPaths.from_data(value.get("logical_paths"))
        grants = _role_grants(value.get("role_grants"))
        stages = tuple(ToolStage.from_data(item) for item in _sequence(value.get("stages"), "stages"))
        if not stages:
            raise SchemaError("stages must not be empty")
        if any(stage.name == "verification" for stage in stages):
            for stage in stages:
                if stage.name == "verification":
                    _verifier_pair(stage.subagents, "verification.subagents")
        return cls(SCHEMA_VERSION, _string(value.get("name"), "name"), paths, grants, stages)


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
