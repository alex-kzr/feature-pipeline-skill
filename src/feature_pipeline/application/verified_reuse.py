"""Read-only lookup for independently verified task evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.models import TaskDefinition


# ``rec09-v1`` digests were persisted before the normalized work-item metadata was part of
# the reusable-evidence boundary.  Keep their wire format readable, but never mistake them
# for the versioned evidence newly emitted by this release.
LEGACY_CANONICAL_CONTRACT_VERSION = "rec09-v1"
CANONICAL_CONTRACT_VERSION = "rec09-v2"
_SUPPORTED_CONTRACT_VERSIONS = frozenset({
    LEGACY_CANONICAL_CONTRACT_VERSION,
    CANONICAL_CONTRACT_VERSION,
})


class EvidenceEligibilityError(Exception):
    """A source run cannot safely provide reusable verification evidence."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalTaskContract:
    """The complete executable identity eligible for verified-evidence reuse.

    This is deliberately built from the validated ``TaskSpec`` rather than Markdown bytes:
    runner-owned ``Status``, ``Result``, and ``Blockers`` projections are presentation only.
    """

    id: str
    task_type: str
    executor: str
    depends_on: tuple[str, ...]
    allowed_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    required_skills: tuple[str, ...]
    max_repair_attempts: int
    documentation_impact: tuple[str, ...]
    verification_commands: tuple[tuple[str, tuple[str, ...]], ...]
    verification_tier: str
    accepts_scoped: tuple[str, ...]
    deferred_verification_commands: tuple[tuple[str, tuple[str, ...]], ...]
    runner_evidence: str | None
    blocking_conditions: str | None
    preconditions: tuple[tuple[str, str], ...]
    acceptance_criteria: tuple[tuple[str, str], ...]
    supersedes: tuple[str, ...]

    @classmethod
    def from_definition(cls, definition: TaskDefinition | TaskSpec) -> "CanonicalTaskContract":
        """Create the deterministic execution identity from one validated definition."""
        spec = definition.spec if isinstance(definition, TaskDefinition) else definition
        source_path = Path(definition.source_path if isinstance(definition, TaskDefinition) else definition.path)
        # JSON tasks have no task-file declaration.  A native Markdown task must remain
        # available while its contract is identified; a missing file cannot be guessed.
        if source_path.is_file():
            from pipeline_core.task_files import parse_supersession_declarations
            supersedes = parse_supersession_declarations(source_path)
        else:
            supersedes = ()
        return cls(
            id=definition.id,
            task_type=definition.task_type,
            executor=definition.executor,
            depends_on=tuple(sorted(definition.depends_on)),
            allowed_scope=tuple(sorted(definition.allowed_scope)),
            out_of_scope=tuple(sorted(definition.out_of_scope)),
            required_skills=tuple(sorted(definition.required_skills)),
            max_repair_attempts=definition.max_repair_attempts,
            documentation_impact=tuple(sorted(definition.documentation_impact)),
            verification_commands=tuple(
                (command.cwd, tuple(command.argv)) for command in definition.verification_commands
            ),
            verification_tier=definition.verification_tier,
            accepts_scoped=tuple(sorted(definition.accepts_scoped)),
            deferred_verification_commands=tuple(
                (command.cwd, tuple(command.argv))
                for command in definition.deferred_verification_commands
            ),
            runner_evidence=spec.runner_evidence,
            blocking_conditions=definition.blocking_conditions,
            preconditions=tuple(sorted(
                (item.kind, item.value) for item in definition.preconditions
            )),
            acceptance_criteria=tuple(
                (criterion.id, criterion.text) for criterion in definition.acceptance_criteria
            ),
            supersedes=supersedes,
        )

    def as_mapping(self) -> dict[str, object]:
        """Return a JSON-safe diagnostic view of this typed contract."""
        return asdict(self)


def canonical_task_contract(definition: TaskDefinition | TaskSpec) -> dict[str, object]:
    """Return the diagnostic mapping of the typed reusable task contract."""
    return CanonicalTaskContract.from_definition(definition).as_mapping()


