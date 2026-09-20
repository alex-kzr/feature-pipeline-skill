"""Authorized in-task amendment lifecycle (TAM-01).

A logical Kanban task occasionally discovers, from runner-owned baseline evidence or
independent verifier findings, that its declared contract (allowed scope, verification
commands, or repair budget) is too narrow to reach its acceptance criteria. This module
defines the small canonical *amendment payload* a human approves, the immutable
:class:`AmendmentRevision` record persisted onto the existing task, and the fail-closed
validation every amendment request must pass before it ever reaches ``run.json`` or opens a
new executor window.

An amendment never changes the task's identity (its id) and never revises an already-``done``
task; it only ever appends a new, separately digested revision of a still-open task's
contract. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Contract fields an amendment is permitted to expand. Anything else (the task id, its
#: type, or its executor role) is immutable across every revision.
AMENDABLE_FIELDS = (
    "allowed_scope",
    "out_of_scope",
    "verification_commands",
    "max_repair_attempts",
    "documentation_impact",
)

#: The three baseline-command classifications a pre-dispatch diagnosis may produce.
CLASSIFICATION_TASK_ATTRIBUTABLE = "task_attributable"
CLASSIFICATION_AMENDMENT_REQUIRED = "amendment_required"
CLASSIFICATION_ENVIRONMENTAL = "environmental"
BASELINE_CLASSIFICATIONS = (
    CLASSIFICATION_TASK_ATTRIBUTABLE,
    CLASSIFICATION_AMENDMENT_REQUIRED,
    CLASSIFICATION_ENVIRONMENTAL,
)

#: Path prefixes an amendment may never add to a task's scope, regardless of approval —
#: they are the runner's own historical evidence and control-plane boundaries.
FORBIDDEN_SCOPE_PREFIXES = (".pipeline/", ".agents/", ".git/", ".gitmodules")


class AmendmentError(Exception):
    """A rejected amendment request. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_commands(commands: Iterable[object]) -> list[list[Any]]:
    normalized: list[list[Any]] = []
    for command in commands:
        if isinstance(command, Mapping):
            normalized.append([command.get("cwd"), list(command.get("argv", []))])
        elif hasattr(command, "cwd") and hasattr(command, "argv"):
            normalized.append([command.cwd, list(command.argv)])
        elif isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            cwd, argv = command
            normalized.append([cwd, list(argv)])
        else:
            raise AmendmentError(
                f"verification command entry must be a mapping, object with cwd/argv, "
                f"or (cwd, argv) pair, got {type(command).__name__}",
                "amendment-malformed-command",
            )
    return normalized


