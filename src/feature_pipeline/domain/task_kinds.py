"""The versioned task-kind catalog: standard-library loaders and validators.

**REC-01** (``docs/plans/tasks/REC-01_executor-context-and-catalog-recovery.md``) — this
module supersedes the blocked TC-04 catalog delivery. It turns the dispatch producer/operation
inventory characterized in ``docs/validation/task-model-routing/TC-01-baseline.md`` into a
portable, editable contract:

* the data lives in packaged JSON resources under
  ``src/feature_pipeline/catalogs/task_kinds/<version>/catalog.json`` so a wheel or sdist
  carries the exact revision without a source checkout;
* :func:`load_catalog` / :func:`load_catalog_from_mapping` parse and *fully validate* a
  revision, failing closed with a stable :class:`CatalogError` code before returning a
  :class:`TaskKindCatalog` — i.e. before any caller could dispatch against it;
* every record carries the facets REC-01 requires: identity, ownership, implementation
  status, role, stack/capability constraints, risk/complexity, context/budget, independence,
  repair/retry, verification, provenance, lifecycle, and aliases/replacements.

The runtime import graph stays standard-library only (``docs/adr/003``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Iterable, Mapping

from .errors import DomainError

#: The import package the versioned catalog resources live under.
CATALOG_PACKAGE = "feature_pipeline.catalogs"

#: JSON ``schema_version`` values this loader understands. A payload outside this set fails
#: closed with :class:`UnknownCatalogVersion` rather than being parsed best-effort.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: The packaged catalog revision :func:`load_catalog` reads when no version is named.
DEFAULT_CATALOG_VERSION = "v1"

STATUS_VALUES = frozenset({
    "live", "library", "manual", "reserved", "unsupported", "disabled",
})
ROLE_VALUES = frozenset({
    "executor", "task_verifier", "test_verifier", "runner", "operator", "human", "none",
})
RISK_VALUES = frozenset({"low", "medium", "high"})
SESSION_POLICIES = frozenset({"fresh", "resume", "fresh-or-resume", "not-applicable"})
VERIFICATION_TIERS = frozenset({"none", "smoke", "full"})
BUDGET_VALUES = frozenset({"routine", "diagnostic", "none"})

#: Record keys that would make a catalog entry an *implicitly executable plugin* — a data file
#: that names code to run. REC-01 requires these to fail before dispatch.
_EXECUTABLE_PLUGIN_KEYS = frozenset({
    "executable", "entrypoint", "entry_point", "plugin", "module", "hook", "hooks",
    "command", "callable", "script", "shell", "argv", "run",
})

#: The complete set of keys a record may carry. Anything else is an invalid schema.
_ALLOWED_RECORD_KEYS = frozenset({
    "id", "version", "title", "summary", "owner", "call_sites", "provenance", "lifecycle",
    "status", "role", "routable", "dispatchable", "constraints", "risk", "complexity",
    "context", "independence", "repair", "verification", "aliases", "replaces",
    "replaced_by", "namespace", "extends", "notes",
})

_RECORD_DEFAULTS: dict[str, object] = {
    "version": "1.0.0",
    "status": "live",
    "role": "runner",
    "routable": False,
    "dispatchable": True,
    "constraints": {},
    "risk": "medium",
    "complexity": "medium",
    "context": {},
    "independence": {},
    "repair": {},
    "verification": {},
    "aliases": [],
    "replaces": [],
    "replaced_by": None,
    "namespace": None,
    "notes": "",
}

_CONSTRAINTS_DEFAULTS: dict[str, object] = {
    "stacks": ["*"],
    "required_capabilities": [],
    "session_policy": "not-applicable",
}


class CatalogError(DomainError):
    """Base class for every fail-closed task-kind catalog rejection."""

    code = "task-kind-catalog-error"


class UnknownCatalogVersion(CatalogError):
    """The requested packaged revision, or a payload ``schema_version``, is not supported."""

    code = "task-kind-unknown-version"


class CatalogSchemaError(CatalogError):
    """A record (or the envelope) violates the task-kind schema."""

    code = "task-kind-invalid-schema"


class DanglingReference(CatalogError):
    """A ``replaces`` / ``replaced_by`` entry names a kind the catalog does not define."""

    code = "task-kind-dangling-reference"


class DuplicateIdentity(CatalogError):
    """Two records claim the same id, or an alias collides with another identity."""

    code = "task-kind-duplicate-identity"


class IncompatibleExtension(CatalogError):
    """A namespaced extension declares a schema/catalog version it cannot extend."""

    code = "task-kind-incompatible-extension"


class ExecutablePluginRejected(CatalogError):
    """A record carries a key that would make it an implicitly executable plugin."""

    code = "task-kind-executable-plugin"


@dataclass(frozen=True)
class CatalogConstraints:
    """Stack / capability / session constraints one task kind places on a launch."""

    stacks: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    session_policy: str


@dataclass(frozen=True)
class TaskKind:
    """One fully-normalized catalog record."""

    id: str
    version: str
    title: str
    summary: str
    owner: str
    call_sites: tuple[str, ...]
    provenance: tuple[str, ...]
    lifecycle_stage: str
    status: str
    role: str
    routable: bool
    dispatchable: bool
    constraints: CatalogConstraints
    risk: str
    complexity: str
    context_requires: tuple[str, ...]
    output_byte_budget: str
    parallelizable: bool
    max_repair_attempts: int | None
    retryable: bool
    verification_tier: str
    verifiers: tuple[str, ...]
    aliases: tuple[str, ...]
    replaces: tuple[str, ...]
    replaced_by: str | None
    namespace: str | None
    notes: str

    def as_canonical(self) -> dict[str, object]:
        """A deterministic, JSON-serialisable projection used for the catalog digest."""
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "owner": self.owner,
            "call_sites": list(self.call_sites),
            "provenance": list(self.provenance),
            "lifecycle_stage": self.lifecycle_stage,
            "status": self.status,
            "role": self.role,
            "routable": self.routable,
            "dispatchable": self.dispatchable,
            "constraints": {
                "stacks": list(self.constraints.stacks),
                "required_capabilities": list(self.constraints.required_capabilities),
                "session_policy": self.constraints.session_policy,
            },
            "risk": self.risk,
            "complexity": self.complexity,
            "context_requires": list(self.context_requires),
            "output_byte_budget": self.output_byte_budget,
            "parallelizable": self.parallelizable,
            "max_repair_attempts": self.max_repair_attempts,
            "retryable": self.retryable,
            "verification_tier": self.verification_tier,
            "verifiers": list(self.verifiers),
            "aliases": list(self.aliases),
            "replaces": list(self.replaces),
            "replaced_by": self.replaced_by,
            "namespace": self.namespace,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TaskKindCatalog:
    """A validated, immutable task-kind catalog revision."""

    schema_version: int
    catalog_version: str
    source: str
    digest: str
    task_kinds: tuple[TaskKind, ...]
    _by_id: Mapping[str, TaskKind] = field(repr=False)
    _by_name: Mapping[str, TaskKind] = field(repr=False)

    @property
    def by_id(self) -> Mapping[str, TaskKind]:
        return self._by_id

    def get(self, kind_id: str) -> TaskKind:
        """Return the record with exactly ``kind_id`` or raise :class:`KeyError`."""
        return self._by_id[kind_id]

    def resolve(self, name: str) -> TaskKind:
        """Return the record named by ``name`` as an id *or* an alias."""
        return self._by_name[name]

    def routable(self) -> tuple[TaskKind, ...]:
        """The records an operator may name as a dispatched task's kind."""
        return tuple(kind for kind in self.task_kinds if kind.routable)


