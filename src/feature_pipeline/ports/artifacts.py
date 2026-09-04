"""The run-evidence (artifact) store port.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``).

Every evidence file a run produces — an executor report, a captured command log, a
diagnostic — is written through this port, never with an ad-hoc ``open(...)``. Two
guarantees matter (``docs/adr/004`` / ``docs/adr/005``):

* **publish before reference** — :meth:`ArtifactStorePort.publish` returns only after the
  bytes are on disk and ``fsync``-ed. The caller records the returned :class:`ArtifactRef`
  into a new state snapshot and commits *after* that, so ``run.json`` can never name a file
  that is not there.
* **orphan reconciliation** — :meth:`ArtifactStorePort.reconcile_orphans` moves any evidence
  file the committed state does not reference into ``orphans/``. It is never deleted and
  never silently adopted, because the storage tree may hold adjacent user data.

The writer performs **no redaction** (``docs/adr/005`` — redaction is a presentation-boundary
policy; canonical evidence and state store validated values). A caller that needs a redacted
log redacts the text before handing it to :meth:`publish`.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol, runtime_checkable

from feature_pipeline.domain.errors import DomainError

#: The evidence categories a run writes. ``report`` -> ``<run-id>/reports/``,
#: ``log`` -> ``<run-id>/logs/``.
ArtifactKind = Literal["report", "log"]


class ArtifactNameError(DomainError):
    """An artifact name is empty, absolute, or contains a path separator or ``..``.

    Names address a file *inside* the run's ``reports/`` or ``logs/`` directory; they may not
    escape it. Rejected before anything is written.
    """

    code = "invalid-artifact-name"


@dataclass(frozen=True)
class ArtifactRef:
    """A run-relative pointer to one published evidence file.

    :attr:`relative_path` is POSIX-style and rooted at the run directory
    (``"reports/EXEC-1.md"``), so it is stable across machines and safe to persist verbatim
    in ``run.json``.
    """

    kind: ArtifactKind
    relative_path: str


@dataclass(frozen=True)
class OrphanReport:
    """One evidence file that the committed state did not reference and was quarantined."""

    original: str
    moved_to: str


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Transactional evidence writes plus orphan reconciliation for one run directory."""

    def publish(
        self, kind: ArtifactKind, name: str, content: str | bytes
    ) -> ArtifactRef:
        """Write ``content`` to ``<run-id>/<kind>s/<name>`` crash-safely and return its ref.

        The bytes are flushed and ``fsync``-ed before the call returns; only then may the
        caller reference the returned :class:`ArtifactRef` from state it is about to commit.
        """
        ...

    def reconcile_orphans(self, referenced: Iterable[str]) -> tuple[OrphanReport, ...]:
        """Move every ``reports/`` or ``logs/`` file not in ``referenced`` into ``orphans/``.

        ``referenced`` is the set of run-relative paths the committed state names. Returns
        one :class:`OrphanReport` per moved file, in sorted path order. Never deletes.
        """
        ...


__all__ = [
    "ArtifactKind",
    "ArtifactNameError",
    "ArtifactRef",
    "ArtifactStorePort",
    "OrphanReport",
]