def _as_iterable(value: object) -> Iterable[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise AmendmentError(
            f"contract field must be an iterable, got {type(value).__name__}",
            "amendment-malformed-field",
        )
    return value


def canonical_amendment_fields(contract: Mapping[str, Any] | object) -> dict[str, Any]:
    """Extract the amendable subset of a task contract mapping in a deterministic shape."""
    def value(name: str, default: object = ()) -> object:
        return contract.get(name, default) if isinstance(contract, Mapping) else getattr(contract, name, default)

    return {
        "allowed_scope": sorted(str(item) for item in _as_iterable(value("allowed_scope"))),
        "out_of_scope": sorted(str(item) for item in _as_iterable(value("out_of_scope"))),
        "verification_commands": _normalize_commands(_as_iterable(value("verification_commands"))),
        "max_repair_attempts": value("max_repair_attempts", None),
        "documentation_impact": sorted(str(item) for item in _as_iterable(value("documentation_impact"))),
    }


def effective_task_contract(contract: Mapping[str, Any] | object) -> dict[str, Any]:
    """Project the runner-authoritative amendable task surface for an agent briefing.

    Callers must pass the task contract selected by the runner, rather than re-reading a
    Markdown task card.  That makes an accepted amendment govern both implementation and
    independent review without granting an unapproved task-file edit any authority.
    """
    return canonical_amendment_fields(contract)


def render_effective_task_contract(contract: Mapping[str, Any] | object) -> str:
    """Render the one canonical effective-contract projection embedded in agent prompts."""
    fields = effective_task_contract(contract)
    commands = fields["verification_commands"]
    command_lines = (
        ["  none declared"] if not commands else [
            f"  - {cwd} -> {' '.join(argv)}" for cwd, argv in commands
        ]
    )
    return "\n".join([
        f"- Allowed scope: {', '.join(fields['allowed_scope']) or 'none'}",
        f"- Out of scope: {', '.join(fields['out_of_scope']) or 'none'}",
        "- Verification commands:",
        *command_lines,
        f"- Maximum repair attempts: {fields['max_repair_attempts']}",
        f"- Documentation impact: {', '.join(fields['documentation_impact']) or 'none'}",
    ])


def contract_digest(fields: Mapping[str, Any]) -> str:
    """A stable ``sha256:<hex>`` identity for one amendable-contract snapshot."""
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def diff_amendable_fields(prior: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """The amendable field names whose canonical value differs between two contracts."""
    return sorted(name for name in AMENDABLE_FIELDS if prior.get(name) != new.get(name))


def _validate_path(path: str) -> None:
    normalized = str(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (normalized.startswith(("~", "/")) or re.match(r"^[A-Za-z]:/", normalized)
            or ".." in normalized.split("/")):
        raise AmendmentError(f"amendment path '{path}' escapes the repository root", "unsafe-path")
    for forbidden in FORBIDDEN_SCOPE_PREFIXES:
        if normalized == forbidden.rstrip("/") or normalized.startswith(forbidden):
            raise AmendmentError(
                f"amendment path '{path}' is out of bounds ({forbidden} is never amendable)",
                "forbidden-scope-path",
            )


@dataclass(frozen=True)
class AmendmentRequest:
    """The explicit human-approved input to :func:`build_amendment_revision`."""

    task_id: str
    task_status: str
    prior_contract: Mapping[str, Any]
    new_contract: Mapping[str, Any]
    rationale: str
    approved_by: str
    source_evidence: str
    added_paths: Sequence[str] = ()


@dataclass(frozen=True)
class AmendmentRevision:
    """One immutable, append-only amendment of an existing (non-done) task's contract."""

    task_id: str
    revision: int
    prior_digest: str
    new_digest: str
    changed_fields: tuple[str, ...]
    added_paths: tuple[str, ...]
    rationale: str
    approved_by: str
    source_evidence: str
    created_at: str
    epoch: int
    new_contract: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "prior_digest": self.prior_digest,
            "new_digest": self.new_digest,
            "changed_fields": list(self.changed_fields),
            "added_paths": list(self.added_paths),
            "rationale": self.rationale,
            "approved_by": self.approved_by,
            "source_evidence": self.source_evidence,
            "created_at": self.created_at,
            "epoch": self.epoch,
            "new_contract": dict(self.new_contract),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AmendmentRevision":
        return cls(
            task_id=data["task_id"],
            revision=data["revision"],
            prior_digest=data["prior_digest"],
            new_digest=data["new_digest"],
            changed_fields=tuple(data.get("changed_fields", ())),
            added_paths=tuple(data.get("added_paths", ())),
            rationale=data["rationale"],
            approved_by=data["approved_by"],
            source_evidence=data["source_evidence"],
            created_at=data["created_at"],
            epoch=data["epoch"],
            new_contract=data.get("new_contract", {}),
        )


def validate_amendment_request(request: AmendmentRequest, *, expected_task_id: str) -> None:
    """Fail-closed validation shared by every amendment entry point.

    Rejects: a missing rationale or approval, a completed task, a task-id change, an
    amendment path escaping the repository root or touching a forbidden control-plane
    prefix, and a no-op amendment whose contract does not actually change.
    """
    if request.task_id != expected_task_id:
        raise AmendmentError(
            f"an amendment cannot change the task id ({request.task_id!r} != {expected_task_id!r})",
            "task-id-changed",
        )
    if request.task_status == "done":
        raise AmendmentError(
            f"'{request.task_id}' is already done; a completed task cannot be amended",
            "task-already-done",
        )
    if not request.rationale or not request.rationale.strip():
        raise AmendmentError("an amendment requires a non-empty rationale", "missing-rationale")
    if not request.approved_by or not request.approved_by.strip():
        raise AmendmentError("an amendment requires explicit human approval", "missing-approval")
    if not request.source_evidence or not request.source_evidence.strip():
        raise AmendmentError("an amendment requires a source-evidence reference", "missing-source-evidence")
    for contract in (request.prior_contract, request.new_contract):
        unknown = set(contract).difference(AMENDABLE_FIELDS)
        if unknown:
            raise AmendmentError(
                "an amendment may change only its declared amendable contract fields",
                "immutable-contract-field",
            )
    prior_fields = canonical_amendment_fields(request.prior_contract)
    new_fields = canonical_amendment_fields(request.new_contract)
    for path in request.added_paths:
        _validate_path(path)
    prior_allowed_scope = set(prior_fields["allowed_scope"])
    for path in new_fields["allowed_scope"]:
        if path not in prior_allowed_scope:
            _validate_path(path)
    if prior_fields == new_fields:
        raise AmendmentError(
            "an amendment must actually change the task's amendable contract fields",
            "no-op-amendment",
        )


def build_amendment_revision(
    request: AmendmentRequest, *, next_revision: int, next_epoch: int,
) -> AmendmentRevision:
    """Validate ``request`` and build its immutable revision record.

    Raises :class:`AmendmentError` (never persists anything) on any fail-closed condition;
    callers persist the returned revision only after this succeeds.
    """
    validate_amendment_request(request, expected_task_id=request.task_id)
    prior_fields = canonical_amendment_fields(request.prior_contract)
    new_fields = canonical_amendment_fields(request.new_contract)
    return AmendmentRevision(
        task_id=request.task_id,
        revision=next_revision,
        prior_digest=contract_digest(prior_fields),
        new_digest=contract_digest(new_fields),
        changed_fields=tuple(diff_amendable_fields(prior_fields, new_fields)),
        added_paths=tuple(request.added_paths),
        rationale=request.rationale,
        approved_by=request.approved_by,
        source_evidence=request.source_evidence,
        created_at=_now(),
        epoch=next_epoch,
        new_contract=new_fields,
    )


def classify_baseline_failure(
    *, exit_code: int, cwd_paths_exist: bool, path_covered_by_scope: bool,
) -> str:
    """Classify one pre-dispatch declared-check failure.

    ``environmental`` when the command's own working directory is unavailable (a setup
    problem, not a task defect); ``amendment_required`` when the command failed but its
    causal evidence lies outside the task's current declared scope; otherwise
    ``task_attributable`` — the current revision's own repair budget applies.
    """
    if exit_code == 0:
        raise AmendmentError("a passing baseline command has no failure to classify", "not-a-failure")
    if not cwd_paths_exist:
        return CLASSIFICATION_ENVIRONMENTAL
    if not path_covered_by_scope:
        return CLASSIFICATION_AMENDMENT_REQUIRED
    return CLASSIFICATION_TASK_ATTRIBUTABLE


@dataclass(frozen=True)
class AmendmentRequiredResult:
    """The durable ``AMENDMENT_REQUIRED`` outcome for a pre-dispatch baseline diagnosis.

    Retains the active task card (no substitute task is created or completed) and states
    the minimal observed paths, the causal command evidence, and that a human decision is
    required before any repair attempt is spent.
    """

    task_id: str
    observed_paths: tuple[str, ...]
    causal_commands: tuple[str, ...]
    reason: str
    outcome: str = "AMENDMENT_REQUIRED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "task_id": self.task_id,
            "observed_paths": list(self.observed_paths),
            "causal_commands": list(self.causal_commands),
            "reason": self.reason,
            "requires_human_decision": True,
        }
