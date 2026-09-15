"""TSL-14 — a project-declared legacy reconciliation registry.

A completed replacement task's canonical contract cannot be mutated to add a
``## Supersession`` declaration after it has been independently verified: doing so would
change its recorded contract digest and invalidate the very PASS/PASS evidence reconciliation
depends on. This module reads a *separate*, repository-relative project file that declares
direct ``historical_task -> replacement_task`` mappings, validates it exactly as strictly as
the in-file ``## Supersession`` grammar (:mod:`pipeline_core.supersession`), and resolves each
named task to its own, untouched task file — never reading or rewriting either side's
Markdown contract.

An invalid registry (malformed JSON, a self-mapping, a duplicate historical entry, a cyclic
pair, or a task ID that resolves to no known task file) fails closed to ``None``: production
reconciliation then finds no registry-declared replacement and retires no cards through this
path.

Standard library only (task-file/spec resolution is delegated to the existing portable
loaders).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from feature_pipeline.contracts import TaskSpec
from pipeline_core.supersession import SupersessionError, SupersessionGraph
from pipeline_core.task_files import TaskFileError, load_task_spec

__all__ = [
    "ReconciliationRegistryError",
    "RegistryMapping",
    "default_registry_path",
    "load_reconciliation_registry",
    "reconciliation_supersession_graph",
]


@dataclass(frozen=True)
class RegistryMapping:
    """One declared ``replacement`` supersedes ``superseded`` registry edge.

    ``source_run`` optionally binds the edge to exactly one named source run directory
    (under the runner's ``.pipeline/runs`` storage) — used when the replacement's own
    verified evidence would otherwise be ambiguous across more than one closed run. It
    names an identity for evidence resolution only; it is never itself mutated or used to
    rewrite a run's recorded bytes. Duck-type compatible with
    :class:`pipeline_core.supersession.Supersession` (``replacement``/``superseded``
    attributes) so it can be used directly as a :class:`SupersessionGraph` edge.
    """

    replacement: str
    superseded: str
    source_run: str | None = None


class ReconciliationRegistryError(Exception):
    """A legacy reconciliation registry record does not meet the contract. Fails closed."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def default_registry_path(repo_root: str | Path) -> Path:
    """The repository-relative project registry of direct legacy replacement mappings."""
    return Path(repo_root) / "tools" / "feature-pipeline" / "config" / "legacy_reconciliation_registry.json"


def load_reconciliation_registry(path: str | Path) -> tuple[RegistryMapping, ...]:
    """Parse the project registry file into ``(replacement, superseded)`` edges.

    A missing registry file declares no mappings. Any malformed record — the file is not a
    JSON object with a ``mappings`` array of ``{"historical_task", "replacement_task"}``
    string pairs, a self-mapping, a historical task named more than once, or a non-string /
    blank ``"source_run"`` — fails closed with a stable diagnostic code. ``source_run`` is
    optional; when present it names exactly one source run directory that resolves an
    otherwise ambiguous replacement's evidence.
    """
    registry_path = Path(path)
    if not registry_path.is_file():
        return ()
    try:
        raw = registry_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationRegistryError(
            f"legacy reconciliation registry '{registry_path}' is unreadable: {exc}",
            "reconciliation-registry-unreadable",
        ) from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("mappings"), list):
        raise ReconciliationRegistryError(
            f"legacy reconciliation registry '{registry_path}' must be an object with a "
            "'mappings' array",
            "reconciliation-registry-malformed",
        )
    seen_historical: set[str] = set()
    edges: list[RegistryMapping] = []
    for index, entry in enumerate(data["mappings"]):
        if not isinstance(entry, Mapping):
            raise ReconciliationRegistryError(
                f"legacy reconciliation registry mapping {index} is not an object",
                "reconciliation-registry-malformed",
            )
        historical = entry.get("historical_task")
        replacement = entry.get("replacement_task")
        if (
            not isinstance(historical, str) or not historical.strip()
            or not isinstance(replacement, str) or not replacement.strip()
        ):
            raise ReconciliationRegistryError(
                f"legacy reconciliation registry mapping {index} must declare non-empty "
                "'historical_task' and 'replacement_task' strings",
                "reconciliation-registry-malformed",
            )
        historical = historical.strip()
        replacement = replacement.strip()
        if historical == replacement:
            raise ReconciliationRegistryError(
                f"legacy reconciliation registry maps {historical} to itself",
                "reconciliation-registry-self",
            )
        if historical in seen_historical:
            raise ReconciliationRegistryError(
                f"legacy reconciliation registry declares '{historical}' more than once",
                "reconciliation-registry-duplicate",
            )
        seen_historical.add(historical)
        raw_source_run = entry.get("source_run")
        source_run: str | None
        if raw_source_run is None:
            source_run = None
        elif isinstance(raw_source_run, str) and raw_source_run.strip():
            source_run = raw_source_run.strip()
            if (
                "/" in source_run or "\\" in source_run
                or source_run in {".", ".."}
            ):
                raise ReconciliationRegistryError(
                    f"legacy reconciliation registry mapping {index} declares a "
                    "'source_run' that is not a bare run directory name",
                    "reconciliation-registry-malformed",
                )
        else:
            raise ReconciliationRegistryError(
                f"legacy reconciliation registry mapping {index} declares a 'source_run' "
                "that is not a non-empty string",
                "reconciliation-registry-malformed",
            )
        edges.append(RegistryMapping(
            replacement=replacement, superseded=historical, source_run=source_run,
        ))
    return tuple(edges)


def _resolve_task_file(repo_root: Path, task_id: str) -> Path | None:
    """The single task file that declares ``task_id`` under ``docs/plans/tasks``, or ``None``.

    Read-only lookup: the returned path's ``## Execution Metadata`` is only ever parsed, never
    written, so resolving a replacement's file here never touches its canonical contract.
    """
    tasks_dir = repo_root / "docs" / "plans" / "tasks"
    if not tasks_dir.is_dir():
        return None
    matches = sorted(tasks_dir.glob(f"{task_id}_*.md"))
    return matches[0] if len(matches) == 1 else None


def reconciliation_supersession_graph(
    repo_root: str | Path, *, path: str | Path | None = None,
) -> tuple[SupersessionGraph, Mapping[str, TaskSpec]] | None:
    """The validated registry graph and the ``TaskSpec`` for each task it names.

    Returns ``None`` when the registry declares no mappings, is itself invalid, malformed,
    cyclic, ambiguous, or names a task ID that resolves to no known task file — a broken
    registry must reconcile nothing rather than reconcile a wrong pairing. Every resolved
    task file is only ever read; no path this function touches is written.
    """
    root = Path(repo_root)
    registry_path = Path(path) if path is not None else default_registry_path(root)
    try:
        edges = load_reconciliation_registry(registry_path)
    except ReconciliationRegistryError:
        return None
    if not edges:
        return None

    referenced_ids: set[str] = set()
    for edge in edges:
        referenced_ids.add(edge.replacement)
        referenced_ids.add(edge.superseded)

    file_by_id: dict[str, Path] = {}
    for task_id in referenced_ids:
        resolved = _resolve_task_file(root, task_id)
        if resolved is not None:
            file_by_id[task_id] = resolved

    try:
        graph = SupersessionGraph(edges, known_ids=file_by_id)
    except SupersessionError:
        return None

    definitions: dict[str, TaskSpec] = {}
    for task_id, task_path in file_by_id.items():
        try:
            definitions[task_id] = load_task_spec(task_path)
        except TaskFileError:
            return None
    return graph, definitions
