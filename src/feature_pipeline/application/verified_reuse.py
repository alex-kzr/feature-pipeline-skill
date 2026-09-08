"""Read-only lookup for independently verified task evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.models import TaskDefinition


class EvidenceEligibilityError(Exception):
    """A source run cannot safely provide reusable verification evidence."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_task_contract(definition: TaskDefinition | TaskSpec) -> dict[str, object]:
    """Return the input-neutral fields that define reusable task evidence."""
    contract: dict[str, object] = {
        "id": definition.id,
        "depends_on": sorted(definition.depends_on),
        "allowed_scope": sorted(definition.allowed_scope),
        "out_of_scope": sorted(definition.out_of_scope),
        "acceptance_criteria": [
            {"id": criterion.id, "text": criterion.text}
            for criterion in definition.acceptance_criteria
        ],
        "verification_commands": [
            {"cwd": command.cwd, "argv": list(command.argv)}
            for command in definition.verification_commands
        ],
        "verification_tier": definition.verification_tier,
    }
    preconditions = getattr(definition, "preconditions", ())
    if preconditions:
        contract["preconditions"] = [
            {"kind": item.kind, "value": item.value} for item in preconditions
        ]
    return contract


def task_contract_digest(definition: TaskDefinition | TaskSpec) -> str:
    """Return the stable SHA-256 identity of ``definition``'s reusable contract."""
    encoded = json.dumps(
        canonical_task_contract(definition), sort_keys=True, separators=(",", ":"), ensure_ascii=False
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
        legacy: list[Mapping[str, str]] = []
        denials: list[EvidenceEligibilityError] = []
        expected_path = canonical_task_path(definition, self.repo_root)
        expected_digest = task_contract_digest(definition)
        for path, raw, data in sources:
            try:
                task = self._eligible_task(data, definition.id)
                digest = task.get("task_contract_digest")
                task_path = task.get("task_path")
                if digest is None:
                    if getattr(definition, "preconditions", ()):
                        raise EvidenceEligibilityError(
                            "legacy evidence has no precondition contract", "evidence-contract-digest-mismatch"
                        )
                    legacy.append(self._evidence(path, raw, data, task, definition.id, "legacy-task-id"))
                elif task_path != expected_path:
                    raise EvidenceEligibilityError(
                        "source task path does not match", "evidence-task-path-mismatch"
                    )
                elif digest != expected_digest:
                    raise EvidenceEligibilityError(
                        "source task contract digest does not match", "evidence-contract-digest-mismatch"
                    )
                else:
                    exact.append(self._evidence(
                        path, raw, data, task, definition.id, "task-path-and-contract-digest"
                    ))
            except EvidenceEligibilityError as exc:
                denials.append(exc)

        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise EvidenceEligibilityError(
                "multiple source runs match the task path and contract digest", "evidence-exact-ambiguous"
            )
        if len(legacy) == 1:
            return legacy[0]
        if len(legacy) > 1:
            raise EvidenceEligibilityError(
                "multiple legacy source runs match the task ID", "evidence-legacy-ambiguous"
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
        digest = task.get("task_contract_digest")
        if digest is None:
            if getattr(definition, "preconditions", ()):
                raise EvidenceEligibilityError(
                    "legacy evidence has no precondition contract", "evidence-contract-digest-mismatch"
                )
            legacy_matches = 0
            for _, _, candidate in self._sources():
                try:
                    candidate_task = self._eligible_task(candidate, definition.id)
                except EvidenceEligibilityError:
                    continue
                if candidate_task.get("task_contract_digest") is None:
                    legacy_matches += 1
            if legacy_matches > 1:
                raise EvidenceEligibilityError(
                    "multiple legacy source runs match the task ID", "evidence-legacy-ambiguous"
                )
            return self._evidence(path, raw, data, task, definition.id, "legacy-task-id")
        if task.get("task_path") != canonical_task_path(definition, self.repo_root):
            raise EvidenceEligibilityError(
                "source task path does not match", "evidence-task-path-mismatch"
            )
        if digest != task_contract_digest(definition):
            raise EvidenceEligibilityError(
                "source task contract digest does not match", "evidence-contract-digest-mismatch"
            )
        return self._evidence(path, raw, data, task, definition.id, "task-path-and-contract-digest")

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

    @staticmethod
    def _eligible_task(data: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
        if data.get("status") != "verified":
            raise EvidenceEligibilityError("source run is not closed", "evidence-source-run-not-closed")
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise EvidenceEligibilityError("source run tasks are unreadable", "evidence-source-unreadable")
        task = next((item for item in tasks if isinstance(item, dict) and item.get("id") == task_id), None)
        if task is None:
            raise EvidenceEligibilityError("source task is absent", "evidence-source-task-missing")
        if task.get("status") != "verified":
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
        identity: str,
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
            "task_verdict": "PASS",
            "test_verdict": "PASS",
            "verified_at": str(verification["verified_at"]),
            "reused_at": _now(),
        })


__all__ = [
    "EvidenceEligibilityError", "VerifiedEvidenceStore", "canonical_task_contract",
    "canonical_task_path", "task_contract_digest",
]
