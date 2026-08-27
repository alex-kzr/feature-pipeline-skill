"""Portable durable run state, transitions, resume validation, and leases."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import ArtifactReadError, read_json, write_json_atomic

SCHEMA_VERSION = 1
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
    "repairing": {"implemented", "blocked"}, "verified": set(),
    "blocked": {"ready", "implemented"},
}


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


@dataclass
class TaskRecord:
    """A portable task's durable identity and state."""
    id: str
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    attempts: int = 0
    blocker: str | None = None


@dataclass
class Run:
    """A JSON-persisted run with explicit repository anchors."""
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

    @classmethod
    def create(cls, feature: str, prompt_path: str | Path, plan_path: str | Path | None, run_dir: str | Path, repo_root: str | Path) -> "Run":
        root = Path(repo_root).resolve()
        return cls(feature, repo_relative(prompt_path, root), repo_relative(plan_path, root) if plan_path else None, Path(run_dir), root, f"{_now().replace(':', '-')}-{feature}")

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
        return entry

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "feature": self.feature, "prompt_path": self.prompt_path, "plan_path": self.plan_path, "run_id": self.run_id, "status": self.status, "tasks": [asdict(task) for task in self.tasks.values()], "history": self.history, "commands": self.commands}

    def save(self) -> Path:
        return write_json_atomic(self.run_dir / "run.json", self.to_dict(), repo_root=self.repo_root)

    @classmethod
    def load(cls, run_dir: str | Path, repo_root: str | Path) -> "Run":
        directory = Path(run_dir)
        try:
            data = read_json(directory / "run.json")
        except ArtifactReadError as exc:
            raise StateError(str(exc), "unreadable-state") from None
        if data.get("schema_version") != SCHEMA_VERSION:
            raise StateError("unsupported run-state schema version", "unknown-schema-version")
        tasks = {entry["id"]: TaskRecord(**entry) for entry in data.get("tasks", [])}
        return cls(data["feature"], data["prompt_path"], data.get("plan_path"), directory, Path(repo_root).resolve(), data["run_id"], data.get("status", "pending"), tasks, data.get("history", []), data.get("commands", []))

    def resume(self, *, feature: str, prompt_path: str | Path, plan_path: str | Path | None) -> None:
        if feature != self.feature or repo_relative(prompt_path, self.repo_root) != self.prompt_path or (repo_relative(plan_path, self.repo_root) if plan_path else None) != self.plan_path:
            raise ResumeError("resume request does not match persisted run identity", "resume-mismatch")
