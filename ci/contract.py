"""UGA-02 — loader and fail-closed validator for ``ci/gates.toml``.

``gates.toml`` is the single executable source of truth for every CI gate and the
supported OS/Python matrix for each. This module parses it with the standard
library only (``tomllib``) and rejects — with a deterministic
:class:`ContractError` — any manifest that:

* declares an unknown ``schema_version``;
* omits, duplicates, or invents a stable gate ID;
* gives a gate an empty command list or an empty ``argv``;
* puts a shell operator (``&&``, ``|``, ``;``, ``>`` …) inside an ``argv`` token;
* names a ``required_paths`` / ``evidence_paths`` entry that is not a plain
  repository-relative path;
* names an unsupported OS runner or an invalid Python version;
* carries an unknown key.

The installable runtime never imports this module.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: The only manifest schema this loader understands.
SCHEMA_VERSION = 1

#: The stable, exhaustive set of gate IDs (UGA-02 Requirements). A manifest must
#: define exactly these — no more, no fewer.
GATE_IDS: tuple[str, ...] = (
    "lint",
    "types",
    "coverage",
    "platform",
    "fault-injection",
    "performance",
    "installed-package",
    "documentation",
)

#: Which repository workflow group runs a gate.
WORKFLOW_GROUPS: tuple[str, ...] = ("core", "consumer")

#: GitHub-hosted runner labels a gate matrix may reference.
SUPPORTED_OS: tuple[str, ...] = ("ubuntu-latest", "windows-latest", "macos-latest")

_PYTHON_RE = re.compile(r"\A3\.(?:[1-9][0-9]?)\Z")

#: Substrings that make an ``argv`` token depend on a shell to be interpreted.
_SHELL_OPERATORS: tuple[str, ...] = (
    "&&", "||", "|", ";", "&", "`", "$(", "${", "$((",
    ">>", ">", "<<", "<", "\n", "\r",
)

_TOP_LEVEL_KEYS = frozenset({"schema_version", "gates"})
_GATE_KEYS = frozenset(
    {
        "id",
        "group",
        "description",
        "commands",
        "required_paths",
        "evidence_paths",
        "os",
        "python",
    }
)

#: The committed manifest, resolved next to this module.
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "gates.toml"


class ContractError(ValueError):
    """A single-cause, deterministic gate-manifest validation failure."""


@dataclass(frozen=True)
class Command:
    """One gate step: an ordered argv array run without a shell."""

    argv: tuple[str, ...]


@dataclass(frozen=True)
class Gate:
    """A single CI gate as declared in ``gates.toml``."""

    id: str
    group: str
    commands: tuple[Command, ...]
    required_paths: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    os: tuple[str, ...] = ()
    python: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Contract:
    """The validated manifest: schema version plus ordered gates."""

    schema_version: int
    gates: Mapping[str, Gate]

    def ids(self) -> tuple[str, ...]:
        """Gate IDs in manifest order."""

        return tuple(self.gates)

    def gate(self, gate_id: str) -> Gate:
        """Return one gate or raise :class:`ContractError` for an unknown ID."""

        try:
            return self.gates[gate_id]
        except KeyError:
            raise ContractError(f"unknown gate id: {gate_id!r}") from None

    def group(self, name: str) -> tuple[Gate, ...]:
        """All gates in a workflow group, in manifest order."""

        if name not in WORKFLOW_GROUPS:
            raise ContractError(f"unknown workflow group: {name!r}")
        return tuple(gate for gate in self.gates.values() if gate.group == name)


def load(path: str | Path | None = None) -> Contract:
    """Parse and validate a gate manifest (the committed one by default)."""

    manifest = Path(path) if path is not None else DEFAULT_MANIFEST
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read gate manifest: {manifest}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"gate manifest is not valid TOML: {exc}") from exc
    return _build(data)


def _build(data: Mapping[str, object]) -> Contract:
    if "schema_version" not in data:
        raise ContractError("gate manifest is missing schema_version")
    version = data["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ContractError(
            f"unsupported schema_version: {version!r}; expected {SCHEMA_VERSION}"
        )

    unknown_top = set(data) - _TOP_LEVEL_KEYS
    if unknown_top:
        listed = ", ".join(sorted(str(key) for key in unknown_top))
        raise ContractError(f"gate manifest has unknown top-level key(s): {listed}")

    entries = data.get("gates")
    if not isinstance(entries, list) or not entries:
        raise ContractError("gate manifest must define a non-empty [[gates]] array")

    gates: dict[str, Gate] = {}
    for index, entry in enumerate(entries):
        gate = _gate(entry, index)
        if gate.id in gates:
            raise ContractError(f"duplicate gate id: {gate.id!r}")
        gates[gate.id] = gate

    missing = [gate_id for gate_id in GATE_IDS if gate_id not in gates]
    if missing:
        raise ContractError(
            f"gate manifest is missing required gate id(s): {', '.join(missing)}"
        )

    return Contract(schema_version=SCHEMA_VERSION, gates=gates)


def _gate(entry: object, index: int) -> Gate:
    if not isinstance(entry, dict):
        raise ContractError(f"[[gates]] entry #{index} is not a table")

    unknown = set(entry) - _GATE_KEYS
    if unknown:
        listed = ", ".join(sorted(str(key) for key in unknown))
        raise ContractError(f"[[gates]] entry #{index} has unknown key(s): {listed}")

    gate_id = entry.get("id")
    if not isinstance(gate_id, str) or not gate_id:
        raise ContractError(f"[[gates]] entry #{index} has a missing or empty id")
    if gate_id not in GATE_IDS:
        raise ContractError(f"unknown gate id: {gate_id!r}")

    group = entry.get("group")
    if group not in WORKFLOW_GROUPS:
        raise ContractError(f"gate {gate_id!r} has an invalid workflow group: {group!r}")

    description = entry.get("description", "")
    if not isinstance(description, str):
        raise ContractError(f"gate {gate_id!r} description must be a string")

    return Gate(
        id=gate_id,
        group=group,
        commands=_commands(entry.get("commands"), gate_id),
        required_paths=_paths(entry.get("required_paths", []), gate_id, "required_paths"),
        evidence_paths=_paths(entry.get("evidence_paths", []), gate_id, "evidence_paths"),
        os=_os(entry.get("os", []), gate_id),
        python=_python(entry.get("python", []), gate_id),
        description=description,
    )


def _commands(value: object, gate_id: str) -> tuple[Command, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"gate {gate_id!r} must define a non-empty commands list")

    commands: list[Command] = []
    for position, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"argv"}:
            raise ContractError(
                f"gate {gate_id!r} command #{position} must be a table with only 'argv'"
            )
        argv = item["argv"]
        if not isinstance(argv, list) or not argv:
            raise ContractError(f"gate {gate_id!r} command #{position} has an empty argv")
        if not all(isinstance(token, str) and token for token in argv):
            raise ContractError(
                f"gate {gate_id!r} command #{position} argv must be non-empty strings"
            )
        for token in argv:
            operator = next((op for op in _SHELL_OPERATORS if op in token), None)
            if operator is not None:
                raise ContractError(
                    f"gate {gate_id!r} command #{position} argv token {token!r} "
                    f"contains a shell operator ({operator!r})"
                )
        commands.append(Command(argv=tuple(argv)))
    return tuple(commands)


def _paths(value: object, gate_id: str, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"gate {gate_id!r} {field_name} must be a list")

    resolved: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ContractError(
                f"gate {gate_id!r} {field_name} entry must be a non-empty string"
            )
        if _unsafe_path(item):
            raise ContractError(
                f"gate {gate_id!r} {field_name} entry {item!r} is not a plain "
                f"repository-relative path"
            )
        resolved.append(item)
    return tuple(resolved)


def _unsafe_path(candidate: str) -> bool:
    if candidate.startswith(("/", "~")):
        return True
    if "\\" in candidate:
        return True
    if re.match(r"\A[A-Za-z]:", candidate):
        return True
    segments = candidate.split("/")
    return "" in segments or ".." in segments or "." in segments


def _os(value: object, gate_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"gate {gate_id!r} os must be a list")
    for item in value:
        if item not in SUPPORTED_OS:
            raise ContractError(
                f"gate {gate_id!r} names an unsupported OS runner: {item!r}"
            )
    if len(set(value)) != len(value):
        raise ContractError(f"gate {gate_id!r} os list has duplicate entries")
    return tuple(value)


def _python(value: object, gate_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"gate {gate_id!r} python must be a list")
    for item in value:
        if not isinstance(item, str) or not _PYTHON_RE.match(item):
            raise ContractError(
                f"gate {gate_id!r} names an invalid Python version: {item!r}"
            )
    if len(set(value)) != len(value):
        raise ContractError(f"gate {gate_id!r} python list has duplicate entries")
    return tuple(value)
