"""UGA-03 — the explicit-root gate runner behind ``ci/run.py``.

The source checkout is an explicit input. Nothing here reads the process current
directory, searches parent/child directories, or infers whether the core is
checked out standalone or nested in an umbrella repository. Every path — the gate
manifest, a gate's required paths, and each command's working directory — is
resolved below the caller-supplied ``--source-root`` and nowhere else.

Gate commands run through :class:`SubprocessRunner` (plain ``subprocess`` argv,
an explicit ``cwd``, never a shell). Unit tests inject a fake runner instead.

This module is CI tooling; the installable runtime never imports it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ci import contract

#: The gate manifest, relative to an explicit source root.
MANIFEST_RELPATH = "ci/gates.toml"

#: Stable anchor for relative ``--source-root`` inputs, independent of the caller's cwd.
DRIVER_ROOT = Path(__file__).resolve().parents[1]


class DriverError(RuntimeError):
    """A single-cause driver failure raised before any gate subprocess starts."""


@dataclass(frozen=True)
class CommandResult:
    """The minimal result a command runner must return."""

    returncode: int
    stdout: str = ""


class SubprocessRunner:
    """Default runner: ``subprocess`` argv, explicit ``cwd``, ``shell=False``."""

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, capture: bool = False
    ) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=capture,
            text=True,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "" if capture else "",
        )


def resolve_source_root(source_root: str | None) -> Path:
    """Validate and absolutize an explicit source root.

    Rejects — with :class:`DriverError` — a missing, empty, nonexistent,
    non-directory, or contract-less root instead of searching for one.
    """

    if not source_root:
        raise DriverError("--source-root is required")
    supplied_root = Path(source_root)
    root = supplied_root if supplied_root.is_absolute() else DRIVER_ROOT / supplied_root
    if not root.exists():
        raise DriverError(f"--source-root does not exist: {source_root}")
    if not root.is_dir():
        raise DriverError(f"--source-root is not a directory: {source_root}")
    if not (root / MANIFEST_RELPATH).is_file():
        raise DriverError(
            f"--source-root has no CI contract ({MANIFEST_RELPATH}): {source_root}"
        )
    return root.resolve()


def load_contract(root: Path) -> contract.Contract:
    """Load and fail-closed validate the gate manifest below ``root``."""

    return contract.load(root / MANIFEST_RELPATH)


def read_source_sha(root: Path, runner) -> str:
    """``git rev-parse HEAD`` in ``root`` — the checked-out source SHA."""

    result = runner(["git", "rev-parse", "HEAD"], cwd=root, capture=True)
    if result.returncode != 0:
        raise DriverError(f"cannot read source SHA (git rev-parse HEAD) in {root}")
    sha = result.stdout.strip()
    if not sha:
        raise DriverError(f"empty source SHA from git rev-parse HEAD in {root}")
    return sha


def check_required_paths(root: Path, gate: contract.Gate) -> None:
    """Fail if any of a gate's manifest ``required_paths`` is absent below ``root``."""

    missing = [rel for rel in gate.required_paths if not (root / rel).exists()]
    if missing:
        raise DriverError(
            f"gate {gate.id!r} required path(s) missing under {root}: "
            f"{', '.join(missing)}"
        )


def _gate_payload(gate: contract.Gate) -> dict[str, object]:
    return {
        "id": gate.id,
        "group": gate.group,
        "description": gate.description,
        "commands": [list(command.argv) for command in gate.commands],
        "required_paths": list(gate.required_paths),
        "evidence_paths": list(gate.evidence_paths),
        "os": list(gate.os),
        "python": list(gate.python),
    }


def list_payload(
    loaded: contract.Contract, group: str | None = None
) -> Mapping[str, object]:
    """The JSON-ready mapping of one workflow group (or all) to its gates."""

    names = (group,) if group is not None else contract.WORKFLOW_GROUPS
    return {name: [_gate_payload(g) for g in loaded.group(name)] for name in names}


def list_json(loaded: contract.Contract, group: str | None = None) -> str:
    """Deterministic, byte-stable JSON for a named workflow group (AC-5)."""

    return json.dumps(list_payload(loaded, group), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class Preflight:
    """The single-cause diagnostics printed before a gate's first command."""

    source_sha: str
    source_root: Path
    gate_id: str
    required_paths: tuple[str, ...]
    workflow_repo: str | None = None
    workflow_sha: str | None = None
    source_repo: str | None = None

    def render(self) -> str:
        lines = [f"workflow repository: {self.workflow_repo or '(unset)'}"]
        if self.workflow_sha:
            lines.append(f"workflow SHA: {self.workflow_sha}")
        lines.append(f"source repository: {self.source_repo or '(unset)'}")
        lines.append(f"source SHA: {self.source_sha}")
        lines.append(f"resolved source root: {self.source_root}")
        lines.append(f"gate: {self.gate_id}")
        lines.append(
            f"required paths: {', '.join(self.required_paths) or '(none)'}"
        )
        return "\n".join(lines)


def run_gate(
    *,
    gate_id: str,
    source_root: str | None,
    runner,
    out,
    expected_source_sha: str | None = None,
    workflow_repo: str | None = None,
    workflow_sha: str | None = None,
    source_repo: str | None = None,
) -> int:
    """Resolve, preflight, and execute one gate below an explicit source root.

    Returns the first command's nonzero exit code (and runs no later command or
    evidence producer), or ``0`` when every command succeeds. A source-SHA
    mismatch or a missing required path raises :class:`DriverError` before any
    gate subprocess starts.
    """

    root = resolve_source_root(source_root)
    loaded = load_contract(root)
    gate = loaded.gate(gate_id)

    source_sha = read_source_sha(root, runner)
    if expected_source_sha is not None and expected_source_sha != source_sha:
        raise DriverError(
            f"source SHA mismatch: expected {expected_source_sha}, "
            f"checked out {source_sha}"
        )
    check_required_paths(root, gate)

    preflight = Preflight(
        source_sha=source_sha,
        source_root=root,
        gate_id=gate.id,
        required_paths=gate.required_paths,
        workflow_repo=workflow_repo,
        workflow_sha=workflow_sha,
        source_repo=source_repo,
    )
    print(preflight.render(), file=out)

    total = len(gate.commands)
    for position, command in enumerate(gate.commands):
        print(f"$ {' '.join(command.argv)}", file=out)
        result = runner(list(command.argv), cwd=root, capture=False)
        if result.returncode != 0:
            remaining = total - position - 1
            print(
                f"gate {gate.id!r} command {position + 1}/{total} failed "
                f"(exit {result.returncode}); {remaining} later command(s) and "
                f"evidence producers were not run",
                file=out,
            )
            return result.returncode
    return 0