# --- parsing / validation ----------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogSchemaError(message)


def _str_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    _require(
        isinstance(value, list) and all(isinstance(item, str) for item in value),
        f"'{field_name}' must be a list of strings",
    )
    assert isinstance(value, list)
    return tuple(value)  # type: ignore[arg-type]


def _require_str(value: object, *, field_name: str) -> str:
    _require(isinstance(value, str), f"'{field_name}' must be a string")
    assert isinstance(value, str)
    return value


def _require_str_or_none(value: object, *, field_name: str) -> str | None:
    _require(value is None or isinstance(value, str), f"'{field_name}' must be a string or null")
    assert value is None or isinstance(value, str)
    return value


def _require_bool(value: object, *, field_name: str) -> bool:
    _require(isinstance(value, bool), f"'{field_name}' must be a boolean")
    assert isinstance(value, bool)
    return value


def _require_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    _require(isinstance(value, Mapping), f"'{field_name}' must be a mapping")
    assert isinstance(value, Mapping)
    return value


def _merged(raw: Mapping[str, object]) -> dict[str, object]:
    merged = dict(_RECORD_DEFAULTS)
    merged.update(raw)
    return merged


def _parse_constraints(value: object) -> CatalogConstraints:
    _require(isinstance(value, Mapping), "'constraints' must be a mapping")
    assert isinstance(value, Mapping)
    unknown = set(value) - set(_CONSTRAINTS_DEFAULTS)
    _require(not unknown, f"unknown constraints key(s): {sorted(unknown)}")
    merged = dict(_CONSTRAINTS_DEFAULTS)
    merged.update(value)
    session_policy = _require_str(merged["session_policy"], field_name="constraints.session_policy")
    _require(
        session_policy in SESSION_POLICIES,
        f"constraints.session_policy '{session_policy}' is not one of {sorted(SESSION_POLICIES)}",
    )
    return CatalogConstraints(
        stacks=_str_tuple(merged["stacks"], field_name="constraints.stacks"),
        required_capabilities=_str_tuple(
            merged["required_capabilities"], field_name="constraints.required_capabilities"
        ),
        session_policy=session_policy,
    )


