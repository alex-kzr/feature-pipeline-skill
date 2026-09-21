"""Load runner-owned, durable strict-isolation probe records."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from feature_pipeline.ports.adapters import (
    STRICT_ISOLATION_CAPABILITIES,
    AdapterCapabilities,
)


class IsolationProbeRecordError(ValueError):
    """A durable probe record is malformed or has failed verification."""


@dataclass(frozen=True)
class DeterministicIsolationVerdict:
    """A runner-owned verdict for the strict-isolation boundary only."""

    token: str
    report: dict[str, object]


def _runtime_identity(executable: str | Sequence[str]) -> str:
    return executable if isinstance(executable, str) else "\0".join(executable)


def _approved_record_path(project_dir: Path, name: str) -> Path:
    return (project_dir.resolve() / ".pipeline" / "isolation-probes" / f"{name}.json")


def _assert_runner_owned_location(path: Path, *, project_dir: Path, name: str) -> None:
    """Accept only the runner's canonical record file, never a caller-selected path."""
    approved = _approved_record_path(project_dir, name)
    try:
        actual = path.resolve(strict=True)
    except OSError as exc:
        raise IsolationProbeRecordError("isolation probe record is unavailable") from exc
    if actual != approved or path.is_symlink() or not actual.is_file():
        raise IsolationProbeRecordError("isolation probe record is not runner-owned")
    if hasattr(os, "getuid") and actual.stat().st_uid != os.getuid():
        raise IsolationProbeRecordError("isolation probe record is not runner-owned")


def _observed_version(executable: str | Sequence[str]) -> str:
    argv = [executable] if isinstance(executable, str) else list(executable)
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise IsolationProbeRecordError("isolation executable is unavailable")
    try:
        completed = subprocess.run(
            [*argv, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="strict", timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise IsolationProbeRecordError("isolation executable version is unavailable") from exc
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        raise IsolationProbeRecordError("isolation executable version is unavailable")
    return version


def unavailable_isolation_capabilities(
    name: str, *, available: bool, supports_resume: bool, supports_read_only: bool,
    supports_write: bool, default_timeout_s: float = 3600.0,
) -> AdapterCapabilities:
    """Return the fail-closed baseline used when no durable record exists."""
    return AdapterCapabilities(
        name, available, supports_resume, supports_read_only, supports_write, default_timeout_s
    )


def load_isolation_capabilities(
    path: Path,
    *,
    name: str,
    available: bool,
    supports_resume: bool,
    supports_read_only: bool,
    supports_write: bool,
    default_timeout_s: float = 3600.0,
) -> AdapterCapabilities:
    """Load one verified runner record, or return no strict capabilities when absent.

    Records live only below the runner-owned probe directory.  The current runner probe is
    diagnostic-only: it observes a private-marker non-leak, but does not attempt forbidden
    sibling/outside-workspace reads or command execution under the executor's exact controls.
    Consequently, even a well-formed passed observation returns the fail-closed baseline.
    """
    baseline = unavailable_isolation_capabilities(
        name, available=available, supports_resume=supports_resume,
        supports_read_only=supports_read_only, supports_write=supports_write,
        default_timeout_s=default_timeout_s,
    )
    if not path.is_file():
        return baseline
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IsolationProbeRecordError("isolation probe record is unreadable") from exc
    if not isinstance(raw, dict):
        raise IsolationProbeRecordError("isolation probe record must be an object")
    required = ("schema_version", "runner_owned", "verification_status", "adapter",
                "executable", "cli_surface", "observed_version", "evidence",
                "evidence_digest", "capabilities")
    if any(key not in raw for key in required):
        raise IsolationProbeRecordError("isolation probe record is incomplete")
    evidence = raw["evidence"]
    capabilities = raw["capabilities"]
    if (
        raw["schema_version"] != 1 or raw["runner_owned"] is not True
        or raw["verification_status"] != "PASS" or raw["adapter"] != name
        or not all(isinstance(raw[key], str) and raw[key].strip() for key in
                   ("executable", "cli_surface", "observed_version", "evidence", "evidence_digest"))
        or not isinstance(capabilities, list)
        or any(token not in STRICT_ISOLATION_CAPABILITIES for token in capabilities)
        or len(set(capabilities)) != len(capabilities)
        or hashlib.sha256(evidence.encode("utf-8")).hexdigest() != raw["evidence_digest"]
    ):
        raise IsolationProbeRecordError("isolation probe record failed validation")
    return baseline


@dataclass(frozen=True)
class ProductionIsolationProbe:
    """Revalidate a canonical runner record against the executable about to launch."""

    project_dir: Path
    name: str
    available: bool
    supports_resume: bool
    supports_read_only: bool
    supports_write: bool

    def validate(
        self, executable: str | Sequence[str], cli_surface: str,
    ) -> AdapterCapabilities:
        path = _approved_record_path(self.project_dir, self.name)
        _assert_runner_owned_location(path, project_dir=self.project_dir, name=self.name)
        capabilities = load_isolation_capabilities(
            path, name=self.name, available=self.available,
            supports_resume=self.supports_resume, supports_read_only=self.supports_read_only,
            supports_write=self.supports_write,
        )
        if (
            capabilities.runtime != _runtime_identity(executable)
            or capabilities.cli_surface != cli_surface
            or capabilities.observed_version != _observed_version(executable)
        ):
            raise IsolationProbeRecordError("isolation probe record does not match executable")
        return capabilities


@dataclass(frozen=True)
class DeterministicIsolationVerifier:
    """Report why the current diagnostic probe cannot replace either verifier.

    Kept as an explicit contract boundary for callers that request a deterministic result;
    it never turns a clean probe observation into a verifier PASS.
    """

    project_dir: Path
    adapter: str

    def verify(self, *, task_id: str, role: str) -> DeterministicIsolationVerdict:
        if task_id != "TC-11":
            return DeterministicIsolationVerdict(
                "FAIL", {"reason": "deterministic-isolation-verifier-not-applicable"}
            )
        return DeterministicIsolationVerdict(
            "FAIL", {"task_id": task_id, "role": role, "adapter": self.adapter,
                     "reason": "probe-not-executor-equivalent"}
        )