def task_contract_digest(
    definition: TaskDefinition | TaskSpec, *, version: str = CANONICAL_CONTRACT_VERSION,
) -> str:
    """Return the versioned stable SHA-256 identity of a reusable task contract.

    V1 is deliberately byte-compatible with historical persisted digests.  V2 adds an
    unambiguous version domain to the digest, so new evidence cannot be replayed as legacy
    evidence (or vice versa) when task metadata evolves again.
    """
    if version not in _SUPPORTED_CONTRACT_VERSIONS:
        raise ValueError(f"unsupported canonical contract version: {version}")
    contract = CanonicalTaskContract.from_definition(definition).as_mapping()
    payload: object = contract
    if version != LEGACY_CANONICAL_CONTRACT_VERSION:
        payload = {"canonical_contract_version": version, "contract": contract}
    encoded = json.dumps(
        payload,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def canonical_task_path(definition: TaskDefinition | TaskSpec, repo_root: str | Path) -> str:
    """Return the definition's repository-relative task file path."""
    source = Path(definition.source_path if isinstance(definition, TaskDefinition) else definition.path)
    root = Path(repo_root).resolve()
    if source.is_absolute():
        try:
            return source.resolve().relative_to(root).as_posix()
        except ValueError:
            raise EvidenceEligibilityError(
                f"task path '{source}' is outside the repository", "evidence-task-path-escape"
            ) from None
    return source.as_posix().removeprefix("./")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VerifiedEvidenceStore:
    """Find reusable evidence without changing any source run artifact."""

    def __init__(self, runs_root: str | Path, repo_root: str | Path) -> None:
        self.runs_root = Path(runs_root)
        self.repo_root = Path(repo_root)

    def find(self, definition: TaskDefinition) -> Mapping[str, str]:
        """Return one eligible evidence record, or fail closed with a stable diagnostic."""
        sources = self._sources()
        if not sources:
            raise EvidenceEligibilityError("no source run exists", "evidence-source-missing")

        exact: list[Mapping[str, str]] = []
        denials: list[EvidenceEligibilityError] = []
        expected_path = canonical_task_path(definition, self.repo_root)
        for path, raw, data in sources:
            try:
                task = self._eligible_task(data, definition.id)
                version = self._canonical_identity(task, definition, expected_path)
                exact.append(self._evidence(
                    path, raw, data, task, definition.id,
                    self._identity_name(version), version,
                ))
            except EvidenceEligibilityError as exc:
                denials.append(exc)

        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise EvidenceEligibilityError(
                "multiple source runs match the task path and contract digest", "evidence-exact-ambiguous"
            )
        if denials:
            raise denials[0]
        raise EvidenceEligibilityError(
            f"no source task matches '{definition.id}'", "evidence-source-task-missing"
        )

    def find_at(self, run_dir: str | Path, definition: TaskDefinition) -> Mapping[str, str]:
        """Evaluate one explicitly selected source run with the default lookup policy."""
        path, raw, data = self._read_source(Path(run_dir) / "run.json")
        task = self._eligible_task(data, definition.id)
        version = self._canonical_identity(
            task, definition, canonical_task_path(definition, self.repo_root)
        )
        return self._evidence(
            path, raw, data, task, definition.id, self._identity_name(version), version
        )

    @staticmethod
    def _identity_name(version: str) -> str:
        if version == LEGACY_CANONICAL_CONTRACT_VERSION:
            return "legacy-task-path-and-contract-digest"
        return "task-path-and-contract-digest"

    @staticmethod
    def _canonical_identity(
        task: Mapping[str, Any], definition: TaskDefinition, expected_path: str,
    ) -> str:
        """Validate the source identity using its declared digest format, never a guess."""
        version = task.get("task_contract_version")
        if not isinstance(version, str) or version not in _SUPPORTED_CONTRACT_VERSIONS:
            raise EvidenceEligibilityError(
                "source task has no recognized canonical contract version",
                "evidence-canonical-identity-missing",
            )
        if task.get("task_path") != expected_path:
            raise EvidenceEligibilityError(
                "source task path does not match", "evidence-task-path-mismatch"
            )
        if task.get("task_contract_digest") != task_contract_digest(definition, version=version):
            raise EvidenceEligibilityError(
                "source task contract digest does not match", "evidence-contract-digest-mismatch"
            )
        return version

    def _sources(self) -> list[tuple[Path, bytes, Mapping[str, Any]]]:
        if not self.runs_root.exists():
            return []
        sources: list[tuple[Path, bytes, Mapping[str, Any]]] = []
        for path in sorted(self.runs_root.glob("*/run.json")):
            sources.append(self._read_source(path))
        return sources

    @staticmethod
    def _read_source(path: Path) -> tuple[Path, bytes, Mapping[str, Any]]:
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
        except FileNotFoundError as exc:
            raise EvidenceEligibilityError("source run does not exist", "evidence-source-missing") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceEligibilityError(
                f"source run '{path.parent.name}' is unreadable", "evidence-source-unreadable"
            ) from exc
        if not isinstance(data, dict) or data.get("schema_version") not in (2, 3):
            raise EvidenceEligibilityError(
                f"source run '{path.parent.name}' has an unsupported schema", "evidence-unsupported-schema"
            )
        return path, raw, data

    #: Terminal run states whose per-task evidence may still be reused. ``verified`` is a
    #: globally closed run; ``blocked`` is a terminal run that could not be globally closed
    #: (e.g. a sibling task needs human recovery). Reuse from a ``blocked`` source is only
    #: ever granted when the *named task itself* clears every check below — the run is never
    #: treated as successful and its bytes are never touched.
    _TERMINAL_RUN_STATES = ("verified", "blocked", "completed")

    @classmethod
    def _eligible_task(cls, data: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
        if data.get("status") not in cls._TERMINAL_RUN_STATES:
            raise EvidenceEligibilityError("source run is not closed", "evidence-source-run-not-closed")
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise EvidenceEligibilityError("source run tasks are unreadable", "evidence-source-unreadable")
        task = next((item for item in tasks if isinstance(item, dict) and item.get("id") == task_id), None)
        if task is None:
            raise EvidenceEligibilityError("source task is absent", "evidence-source-task-missing")
        legacy_verified = task.get("status") == "verified"
        completed = task.get("status") == "done" and task.get("resolution") == "completed"
        if not (legacy_verified or completed):
            raise EvidenceEligibilityError("source task is not verified", "evidence-source-task-not-verified")
        verification = task.get("verification")
        if not isinstance(verification, dict):
            raise EvidenceEligibilityError("source verdict evidence is missing", "evidence-verdict-missing")
        if verification.get("task_verdict") != "PASS":
            raise EvidenceEligibilityError("source task verifier did not PASS", "evidence-task-verdict-not-pass")
        if verification.get("test_verdict") != "PASS":
            raise EvidenceEligibilityError("source test verifier did not PASS", "evidence-test-verdict-not-pass")
        if not isinstance(verification.get("verified_at"), str):
            raise EvidenceEligibilityError("source verification time is missing", "evidence-verified-at-missing")
        return task

    @staticmethod
    def _evidence(
        path: Path, raw: bytes, data: Mapping[str, Any], task: Mapping[str, Any], dependency_id: str,
        identity: str, evidence_contract_version: str,
    ) -> Mapping[str, str]:
        verification = task["verification"]
        assert isinstance(verification, Mapping)
        run_id = data.get("run_id")
        if not isinstance(run_id, str):
            raise EvidenceEligibilityError("source run ID is missing", "evidence-source-run-id-missing")
        return MappingProxyType({
            "dependency_id": dependency_id,
            "source_run_id": run_id,
            "source_run_digest": f"sha256:{hashlib.sha256(raw).hexdigest()}",
            "evidence_identity": identity,
            "evidence_contract_version": evidence_contract_version,
            "evidence_digest_version": evidence_contract_version,
            "task_verdict": "PASS",
            "test_verdict": "PASS",
            "verified_at": str(verification["verified_at"]),
            "reused_at": _now(),
        })


def supersession_graph(definitions: Mapping[str, Any], repo_root: str | Path) -> Any | None:
    """Rebuild the plan's supersession relation from the task files' canonical
    ``## Supersession`` grammar, or ``None`` when nothing declares one.

    Read-only: it only re-parses task files already on disk (the same grammar
    :class:`CanonicalTaskContract` folds into the reuse digest). Fails closed to ``None`` on
    an invalid / cyclic / ambiguous declaration set — the plan loader already rejected those
    before a run reaches here, so a malformed declaration can never *grant* a reuse.
    """
    from pipeline_core.supersession import Supersession, SupersessionError, SupersessionGraph
    from pipeline_core.task_files import parse_supersession_declarations

    root = Path(repo_root)
    edges: list[Any] = []
    for task_id, definition in definitions.items():
        source = Path(
            getattr(definition, "source_path", None) or getattr(definition, "path", "")
        )
        if not source.is_absolute():
            source = root / source
        if not source.is_file():
            continue
        for superseded in parse_supersession_declarations(source):
            edges.append(Supersession(replacement=task_id, superseded=superseded))
    if not edges:
        return None
    try:
        return SupersessionGraph(
            edges,
            known_ids=list(definitions),
            dependencies={
                tid: list(getattr(d, "depends_on", ())) for tid, d in definitions.items()
            },
        )
    except SupersessionError:
        return None


def find_superseding_evidence(
    store: "VerifiedEvidenceStore",
    graph: Any | None,
    superseded_id: str,
    definitions: Mapping[str, Any],
) -> tuple[str, Mapping[str, str]] | None:
    """``(replacement_id, evidence)`` for the task that directly supersedes
    ``superseded_id`` and carries its *own* exact eligible verified evidence.

    Returns ``None`` when no direct replacement is declared or it lacks eligible evidence.
    A later successor cannot validate an unfinished intermediate replacement: every retired
    card must have a formal edge backed by that edge's replacement evidence. The blocked
    predecessor itself is never read, forged, or modified.
    """
    if graph is None:
        return None
    node = graph.replacement_for(superseded_id)
    if node is None:
        return None
    definition = definitions.get(node)
    if definition is None:
        return None
    try:
        return node, store.find(definition)
    except EvidenceEligibilityError:
        return None


def resolve_default_reuse(
    store: "VerifiedEvidenceStore",
    definitions: Mapping[str, Any],
    scope: tuple[str, ...] | list[str],
    selected: tuple[str, ...] | list[str],
    repo_root: str | Path,
    pre_resolved: tuple[str, ...] | list[str] = (),
) -> dict[str, Mapping[str, str]]:
    """Resolve default dependency reuse once for preview and execution.

    A replacement is evidence for the *declared* dependency, never evidence that rewrites the
    predecessor.  Once a replacement edge exists, failure to validate its evidence is a denial
    rather than permission to redispatch a retired task.
    """
    graph = supersession_graph(definitions, repo_root)
    if graph is None:
        # ``supersession_graph`` deliberately has a permissive public shape for legacy callers.
        # Default reuse cannot be permissive: an invalid declared replacement relation must not
        # fall through to dispatching the terminal predecessor.
        from pipeline_core.task_files import parse_supersession_declarations
        root = Path(repo_root)
        has_declaration = any(
            parse_supersession_declarations(
                path if path.is_absolute() else root / path
            )
            for definition in definitions.values()
            for path in (Path(getattr(definition, "source_path", getattr(definition, "path", "")),),)
            if (path if path.is_absolute() else root / path).is_file()
        )
        if has_declaration:
            raise EvidenceEligibilityError(
                "supersession declarations are invalid or cyclic", "evidence-supersession-invalid"
            )
    reused: dict[str, Mapping[str, str]] = {}
    selected_ids = set(selected)
    pre_resolved_ids = set(pre_resolved)
    for task_id in scope:
        if task_id in selected_ids or task_id in pre_resolved_ids:
            continue
        try:
            reused[task_id] = store.find(definitions[task_id])
            continue
        except EvidenceEligibilityError as direct_denial:
            if graph is None:
                continue
            replacement_id = graph.replacement_for(task_id)
            if replacement_id is None:
                continue
            replacement = definitions.get(replacement_id)
            if replacement is None:
                raise direct_denial
            evidence = dict(store.find(replacement))
            evidence["dependency_id"] = task_id
            evidence["replacement_id"] = replacement_id
            reused[task_id] = MappingProxyType(evidence)
    return reused


__all__ = [
    "CANONICAL_CONTRACT_VERSION", "LEGACY_CANONICAL_CONTRACT_VERSION", "CanonicalTaskContract", "EvidenceEligibilityError", "VerifiedEvidenceStore",
    "canonical_task_contract",
    "canonical_task_path", "find_superseding_evidence", "resolve_default_reuse", "supersession_graph", "task_contract_digest",
]