def _parse_record(raw: object, *, schema_version: int, catalog_version: str) -> TaskKind:
    _require(isinstance(raw, Mapping), "each task-kind record must be a mapping")
    assert isinstance(raw, Mapping)

    executable = sorted(set(raw) & _EXECUTABLE_PLUGIN_KEYS)
    if executable:
        raise ExecutablePluginRejected(
            f"record carries implicitly executable key(s) {executable}; catalog records are "
            "data, never code"
        )

    unknown = set(raw) - _ALLOWED_RECORD_KEYS
    _require(not unknown, f"unknown record key(s): {sorted(unknown)}")

    for required in ("id", "title", "summary", "owner", "call_sites", "provenance", "lifecycle"):
        _require(required in raw, f"record is missing required field '{required}'")

    merged = _merged(raw)

    kind_id = merged["id"]
    _require(isinstance(kind_id, str) and kind_id.strip() == kind_id and kind_id != "",
             "'id' must be a non-empty, unpadded string")
    assert isinstance(kind_id, str)
    version = _require_str(merged["version"], field_name="version")
    title = _require_str(merged["title"], field_name="title")
    summary = _require_str(merged["summary"], field_name="summary")
    owner = _require_str(merged["owner"], field_name="owner")
    notes = _require_str(merged["notes"], field_name="notes")

    lifecycle = _require_mapping(merged["lifecycle"], field_name="lifecycle")
    _require(isinstance(lifecycle.get("stage"), str), "'lifecycle.stage' must be a string")
    lifecycle_stage = lifecycle["stage"]
    assert isinstance(lifecycle_stage, str)

    status = _require_str(merged["status"], field_name="status")
    _require(status in STATUS_VALUES, f"status '{status}' is not one of {sorted(STATUS_VALUES)}")
    role = _require_str(merged["role"], field_name="role")
    _require(role in ROLE_VALUES, f"role '{role}' is not one of {sorted(ROLE_VALUES)}")
    levels: dict[str, str] = {}
    for level_field in ("risk", "complexity"):
        levels[level_field] = _require_str(merged[level_field], field_name=level_field)
        _require(levels[level_field] in RISK_VALUES,
                 f"{level_field} '{levels[level_field]}' is not one of {sorted(RISK_VALUES)}")
    risk = levels["risk"]
    complexity = levels["complexity"]
    routable = _require_bool(merged["routable"], field_name="routable")
    dispatchable = _require_bool(merged["dispatchable"], field_name="dispatchable")

    context = _require_mapping(merged["context"], field_name="context")
    unknown_ctx = set(context) - {"requires", "output_byte_budget"}
    _require(not unknown_ctx, f"unknown context key(s): {sorted(unknown_ctx)}")
    budget = context.get("output_byte_budget", "routine")
    _require(budget in BUDGET_VALUES,
             f"context.output_byte_budget '{budget}' is not one of {sorted(BUDGET_VALUES)}")
    assert isinstance(budget, str)

    independence = _require_mapping(merged["independence"], field_name="independence")
    unknown_ind = set(independence) - {"parallelizable"}
    _require(not unknown_ind, f"unknown independence key(s): {sorted(unknown_ind)}")
    parallelizable = _require_bool(
        independence.get("parallelizable", False), field_name="independence.parallelizable"
    )

    repair = _require_mapping(merged["repair"], field_name="repair")
    unknown_rep = set(repair) - {"max_repair_attempts", "retryable"}
    _require(not unknown_rep, f"unknown repair key(s): {sorted(unknown_rep)}")
    max_repair_attempts = repair.get("max_repair_attempts", None)
    _require(
        max_repair_attempts is None
        or (isinstance(max_repair_attempts, int) and not isinstance(max_repair_attempts, bool)
            and max_repair_attempts >= 0),
        "repair.max_repair_attempts must be a non-negative integer or null",
    )
    assert max_repair_attempts is None or (
        isinstance(max_repair_attempts, int) and not isinstance(max_repair_attempts, bool)
    )
    retryable = _require_bool(repair.get("retryable", False), field_name="repair.retryable")

    verification = _require_mapping(merged["verification"], field_name="verification")
    unknown_ver = set(verification) - {"tier", "verifiers"}
    _require(not unknown_ver, f"unknown verification key(s): {sorted(unknown_ver)}")
    tier = verification.get("tier", "none")
    _require(tier in VERIFICATION_TIERS,
             f"verification.tier '{tier}' is not one of {sorted(VERIFICATION_TIERS)}")
    assert isinstance(tier, str)

    namespace = _require_str_or_none(merged["namespace"], field_name="namespace")
    extends = merged.get("extends")
    if namespace is not None:
        _require(namespace != "", "'namespace' must be a non-empty string")
        _require(
            kind_id.startswith(f"{namespace}:"),
            f"namespaced record id '{kind_id}' must start with '{namespace}:'",
        )
        _require(isinstance(extends, Mapping), "a namespaced record must declare 'extends'")
        assert isinstance(extends, Mapping)
        if extends.get("schema_version") != schema_version or \
                extends.get("catalog_version") != catalog_version:
            raise IncompatibleExtension(
                f"extension '{kind_id}' extends "
                f"{extends.get('schema_version')}/{extends.get('catalog_version')}, "
                f"catalog is {schema_version}/{catalog_version}"
            )
    else:
        _require(extends is None, "'extends' is only valid on a namespaced record")

    replaced_by = _require_str_or_none(merged["replaced_by"], field_name="replaced_by")

    return TaskKind(
        id=kind_id,
        version=version,
        title=title,
        summary=summary,
        owner=owner,
        call_sites=_str_tuple(merged["call_sites"], field_name="call_sites"),
        provenance=_str_tuple(merged["provenance"], field_name="provenance"),
        lifecycle_stage=lifecycle_stage,
        status=status,
        role=role,
        routable=routable,
        dispatchable=dispatchable,
        constraints=_parse_constraints(merged["constraints"]),
        risk=risk,
        complexity=complexity,
        context_requires=_str_tuple(context.get("requires", []), field_name="context.requires"),
        output_byte_budget=budget,
        parallelizable=parallelizable,
        max_repair_attempts=max_repair_attempts,
        retryable=retryable,
        verification_tier=tier,
        verifiers=_str_tuple(verification.get("verifiers", []), field_name="verification.verifiers"),
        aliases=_str_tuple(merged["aliases"], field_name="aliases"),
        replaces=_str_tuple(merged["replaces"], field_name="replaces"),
        replaced_by=replaced_by,
        namespace=namespace,
        notes=notes,
    )


