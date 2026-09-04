"""The deterministic allowed-scope gate for one executor window.

**WT-02** (``docs/plans/tasks/WT-02_deterministic-scope-gate.md``, ``docs/adr/007`` §6).

WT-01 gave the runner *bounded* attribution of an executor launch window
(:class:`feature_pipeline.ports.worktree.WorktreeInspection`): every path the executor
touched, each already labelled ``in_allowed_scope`` or ``out_of_scope``. But nothing acted
on that label — ``dispatch_executor`` transitioned ``running -> implemented`` regardless and
left the decision to the LLM verifier (BL-02 finding **F-04**). The CLI, meanwhile,
documents an out-of-scope change as a blocking outcome.

:func:`enforce_scope_gate` closes that gap. It runs **immediately after attribution and
before any verifier is launched**, and returns a :class:`ScopeGateDecision`:

* ``clean`` — every executor-owned change is in scope; :pyattr:`~ScopeGateDecision.verifiers_may_launch`
  is ``True``;
* ``scope-violation`` — at least one executor-owned change is outside the allowed scope;
  ``verifiers_may_launch`` is ``False`` and the caller blocks the task (the documented
  exit ``20`` outcome);
* ``attribution-unavailable`` — WT-01 could not attribute the window with confidence, so
  scope compliance cannot be proven; the gate fails closed the same way.

Two invariants from ``docs/adr/007`` §6 hold here:

* **the gate is mechanical** — it re-derives every classification from the task's
  ``Allowed scope`` via :class:`feature_pipeline.domain.scope.AllowedScope`; it never trusts
  the label the infrastructure inspector attached;
* **no file is reverted or cleaned** — the only thing the gate writes is bounded evidence,
  through the typed :class:`feature_pipeline.ports.artifacts.ArtifactStorePort`, carrying
  explicit run and repository coordinates (AC-3). Out-of-scope files may hold user work.

A change the executor did *not* make never reaches the gate: WT-01's candidate set only
contains paths whose content differs across the window, so a file the user left dirty before
the run and the executor never touched is not a candidate and cannot trip the gate.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from feature_pipeline.domain.scope import (
    IN_ALLOWED_SCOPE,
    OUT_OF_SCOPE,
    AllowedScope,
    ScopeViolation,
)
from feature_pipeline.ports.artifacts import ArtifactRef, ArtifactStorePort
from feature_pipeline.ports.worktree import WorktreeInspection

#: Every executor-owned change is inside the allowed scope. Verifiers may launch.
SCOPE_CLEAN = "clean"
#: An executor-owned change landed outside the allowed scope. The task is blocked.
SCOPE_VIOLATION = "scope-violation"
#: Attribution itself could not be trusted, so scope compliance cannot be proven.
ATTRIBUTION_UNAVAILABLE = "attribution-unavailable"

#: The default coordinate-safe repository label persisted in evidence — the same redaction
#: token the rest of the core writes in place of a host path.
DEFAULT_REPOSITORY_LABEL = "<repo_root>"


@dataclass(frozen=True)
class ScopeEvidence:
    """A typed, coordinate-safe pointer to the persisted scope manifest and diff.

    ``run_id`` and ``repository`` are the explicit coordinates AC-3 requires; ``manifest``
    and ``diff`` are :class:`~feature_pipeline.ports.artifacts.ArtifactRef` values whose
    ``relative_path`` is rooted at the run directory, so the whole record is safe to persist
    in ``run.json`` verbatim and to hand to diagnostics or a human reviewer.
    """

    run_id: str
    repository: str
    confidence: str
    manifest: ArtifactRef
    diff: ArtifactRef


@dataclass(frozen=True)
class ScopeGateDecision:
    """The outcome of the scope gate for one executor window."""

    task_id: str
    outcome: str  # SCOPE_CLEAN | SCOPE_VIOLATION | ATTRIBUTION_UNAVAILABLE
    violations: tuple[ScopeViolation, ...]
    evidence: ScopeEvidence
    reason: str | None = None

    @property
    def verifiers_may_launch(self) -> bool:
        """``True`` only for a ``clean`` decision — the one state in which the pipeline
        proceeds to verification."""
        return self.outcome == SCOPE_CLEAN


def enforce_scope_gate(
    inspection: WorktreeInspection,
    *,
    task_id: str,
    run_id: str,
    allowed_scope: Sequence[str],
    store: ArtifactStorePort,
    attempt: int = 1,
    repository: str = DEFAULT_REPOSITORY_LABEL,
) -> ScopeGateDecision:
    """Apply the deterministic scope gate to one attributed executor window.

    ``inspection`` is the WT-01 attribution of the window; ``allowed_scope`` is the task's
    declared ``Allowed scope`` (raw glob strings). The manifest and diff are published
    through ``store`` before the decision is returned, so a caller may reference the
    returned :class:`ScopeEvidence` from state it is about to commit.
    """
    scope = AllowedScope.parse(allowed_scope)

    rows: list[dict[str, object]] = []
    violations: list[ScopeViolation] = []
    for candidate in inspection.candidates:
        classification = scope.classify(candidate.path)
        rows.append(
            {
                "path": candidate.path,
                "change": candidate.change.value,
                "classification": classification,
                "digest": candidate.digest,
                "boundary": candidate.boundary,
                "is_symlink": candidate.is_symlink,
            }
        )
        # A window that could not be attributed with confidence yields a partial candidate
        # set; fail the gate closed on the confidence grade rather than enumerate from it.
        if inspection.available and classification == OUT_OF_SCOPE:
            violations.append(
                ScopeViolation(
                    path=candidate.path,
                    change=candidate.change.value,
                    boundary=candidate.boundary,
                    digest=candidate.digest,
                )
            )

    if not inspection.available:
        outcome = ATTRIBUTION_UNAVAILABLE
        reason: str | None = (
            inspection.reason
            or "the executor window could not be attributed with confidence"
        )
    elif violations:
        outcome = SCOPE_VIOLATION
        reason = "executor-owned changes outside the allowed scope: " + ", ".join(
            v.path for v in violations
        )
    else:
        outcome = SCOPE_CLEAN
        reason = None

    manifest = {
        "task_id": task_id,
        "run_id": run_id,
        "repository": repository,
        "attempt": attempt,
        "outcome": outcome,
        "attribution_available": inspection.available,
        "attribution_confidence": inspection.confidence,
        "reason": reason,
        "changed_files": rows,
        "violations": [v.path for v in violations],
    }
    stem = f"{task_id}-scope-{attempt}"
    manifest_ref = store.publish(
        "report", f"{stem}.json", json.dumps(manifest, indent=2) + "\n"
    )
    diff_ref = store.publish(
        "report",
        f"{stem}.diff",
        inspection.diff_text or f"# attribution: {inspection.confidence}\n",
    )

    return ScopeGateDecision(
        task_id=task_id,
        outcome=outcome,
        violations=tuple(violations),
        evidence=ScopeEvidence(
            run_id=run_id,
            repository=repository,
            confidence=inspection.confidence,
            manifest=manifest_ref,
            diff=diff_ref,
        ),
        reason=reason,
    )


__all__ = [
    "SCOPE_CLEAN",
    "SCOPE_VIOLATION",
    "ATTRIBUTION_UNAVAILABLE",
    "DEFAULT_REPOSITORY_LABEL",
    "ScopeEvidence",
    "ScopeGateDecision",
    "enforce_scope_gate",
]
