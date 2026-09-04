"""ADR-004 run-directory identity: one immutable ``<run-id>/`` per run, plus ``latest.json``.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``).

``pipeline_core.state`` stores a run at ``<storage>/<feature>/run.json`` with no per-run
directory, and mints ``run_id`` from a second-resolution timestamp — two ``Run.create``
calls in the same wall-clock second collide, and a second fresh invocation overwrites
``run.json`` in place, orphaning the first run's evidence beside it (finding F-03).

This module is the accepted replacement (``docs/adr/004``):

* on-disk layout ``<storage>/<feature>/<run-id>/{run.json,reports/,logs/,orphans/}`` with a
  sibling ``<storage>/<feature>/latest.json`` = ``{"run_id": "<run-id>"}`` (a JSON
  indirection, **not** an OS symlink, for Windows/Linux parity);
* ``run-id`` = ``YYYYMMDDThhmmssZ`` + ``-`` + 8 random hex characters, so same-second
  creations still produce distinct ids and distinct directories;
* a fresh run **never** writes into an existing ``<run-id>/``;
* resume resolves an explicit ``run-id`` or, with none, whatever ``latest.json`` names; a
  ``latest.json`` pointing at a missing directory fails closed (:class:`StaleRunPointer`).

The optional single-active-run policy (``docs/adr/004`` rejected-as-default, kept as a future
opt-in) is available as :data:`RunIdentityPolicy` ``"single-active-run"``: a fresh start is
refused while ``latest.json`` exists.

Standard library only.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Literal

from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.identifiers import FeatureId
from feature_pipeline.ports.clock import Clock

from .durability import atomic_write_text

#: ``YYYYMMDDThhmmssZ`` — the ``run-id`` timestamp component (``docs/adr/004``).
RUN_ID_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: A well-formed ``run-id``: the compact UTC stamp, a dash, and 8 lowercase hex characters.
RUN_ID_RE = re.compile(r"\A\d{8}T\d{6}Z-[0-9a-f]{8}\Z")

RunIdentityPolicy = Literal["unique-run", "single-active-run"]


class RunIdentityError(DomainError):
    """Base class for a run-directory identity violation."""

    code = "run-identity-error"


class RunAlreadyExists(RunIdentityError):
    """A fresh run was allocated a ``<run-id>/`` directory that already exists on disk."""

    code = "run-already-exists"


class ActiveRunPresent(RunIdentityError):
    """A fresh start was refused under the ``single-active-run`` policy — state exists."""

    code = "active-run-present"


class StaleRunPointer(RunIdentityError):
    """``latest.json`` names a ``<run-id>/`` directory that is not on disk."""

    code = "stale-run-pointer"


def mint_run_id(clock: Clock, *, entropy: str | None = None) -> str:
    """Compose a collision-free ``run-id`` from an injected clock (``docs/adr/004``).

    ``entropy`` (8 hex characters) is injectable for deterministic tests; in production it is
    drawn from :func:`secrets.token_hex`.
    """
    if entropy is None:
        entropy = secrets.token_hex(4)
    if not re.fullmatch(r"[0-9a-f]{8}", entropy):
        raise RunIdentityError(f"run-id entropy must be 8 hex characters, got {entropy!r}")
    moment = clock.now()
    if moment.tzinfo is None:
        raise RunIdentityError("clock timestamps must be timezone-aware")
    stamp = moment.astimezone(timezone.utc).strftime(RUN_ID_STAMP_FORMAT)
    return f"{stamp}-{entropy}"


@dataclass(frozen=True)
class RunLayout:
    """The resolved paths for one run instance under ``<storage>/<feature>/<run-id>/``."""

    storage_root: Path
    feature: str
    run_id: str

    def __post_init__(self) -> None:
        FeatureId(self.feature)  # same slug rule the CLI enforces
        if not RUN_ID_RE.match(self.run_id):
            raise RunIdentityError(
                f"{self.run_id!r} is not a well-formed run-id "
                "(expected YYYYMMDDThhmmssZ-<8 hex>)"
            )

    # -- feature-level --------------------------------------------------------------------

    @property
    def feature_dir(self) -> Path:
        return self.storage_root / self.feature

    @property
    def latest_path(self) -> Path:
        return self.feature_dir / "latest.json"

    # -- run-level -----------------------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return self.feature_dir / self.run_id

    @property
    def state_path(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def reports_dir(self) -> Path:
        return self.run_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def orphans_dir(self) -> Path:
        return self.run_dir / "orphans"

    def dir_for(self, kind: str) -> Path:
        """``reports/`` or ``logs/`` for an artifact ``kind`` (``"report"`` / ``"log"``)."""
        if kind == "report":
            return self.reports_dir
        if kind == "log":
            return self.logs_dir
        raise RunIdentityError(f"unknown artifact kind {kind!r}")


def allocate_run(
    storage_root: str | Path,
    feature: str,
    run_id: str,
    *,
    policy: RunIdentityPolicy = "unique-run",
) -> RunLayout:
    """Resolve the layout for a *fresh* run and create its directory tree.

    A fresh run never writes into an existing ``<run-id>/`` (:class:`RunAlreadyExists`).
    Under ``"single-active-run"`` a fresh start is refused while ``latest.json`` exists
    (:class:`ActiveRunPresent`).
    """
    layout = RunLayout(Path(storage_root), feature, run_id)
    if policy == "single-active-run" and layout.latest_path.exists():
        raise ActiveRunPresent(
            f"a run already exists for {feature!r}; resume it or archive it first"
        )
    if layout.run_dir.exists():
        raise RunAlreadyExists(f"run directory already exists: {layout.run_id}")
    for directory in (layout.reports_dir, layout.logs_dir, layout.orphans_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return layout


def write_latest(layout: RunLayout) -> Path:
    """Point ``<feature>/latest.json`` at ``layout.run_id`` with a crash-safe write."""
    text = json.dumps({"run_id": layout.run_id}, indent=2) + "\n"
    return atomic_write_text(layout.latest_path, text)


def read_latest(storage_root: str | Path, feature: str) -> str:
    """Return the ``run_id`` ``<feature>/latest.json`` names, or fail closed.

    :class:`StaleRunPointer` if the file is absent, unreadable, or names a directory that is
    not on disk — a resume never fabricates a run.
    """
    root = Path(storage_root)
    pointer = root / feature / "latest.json"
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaleRunPointer(f"latest.json for {feature!r} is missing or unreadable") from exc
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise StaleRunPointer(f"latest.json for {feature!r} names no valid run-id")
    if not (root / feature / run_id).is_dir():
        raise StaleRunPointer(f"latest.json for {feature!r} points at a missing run {run_id}")
    return run_id


def resume_run(
    storage_root: str | Path, feature: str, run_id: str | None = None
) -> RunLayout:
    """Resolve the layout for an existing run — an explicit ``run_id`` or ``latest.json``.

    Fails closed (:class:`StaleRunPointer`) if the resolved ``<run-id>/`` is not on disk.
    """
    resolved = run_id or read_latest(storage_root, feature)
    layout = RunLayout(Path(storage_root), feature, resolved)
    if not layout.run_dir.is_dir():
        raise StaleRunPointer(f"run directory for {resolved} is not on disk")
    return layout


__all__ = [
    "RUN_ID_RE",
    "RUN_ID_STAMP_FORMAT",
    "ActiveRunPresent",
    "RunAlreadyExists",
    "RunIdentityError",
    "RunIdentityPolicy",
    "RunLayout",
    "StaleRunPointer",
    "allocate_run",
    "mint_run_id",
    "read_latest",
    "resume_run",
    "write_latest",
]