def load_catalog_from_mapping(data: Mapping[str, object], *, source: str = "<memory>") -> TaskKindCatalog:
    """Validate an already-parsed catalog payload and return an immutable revision.

    Raises a :class:`CatalogError` subclass — never returns a partially-valid catalog — for an
    unknown schema version, an invalid record schema, an implicitly executable plugin record, a
    duplicate identity, a dangling ``replaces`` / ``replaced_by`` reference, or an incompatible
    namespaced extension.
    """
    _require(isinstance(data, Mapping), "catalog payload must be a mapping")

    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnknownCatalogVersion(
            f"catalog schema_version {schema_version!r} is not one of "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    assert isinstance(schema_version, int)

    catalog_version = data.get("catalog_version")
    _require(isinstance(catalog_version, str) and catalog_version != "",
             "'catalog_version' must be a non-empty string")
    assert isinstance(catalog_version, str)

    raw_records = data.get("task_kinds")
    _require(isinstance(raw_records, list) and len(raw_records) > 0,
             "'task_kinds' must be a non-empty list")
    assert isinstance(raw_records, list)

    parsed = [
        _parse_record(raw, schema_version=schema_version, catalog_version=catalog_version)
        for raw in raw_records
    ]

    by_id: dict[str, TaskKind] = {}
    by_name: dict[str, TaskKind] = {}
    for kind in parsed:
        if kind.id in by_id or kind.id in by_name:
            raise DuplicateIdentity(f"task-kind identity '{kind.id}' is defined more than once")
        by_id[kind.id] = kind
        by_name[kind.id] = kind
    for kind in parsed:
        for alias in kind.aliases:
            if alias in by_name:
                raise DuplicateIdentity(
                    f"alias '{alias}' on '{kind.id}' collides with an existing identity"
                )
            by_name[alias] = kind

    known = set(by_id)
    for kind in parsed:
        for ref in kind.replaces:
            if ref not in known:
                raise DanglingReference(
                    f"'{kind.id}' replaces unknown kind '{ref}'"
                )
        if kind.replaced_by is not None and kind.replaced_by not in known:
            raise DanglingReference(
                f"'{kind.id}' is replaced_by unknown kind '{kind.replaced_by}'"
            )

    digest = _digest(schema_version, catalog_version, parsed)
    return TaskKindCatalog(
        schema_version=schema_version,
        catalog_version=catalog_version,
        source=source,
        digest=digest,
        task_kinds=tuple(parsed),
        _by_id=by_id,
        _by_name=by_name,
    )


def _digest(schema_version: int, catalog_version: str, kinds: Iterable[TaskKind]) -> str:
    payload = {
        "schema_version": schema_version,
        "catalog_version": catalog_version,
        "task_kinds": [kind.as_canonical() for kind in kinds],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


# --- packaged resource access ----------------------------------------------------------


def _catalog_root() -> Traversable:
    return resources.files(CATALOG_PACKAGE).joinpath("task_kinds")


def available_versions() -> tuple[str, ...]:
    """Every packaged revision directory that carries a ``catalog.json``."""
    root = _catalog_root()
    found: list[str] = []
    for child in root.iterdir():
        if child.is_dir() and child.joinpath("catalog.json").is_file():
            found.append(child.name)
    return tuple(sorted(found))


def load_catalog(version: str = DEFAULT_CATALOG_VERSION) -> TaskKindCatalog:
    """Load and validate the packaged catalog ``version`` (default :data:`DEFAULT_CATALOG_VERSION`)."""
    resource = _catalog_root().joinpath(version, "catalog.json")
    if not resource.is_file():
        raise UnknownCatalogVersion(f"no packaged task-kind catalog revision '{version}'")
    data = json.loads(resource.read_text(encoding="utf-8"))
    return load_catalog_from_mapping(
        data, source=f"{CATALOG_PACKAGE}/task_kinds/{version}/catalog.json"
    )


# --- generated Markdown inventory -----------------------------------------------------


def render_inventory(catalog: TaskKindCatalog) -> str:
    """Render the human-readable Markdown inventory for ``catalog`` (generated, never hand-edited)."""
    lines: list[str] = []
    lines.append("<!-- GENERATED by feature_pipeline.domain.task_kinds.render_inventory -->")
    lines.append("<!-- Source of truth: src/feature_pipeline/catalogs/task_kinds/"
                 f"{_version_dir(catalog)}/catalog.json -->")
    lines.append("")
    lines.append(f"# Task-kind catalog inventory ({catalog.catalog_version})")
    lines.append("")
    lines.append(f"- Schema version: `{catalog.schema_version}`")
    lines.append(f"- Records: {len(catalog.task_kinds)}")
    lines.append(f"- Digest: `{catalog.digest}`")
    lines.append("")
    lines.append("| Kind | Status | Role | Routable | Dispatchable | Risk | Complexity | Lifecycle |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for kind in sorted(catalog.task_kinds, key=lambda item: item.id):
        lines.append(
            f"| `{kind.id}` | {kind.status} | {kind.role} | "
            f"{'yes' if kind.routable else 'no'} | "
            f"{'yes' if kind.dispatchable else 'no'} | {kind.risk} | {kind.complexity} | "
            f"{kind.lifecycle_stage} |"
        )
    lines.append("")
    for kind in sorted(catalog.task_kinds, key=lambda item: item.id):
        lines.append(f"## `{kind.id}`")
        lines.append("")
        lines.append(kind.summary)
        lines.append("")
        lines.append(f"- Owner: {kind.owner}")
        lines.append(f"- Call sites: {', '.join(f'`{site}`' for site in kind.call_sites)}")
        lines.append(f"- Provenance: {', '.join(kind.provenance)}")
        lines.append(
            "- Constraints: stacks "
            f"{', '.join(f'`{stack}`' for stack in kind.constraints.stacks)}; "
            f"capabilities {', '.join(f'`{cap}`' for cap in kind.constraints.required_capabilities) or '—'}; "
            f"session `{kind.constraints.session_policy}`"
        )
        lines.append(
            f"- Context: {', '.join(f'`{item}`' for item in kind.context_requires) or '—'} "
            f"(budget `{kind.output_byte_budget}`)"
        )
        lines.append(
            f"- Repair: attempts {kind.max_repair_attempts}; retryable "
            f"{'yes' if kind.retryable else 'no'}"
        )
        lines.append(
            f"- Verification: tier `{kind.verification_tier}`; verifiers "
            f"{', '.join(f'`{name}`' for name in kind.verifiers) or '—'}"
        )
        parallelism = "yes" if kind.parallelizable else "no"
        lines.append(f"- Independent / parallelizable: {parallelism}")
        aliases = ", ".join(f"`{alias}`" for alias in kind.aliases) or "—"
        replaces = ", ".join(f"`{ref}`" for ref in kind.replaces) or "—"
        replaced_by = f"`{kind.replaced_by}`" if kind.replaced_by else "—"
        lines.append(f"- Aliases: {aliases}; replaces: {replaces}; replaced by: {replaced_by}")
        if kind.namespace:
            lines.append(f"- Namespace: `{kind.namespace}`")
        if kind.notes:
            lines.append(f"- Notes: {kind.notes}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _version_dir(catalog: TaskKindCatalog) -> str:
    tail = catalog.source.rsplit("/", 2)
    return tail[-2] if len(tail) >= 2 else DEFAULT_CATALOG_VERSION


def _write_packaged_inventory(version: str = DEFAULT_CATALOG_VERSION) -> str:
    """Regenerate the committed ``INVENTORY.md`` for ``version`` from its ``catalog.json``.

    Used by ``python -m feature_pipeline.domain.task_kinds`` and the drift-guard test. Writes
    to the in-tree source resource; a wheel/sdist ships the file already generated.
    """
    from pathlib import Path

    catalog = load_catalog(version)
    target = (
        Path(__file__).resolve().parents[1]
        / "catalogs" / "task_kinds" / version / "INVENTORY.md"
    )
    text = render_inventory(catalog)
    target.write_text(text, encoding="utf-8", newline="\n")
    return str(target)


if __name__ == "__main__":  # pragma: no cover - developer regeneration entry point
    for _version in available_versions():
        print(f"wrote {_write_packaged_inventory(_version)}")


__all__ = [
    "CATALOG_PACKAGE",
    "DEFAULT_CATALOG_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "CatalogConstraints",
    "CatalogError",
    "CatalogSchemaError",
    "DanglingReference",
    "DuplicateIdentity",
    "ExecutablePluginRejected",
    "IncompatibleExtension",
    "TaskKind",
    "TaskKindCatalog",
    "UnknownCatalogVersion",
    "available_versions",
    "load_catalog",
    "load_catalog_from_mapping",
    "render_inventory",
]
