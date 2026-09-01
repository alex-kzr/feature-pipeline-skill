"""Portable verifier verdict and evidence contracts.

The runner — never an executor or a verifier — is the authority on which verification
commands actually ran and what they returned. :func:`build_verification_evidence` packages
that runner-owned command evidence together with the task/attempt identity and the exact
implementation manifest/diff/report references into one immutable :class:`VerificationEvidence`.
:func:`verifier_evidence_payload` serializes it once so the task-verifier and the
test-verifier request builders are handed byte-identical facts.

Command *execution* lives in :mod:`pipeline_core.commands`; this module only packages the
result and interprets verdicts — it never launches a command itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .adapters import Adapter, LaunchRequest, LaunchResult
from .commands import VerificationRun


VERDICTS = frozenset({"PASS", "FAIL", "BLOCKED"})

#: The command-record keys carried into the evidence payload. Redacted, budgeted stdout/stderr
#: text stays in the persisted log files; the payload references those logs, it never inlines
#: their content (Implementation Notes).
_COMMAND_REFERENCE_KEYS = (
    "id", "stage", "cwd", "argv", "exit_code", "disposition", "duration",
    "stdout_log", "stderr_log", "reason", "command_index", "task_id", "attempt",
)


class VerificationError(ValueError):
    """A verifier response that cannot be trusted."""


def command_reference(record: Mapping[str, object]) -> dict[str, object]:
    """Project one persisted command record to the fields the evidence payload carries."""
    return {key: record[key] for key in _COMMAND_REFERENCE_KEYS if key in record}


def _normalize_cwd(value: object) -> str:
    text = str(value or ".").replace("\\", "/")
    text = text[2:] if text.startswith("./") else text
    return text or "."


def _claim_cwd_argv(claim: object) -> tuple[str, tuple[str, ...]]:
    cwd = getattr(claim, "cwd", None)
    argv = getattr(claim, "argv", None)
    if argv is None and isinstance(claim, Mapping):
        cwd = claim.get("cwd")
        argv = claim.get("argv")
        if argv is None and claim.get("command"):
            argv = str(claim["command"]).split()
    if isinstance(argv, str):
        argv = (argv,)
    return _normalize_cwd(cwd), tuple(str(part) for part in (argv or ()))


def missing_command_evidence(
    claimed_checks: Iterable[object], command_records: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], ...]:
    """Claimed executor checks with no matching runner-recorded command.

    A match is an exact ``(cwd, argv)`` equality against a recorded command. Anything a
    verifier can only assert on the executor's say-so is returned here so the test verifier
    can fail on it without running a tool.
    """
    recorded = {
        (_normalize_cwd(record.get("cwd")), tuple(str(part) for part in record.get("argv", ())))
        for record in command_records
    }
    missing: list[dict[str, object]] = []
    for claim in claimed_checks:
        cwd, argv = _claim_cwd_argv(claim)
        if (cwd, argv) not in recorded:
            missing.append(
                {
                    "cwd": cwd,
                    "argv": list(argv),
                    "reason": "executor claimed this check but there is no runner-recorded command",
                }
            )
    return tuple(missing)


@dataclass(frozen=True)
class VerificationEvidence:
    """Runner-captured facts supplied, byte-for-byte, to both independent verifiers.

    Immutable by construction: a frozen dataclass over tuples. ``commands`` are record
    *references* (they name the persisted redacted logs, they do not inline them);
    ``unrun_commands`` and ``external_blocker`` keep a partial pass explicit rather than
    letting it read as complete.
    """

    task_id: str
    attempt: int
    commands: tuple[Mapping[str, object], ...] = ()
    executor_report: str | None = None
    implementation_manifest: str | None = None
    implementation_diff: str | None = None
    changed_files: tuple[Mapping[str, object], ...] = ()
    missing_evidence: tuple[Mapping[str, object], ...] = ()
    unrun_commands: tuple[Mapping[str, object], ...] = ()
    external_blocker: str | None = None

    @property
    def complete(self) -> bool:
        """Every declared check produced a command record and nothing blocked the pass."""
        return self.external_blocker is None and not self.unrun_commands

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "executor_report": self.executor_report,
            "implementation": {
                "manifest": self.implementation_manifest,
                "diff": self.implementation_diff,
                "changed_files": [dict(entry) for entry in self.changed_files],
            },
            "commands": [dict(entry) for entry in self.commands],
            "missing_evidence": [dict(entry) for entry in self.missing_evidence],
            "unrun_commands": [dict(entry) for entry in self.unrun_commands],
            "external_blocker": self.external_blocker,
            "complete": self.complete,
        }

    def serialized(self) -> str:
        """Deterministic canonical JSON — the exact bytes both verifier builders embed."""
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


def build_verification_evidence(
    run: object,
    task_id: str,
    *,
    attempt: int,
    commands_run: VerificationRun,
    claimed_checks: Iterable[object] = (),
) -> VerificationEvidence:
    """Assemble the immutable evidence package for one task attempt.

    ``commands_run`` is the ordered result of
    :func:`pipeline_core.commands.run_verification_commands`; the implementation
    manifest/diff/report references are read from the task's already-recorded execution
    evidence. This function runs nothing — it only packages runner-owned facts.
    """
    record = run.task(task_id)
    execution = record.execution_evidence or {}
    implementation = execution.get("implementation") or {}
    references = tuple(command_reference(item) for item in commands_run.records)
    unrun = tuple(
        {
            "cwd": cwd,
            "argv": list(argv),
            "reason": "declared check not executed: the pass halted on an external blocker",
        }
        for cwd, argv in commands_run.unrun
    )
    return VerificationEvidence(
        task_id=task_id,
        attempt=attempt,
        commands=references,
        executor_report=execution.get("executor_report"),
        implementation_manifest=implementation.get("manifest"),
        implementation_diff=implementation.get("diff"),
        changed_files=tuple(dict(entry) for entry in implementation.get("changed_files") or ()),
        missing_evidence=missing_command_evidence(claimed_checks, commands_run.records),
        unrun_commands=unrun,
        external_blocker=commands_run.stopped_reason,
    )


def verifier_evidence_payload(evidence: VerificationEvidence) -> str:
    """Serialize the evidence once. Both verifier request builders embed exactly this string;
    a caller must never re-serialize per role, so the two verifiers cannot diverge (AC-3)."""
    return evidence.serialized()


def evidence_forces_fail(evidence: VerificationEvidence) -> str | None:
    """A fact-only reason the test verifier returns ``FAIL`` without running any tool, or
    ``None``. Set when the executor claimed a check the runner has no command record for —
    an unbacked claim is a failure, never a prompt to re-run the check."""
    if not evidence.missing_evidence:
        return None
    first = evidence.missing_evidence[0]
    argv = " ".join(str(part) for part in first.get("argv", ()))
    return (
        f"claimed check '{first.get('cwd', '.')} -> {argv}' has no runner-recorded command; "
        f"an unbacked claim is a FAIL"
    )


@dataclass(frozen=True)
class VerificationOutcome:
    """The two independent verifier results for one task attempt."""

    task_verdict: str
    test_verdict: str
    task_result: LaunchResult
    test_result: LaunchResult

    @property
    def status(self) -> str:
        if "BLOCKED" in (self.task_verdict, self.test_verdict):
            return "blocked"
        if "FAIL" in (self.task_verdict, self.test_verdict):
            return "verification_failed"
        return "verified"


def parse_verdict(text: str) -> str:
    """Parse one exact JSON verdict envelope; prose is never treated as a verdict."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise VerificationError("verdict must be a JSON object") from error
    if not isinstance(payload, dict) or set(payload) != {"verdict"}:
        raise VerificationError("verdict envelope must contain only 'verdict'")
    verdict = payload["verdict"]
    if verdict not in VERDICTS:
        raise VerificationError("verdict must be PASS, FAIL, or BLOCKED")
    return verdict


def verify_independently(
    adapter: Adapter, evidence: VerificationEvidence, task_request: LaunchRequest, test_request: LaunchRequest,
) -> VerificationOutcome:
    """Run two explicitly read-only requests without sharing verifier output between them."""
    if not task_request.read_only or not test_request.read_only:
        raise VerificationError("independent verifiers must use read-only requests")
    if task_request.task_id != evidence.task_id or test_request.task_id != evidence.task_id:
        raise VerificationError("verifier task IDs must match the evidence task")
    task_result = adapter.launch(task_request)
    test_result = adapter.launch(test_request)
    if task_result.exit_code != 0 or test_result.exit_code != 0:
        raise VerificationError("verifier launch failed")
    return VerificationOutcome(
        parse_verdict(task_result.stdout), parse_verdict(test_result.stdout), task_result, test_result,
    )
