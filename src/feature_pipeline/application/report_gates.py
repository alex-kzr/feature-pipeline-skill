"""Deterministic mechanical gates over the report protocol (VP-02).

The task requires that *command exit*, *evidence presence*, *scope*, and *protocol validity*
be **application decisions**, not something a verifier judges by eye. Each gate here is a pure
function: it takes runner-captured facts and returns a typed
:class:`~feature_pipeline.reports.findings.Outcome` (a
:class:`~feature_pipeline.reports.dispositions.Disposition` plus
:class:`~feature_pipeline.reports.findings.Finding` rows). No gate runs a subprocess, reads a
clock, or mutates state.

The independent task/test verifiers still produce their own verdicts and both must ``PASS``
(:func:`both_verifiers_pass`); these gates supply the mechanical half of that judgement so it
is reproducible and cannot drift between attempts. A bounded repair re-runs every gate.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from feature_pipeline.reports.dispositions import Disposition, classify_command
from feature_pipeline.reports.findings import Finding, Outcome, Severity
from feature_pipeline.reports.settlement import (
    EnvelopeContract,
    ReportProtocolError,
    settle,
)

_CLEAN = Outcome(Disposition.CLEAN)


def _argv_parts(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value)
    return ()


def _normalize_cwd(value: object) -> str:
    text = str(value or ".").replace("\\", "/")
    text = text[2:] if text.startswith("./") else text
    return text or "."


def _claim_cwd_argv(claim: object) -> tuple[str, tuple[str, ...]]:
    cwd = getattr(claim, "cwd", None)
    argv: Any = getattr(claim, "argv", None)
    if argv is None and isinstance(claim, Mapping):
        cwd = claim.get("cwd")
        argv = claim.get("argv")
        if argv is None and claim.get("command"):
            argv = str(claim["command"]).split()
    if isinstance(argv, str):
        argv = (argv,)
    return _normalize_cwd(cwd), tuple(str(part) for part in (argv or ()))


def command_exit_gate(command_records: Sequence[Mapping[str, object]]) -> Outcome:
    """Every recorded command must have exited cleanly.

    A timeout, an unavailable toolchain, a failed launch, and a plain non-zero exit each map
    to their standardized disposition. The first non-clean record decides the outcome's
    disposition; every non-clean record contributes a finding.
    """
    findings: list[Finding] = []
    worst: Disposition = Disposition.CLEAN
    for record in command_records:
        exit_code = record.get("exit_code", 0)
        reason = str(record.get("reason") or "")
        disposition = classify_command(
            exit_code if isinstance(exit_code, (int, str)) else 0,
            timed_out=record.get("disposition") == "FAIL" and "timeout" in reason.lower(),
            unavailable="not available" in reason.lower(),
            launch_failed="could not be launched" in reason.lower(),
        )
        if disposition is Disposition.CLEAN:
            continue
        if worst is Disposition.CLEAN:
            worst = disposition
        argv = " ".join(_argv_parts(record.get("argv", ())))
        findings.append(
            Finding(
                code=f"command-{disposition}",
                message=(
                    f"command '{argv or record.get('id', '?')}' exited with "
                    f"{exit_code!r}" + (f" ({reason})" if reason else "")
                ),
                severity=(
                    Severity.BLOCKER
                    if disposition is Disposition.INFRASTRUCTURE_BLOCKER
                    else Severity.CRITICAL
                ),
                location=str(record.get("cwd") or "."),
            )
        )
    if not findings:
        return _CLEAN
    return Outcome(worst, tuple(findings))


def evidence_presence_gate(
    claimed_checks: Iterable[object],
    command_records: Sequence[Mapping[str, object]],
) -> Outcome:
    """Every executor-claimed check must map to a runner-recorded ``(cwd, argv)``.

    A claim with no matching recorded command is unverifiable on the executor's say-so; it is
    a ``product-failure`` finding (the same rule as
    ``pipeline_core.verification.missing_command_evidence``, now typed).
    """
    recorded = {
        (_normalize_cwd(record.get("cwd")), _argv_parts(record.get("argv", ())))
        for record in command_records
    }
    findings: list[Finding] = []
    for claim in claimed_checks:
        cwd, argv = _claim_cwd_argv(claim)
        if (cwd, argv) in recorded:
            continue
        findings.append(
            Finding(
                code="missing-command-evidence",
                message=(
                    "executor claimed this check but there is no runner-recorded command"
                ),
                severity=Severity.CRITICAL,
                location=f"{cwd} -> {list(argv)}",
                remediation="run the check through the runner or drop the claim",
            )
        )
    if not findings:
        return _CLEAN
    return Outcome(Disposition.PRODUCT_FAILURE, tuple(findings))


def protocol_gate(
    contract: EnvelopeContract,
    *,
    prose_text: str,
    envelope_text: str,
    role: str,
    task_id: str,
    attempt: int,
) -> Outcome:
    """The report envelope must settle. Any settlement failure is a ``protocol`` disposition."""
    try:
        settle(
            contract,
            prose_text=prose_text,
            envelope_text=envelope_text,
            role=role,
            task_id=task_id,
            attempt=attempt,
        )
    except ReportProtocolError as exc:
        return Outcome(
            Disposition.PROTOCOL,
            (
                Finding(
                    code=exc.code,
                    message=str(exc),
                    severity=Severity.BLOCKER,
                    location=f"{contract.name} envelope for {task_id} attempt {attempt}",
                ),
            ),
        )
    return _CLEAN


def scope_gate(decision: object) -> Outcome:
    """Adapt a WT-02 ``ScopeGateDecision`` to a typed outcome.

    ``clean`` -> :attr:`Disposition.CLEAN`; ``attribution-unavailable`` ->
    :attr:`Disposition.INFRASTRUCTURE_BLOCKER` (the change cannot fix a broken snapshot); a
    ``scope-violation`` -> :attr:`Disposition.PRODUCT_FAILURE` with one finding per violation.
    """
    outcome = str(getattr(decision, "outcome", "") or "")
    if outcome == "clean":
        return _CLEAN
    if outcome == "attribution-unavailable":
        return Outcome(
            Disposition.INFRASTRUCTURE_BLOCKER,
            (
                Finding(
                    code="attribution-unavailable",
                    message=str(
                        getattr(decision, "reason", None)
                        or "worktree attribution was unavailable"
                    ),
                    severity=Severity.BLOCKER,
                ),
            ),
        )
    violations = tuple(getattr(decision, "violations", ()) or ())
    findings = tuple(
        Finding(
            code="out-of-scope-change",
            message=f"executor changed a path outside its allowed scope: {path}",
            severity=Severity.CRITICAL,
            location=str(path),
        )
        for path in (str(getattr(v, "path", v)) for v in violations)
    ) or (
        Finding(
            code="out-of-scope-change",
            message="executor changed a path outside its allowed scope",
            severity=Severity.CRITICAL,
        ),
    )
    return Outcome(Disposition.PRODUCT_FAILURE, findings)


def both_verifiers_pass(task_verdict: str, test_verdict: str) -> bool:
    """``verified`` needs **both** independent verifier verdicts to be exactly ``PASS`` (AC-3)."""
    return task_verdict == "PASS" and test_verdict == "PASS"
