"""Portable verifier verdict and evidence contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from .adapters import Adapter, LaunchRequest, LaunchResult


VERDICTS = frozenset({"PASS", "FAIL", "BLOCKED"})


class VerificationError(ValueError):
    """A verifier response that cannot be trusted."""


@dataclass(frozen=True)
class VerificationEvidence:
    """Runner-captured facts supplied equally to independent verifiers."""

    task_id: str
    attempt: int
    commands: tuple[Mapping[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {"task_id": self.task_id, "attempt": self.attempt, "commands": list(self.commands)}


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
