"""Load and validate the project's tool-integration contract (``integrations.json``).

The portable core carries no project identity. A repository that wires Graphify into its
pipeline declares the contract in ``tools/feature-pipeline/config/integrations.json`` and the
core consumes it through :func:`load_graphify_integration`. The returned
:class:`GraphifyIntegration` is the *only* Graphify knowledge the post-task lifecycle
(:mod:`pipeline_core.post_task`) ever has: which wrapper directory to invoke, which outputs to
require, which diff policy to enforce, and which ``graphify … install`` argvs to refuse.

Loading is fail-closed: an unknown ``schema_version``, a missing ``graphify`` object, an
unsafe path, an unknown ``diff_policy``, or a ``forbidden`` entry that is not actually a
``graphify … install`` command all raise :class:`schemas.SchemaError` before any stage runs.

Standard library only.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from schemas import SchemaError, validate_relative_path
from schemas.contracts import DIFF_POLICIES

#: The only ``schema_version`` this loader understands. A different value is an unknown schema
#: and is refused, never best-effort parsed.
SUPPORTED_INTEGRATIONS_SCHEMA = 1

#: ``graphify <agent> install`` rewrites ``CLAUDE.md`` / ``AGENTS.md`` and the shared
#: ``.agents`` tree. The documented forbidden agents; detection below is deliberately broader
#: (any ``graphify … install``), because the safe direction for the ban is over-refusal.
INSTALLER_AGENTS = frozenset({"claude", "codex", "antigravity"})


def _basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def is_forbidden_installer_argv(argv: Sequence[str]) -> bool:
    """True when ``argv`` invokes ``graphify`` with an ``install`` subcommand.

    Matches ``graphify install``, ``graphify claude install``, and
    ``/opt/bin/graphify codex install --force`` alike; does not match ``graphify update .``
    or a wrapper such as ``bash tools/graphify/scripts/build.sh``.
    """
    tokens = [str(token).strip() for token in argv if str(token).strip()]
    for index, token in enumerate(tokens):
        name = _basename(token)
        if name in ("graphify", "graphify.exe"):
            rest = [t.lower() for t in tokens[index + 1:] if not t.startswith("-")]
            if "install" in rest:
                return True
    return False


def is_forbidden_installer_command(command: str) -> bool:
    """:func:`is_forbidden_installer_argv` for a single shell-free command string."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return is_forbidden_installer_argv(argv)


@dataclass(frozen=True)
class GraphifyIntegration:
    """The project's declared Graphify stage contract."""

    stage: str
    executor: str
    workspace: str
    scan_root: str
    wrapper_dir: str
    expected_outputs: tuple[str, ...]
    diff_policy: str
    forbidden: tuple[str, ...]

    def forbids(self, argv: Sequence[str]) -> bool:
        """True when ``argv`` is a structurally-detected installer command or exactly matches
        a configured ``forbidden`` entry."""
        if is_forbidden_installer_argv(argv):
            return True
        normalized = [str(token).strip().lower() for token in argv if str(token).strip()]
        for entry in self.forbidden:
            try:
                if [token.lower() for token in shlex.split(entry)] == normalized:
                    return True
            except ValueError:
                continue
        return False

    @classmethod
    def from_data(cls, data: object) -> "GraphifyIntegration":
        root = _mapping(data, "integrations.json")
        if root.get("schema_version") != SUPPORTED_INTEGRATIONS_SCHEMA:
            raise SchemaError(
                f"integrations.json schema_version must be {SUPPORTED_INTEGRATIONS_SCHEMA}, "
                f"got {root.get('schema_version')!r}"
            )
        value = _mapping(root.get("graphify"), "integrations.graphify")
        stage = _text(value.get("stage"), "integrations.graphify.stage")
        executor = _text(value.get("executor"), "integrations.graphify.executor")
        workspace = validate_relative_path(
            value.get("workspace"), "integrations.graphify.workspace")
        scan_root = _text(value.get("scan_root"), "integrations.graphify.scan_root")
        if scan_root != ".":
            scan_root = validate_relative_path(scan_root, "integrations.graphify.scan_root")
        wrapper_dir = validate_relative_path(
            value.get("wrapper_dir"), "integrations.graphify.wrapper_dir")

        outputs_raw = value.get("expected_outputs")
        if not isinstance(outputs_raw, list) or not outputs_raw:
            raise SchemaError(
                "integrations.graphify.expected_outputs must be a non-empty list")
        expected = tuple(
            validate_relative_path(item, "integrations.graphify.expected_outputs[]")
            for item in outputs_raw
        )

        diff_policy = value.get("diff_policy", "ignore")
        if diff_policy not in DIFF_POLICIES:
            raise SchemaError(
                f"integrations.graphify.diff_policy must be one of {sorted(DIFF_POLICIES)}")

        forbidden_raw = value.get("forbidden", [])
        if not isinstance(forbidden_raw, list):
            raise SchemaError("integrations.graphify.forbidden must be a list")
        forbidden = tuple(
            _text(item, "integrations.graphify.forbidden[]") for item in forbidden_raw)
        for entry in forbidden:
            if not is_forbidden_installer_command(entry):
                raise SchemaError(
                    f"integrations.graphify.forbidden entry {entry!r} is not a "
                    "'graphify … install' command"
                )

        return cls(stage, executor, workspace, scan_root, wrapper_dir, expected,
                   diff_policy, forbidden)


def load_graphify_integration(path: str | Path) -> GraphifyIntegration:
    """Read and validate ``integrations.json`` into a :class:`GraphifyIntegration`.

    Propagates :class:`FileNotFoundError` and :class:`json.JSONDecodeError` for the caller to
    map to its own exit code; raises :class:`schemas.SchemaError` for a contract violation.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return GraphifyIntegration.from_data(raw)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{field} must be a non-empty string")
    return value
