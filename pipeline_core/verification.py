"""Portable verifier verdict, evidence, and independent-orchestration contracts.

The runner — never an executor or a verifier — is the authority on which verification
commands actually ran and what they returned. :func:`build_verification_evidence` packages
that runner-owned command evidence together with the task/attempt identity and the exact
implementation manifest/diff/report references into one immutable :class:`VerificationEvidence`.
:func:`verifier_evidence_payload` serializes it once so the task-verifier and the
test-verifier request builders are handed byte-identical facts.

:func:`orchestrate_verification` completes stage 8: it builds two separate, fresh, read-only
verifier contexts over that one evidence payload, settles each verifier's strict JSON verdict
envelope against its prose report, and lets *only* the parsed ``PASS``/``FAIL``/``BLOCKED``
combination move task state (via :meth:`pipeline_core.state.Run.record_verdicts`). A launch
that fails, a malformed envelope, or a prose/envelope disagreement is written to a diagnostic
and blocks the task — it can never produce ``verified``.

Command *execution* lives in :mod:`pipeline_core.commands`; this module only packages the
result and interprets verdicts — it never launches a command itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .adapters import Adapter, AdapterError, LaunchRequest, LaunchResult, normalize_role
from .artifacts import write_json_atomic, write_text_atomic
from .commands import VerificationRun
from .diagnostics import write_diagnostic_report
from .reports import (
    ReportError,
    VerdictResolution,
    build_verdict_envelope_prompt,
    settle_verifier_verdict,
    verifier_artifacts,
)
from .state import ACTOR_RUNNER, Run


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


def combine_verdict_status(task_verdict: str, test_verdict: str) -> str:
    """The task state two parsed verdicts imply, fail-closed: any ``BLOCKED`` blocks, then any
    ``FAIL`` fails verification, and only ``PASS`` + ``PASS`` verifies."""
    if "BLOCKED" in (task_verdict, test_verdict):
        return "blocked"
    if "FAIL" in (task_verdict, test_verdict):
        return "verification_failed"
    return "verified"


@dataclass(frozen=True)
class VerificationOutcome:
    """The result of one independent-verification attempt.

    On a settled pass ``status`` is what :meth:`Run.record_verdicts` produced and both verdict
    tokens are set. On a launch/settlement failure ``status`` is ``blocked``, ``failure``
    carries the stable reason, ``diagnostic`` names the written report, and the verdicts stay
    ``None`` — there is no path here that yields ``verified`` from anything but two parsed
    ``PASS`` tokens.
    """

    task_id: str
    attempt: int
    status: str
    task_verdict: str | None = None
    test_verdict: str | None = None
    task_result: LaunchResult | None = None
    test_result: LaunchResult | None = None
    verdict_record: Path | None = None
    diagnostic: Path | None = None
    failure: str | None = None
    task_drift: str | None = None
    test_drift: str | None = None
    forced_fail_reason: str | None = None


def parse_verdict(text: str) -> str:
    """Parse one minimal ``{"verdict": <token>}`` JSON envelope; prose is never a verdict.

    This is the low-level primitive :func:`verify_independently` uses. The full VR-02
    orchestration settles the strict four-key envelope against the prose report through
    :func:`pipeline_core.reports.settle_verifier_verdict` instead.
    """
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
    task_verdict = parse_verdict(task_result.stdout)
    test_verdict = parse_verdict(test_result.stdout)
    return VerificationOutcome(
        task_id=evidence.task_id,
        attempt=evidence.attempt,
        status=combine_verdict_status(task_verdict, test_verdict),
        task_verdict=task_verdict,
        test_verdict=test_verdict,
        task_result=task_result,
        test_result=test_result,
    )


# --- independent verifier orchestration (VR-02) --------------------------------------------


@dataclass(frozen=True)
class VerifierAnchors:
    """The repository anchors a fresh verifier needs to resolve paths and load skills."""

    project_root: str
    agents_root: str
    kanban_path: str = "docs/kanban.md"


@dataclass(frozen=True)
class VerifierLaunchers:
    """The injectable independent launcher pair.

    In production both are the same resolved read-only adapter; a test supplies two unrelated
    fakes to prove the two verifier contexts never share state. Concurrency is allowed but not
    required for correctness — :func:`orchestrate_verification` launches them one after another
    and neither launch's output is ever fed into the other's prompt.
    """

    task: Adapter
    test: Adapter


_VERIFIER_RULES: dict[str, tuple[str, ...]] = {
    "task_verifier": (
        "Re-read the feature prompt, the task file, and the acceptance criteria yourself.",
        "Judge only whether the acceptance criteria are met by the implementation below.",
        "You may read the worktree; you may not modify anything and you may not run the "
        "verification commands — their outcomes are the runner-owned evidence below.",
        "Do not tick acceptance-criteria checkboxes.",
    ),
    "test_verifier": (
        "You have no tools. Interpret only the runner-owned evidence below; run nothing.",
        "A declared verification command with no matching runner-recorded command is a FAIL, "
        "never a prompt to re-run the check.",
        "Judge whether the recorded command evidence shows the task's verification commands "
        "passed.",
    ),
}


def _acceptance_block(spec: object) -> str:
    criteria = getattr(spec, "acceptance_criteria", ()) or ()
    if not criteria:
        return "  none declared"
    return "\n".join(f"  - {ac.id}: {ac.text}" for ac in criteria)


def _commands_block(spec: object) -> str:
    commands = getattr(spec, "verification_commands", ()) or ()
    if not commands:
        return "  none declared"
    return "\n".join(f"  - {c.cwd} -> {' '.join(c.argv)}" for c in commands)


def build_verifier_prompt(
    role: str,
    spec: object,
    *,
    anchors: VerifierAnchors,
    feature_prompt: str,
    evidence_payload: str,
    attempt: int,
    plan_path: str | None = None,
) -> str:
    """Render one fresh, read-only verifier context.

    ``role`` is ``task_verifier`` or ``test_verifier``. The two prompts differ only in their
    rules block and in the identical ``evidence_payload`` they both embed verbatim, so the two
    verifiers reason over byte-identical facts and one verifier's findings can never reach the
    other (Risks / Dependencies).
    """
    normalized = normalize_role(role)
    if normalized not in _VERIFIER_RULES:
        raise VerificationError(
            f"unknown verifier role {role!r}; expected task_verifier or test_verifier")
    lines = [
        "Context:",
        f"- Project root: {anchors.project_root}",
        f"- Agents root: {anchors.agents_root}",
        f"- Feature prompt: {feature_prompt}",
        f"- Plan file: {plan_path or 'none'}",
        f"- Task file: {getattr(spec, 'path', '') or 'none'}",
        f"- Kanban file: {anchors.kanban_path}",
        "",
        "Role:",
        f"- {normalized}",
        "",
        "Task:",
        f"- Task ID: {spec.id}",
        f"- Task title: {getattr(spec, 'title', '') or spec.id}",
        f"- Attempt: {attempt}",
        "- Acceptance criteria:",
        _acceptance_block(spec),
        "- Verification commands:",
        _commands_block(spec),
        "",
        "Rules:",
        *(f"- {rule}" for rule in _VERIFIER_RULES[normalized]),
        "",
        "Runner-owned verification evidence (captured this attempt; do not re-run any of it):",
        "```json",
        evidence_payload,
        "```",
        "",
        "Final report:",
        "- Verdict: PASS | FAIL | BLOCKED",
        "- Findings:",
        "- Acceptance criteria assessment:",
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _SettledVerifier:
    """One verifier role's settled outcome, or a failure that blocks the task."""

    token: str | None
    result: LaunchResult | None
    envelope_result: LaunchResult | None
    report_text: str | None
    drift: str | None
    failure: str | None
    diagnostic: Path | None


def _settle_text(path: Path, fallback_stdout: str, run: Run) -> str:
    """Read the artifact the adapter wrote (or fall back to its stdout), redact it, return it —
    so the committed artifact and the text the runner reasons about are the same bytes."""
    target = Path(path)
    text = target.read_text(encoding="utf-8") if target.exists() else (fallback_stdout or "")
    written = write_text_atomic(target, text, repo_root=run.repo_root)
    return Path(written).read_text(encoding="utf-8")


def _diagnose(
    run: Run, spec: object, artifacts: object, *, role: str, attempt: int, reason: str,
    result: LaunchResult | None = None, envelope_result: LaunchResult | None = None,
    report_text: str | None = None,
) -> _SettledVerifier:
    """Persist a failure record and a diagnostic *before* any transition, then report it."""
    write_json_atomic(
        artifacts.verifier_failure,
        {
            "task_id": spec.id,
            "attempt": attempt,
            "role": normalize_role(role),
            "reason": reason,
            "exit_code": None if result is None else result.exit_code,
            "session_id": None if result is None else result.session_id,
            "envelope_exit_code": None if envelope_result is None else envelope_result.exit_code,
        },
        repo_root=run.repo_root,
    )
    diagnostic = write_diagnostic_report(
        run, task_id=spec.id, attempt=attempt,
        note=f"{normalize_role(role)} verification failed: {reason}",
        reports_dir=artifacts.directory,
    )
    return _SettledVerifier(
        token=None, result=result, envelope_result=envelope_result, report_text=report_text,
        drift=None, failure=reason, diagnostic=diagnostic,
    )


def _run_one_verifier(
    run: Run, spec: object, role: str, adapter: Adapter, artifacts: object,
    anchors: VerifierAnchors, evidence_payload: str, attempt: int, plan_path: str | None,
) -> _SettledVerifier:
    normalized = normalize_role(role)
    tool_less = normalized == "test_verifier"
    prompt = build_verifier_prompt(
        normalized, spec, anchors=anchors, feature_prompt=run.prompt_path,
        evidence_payload=evidence_payload, attempt=attempt, plan_path=plan_path,
    )
    write_text_atomic(artifacts.prompt(normalized), prompt, repo_root=run.repo_root)

    request = LaunchRequest(
        role=normalized,
        task_id=spec.id,
        prompt=prompt,
        report_path=artifacts.report(normalized),
        read_only=True,
        working_root=".",
        allowed_scope=tuple(getattr(spec, "allowed_scope", ()) or ()),
        fresh_session=True,
        resume_session_id=None,
        no_tools=tool_less,
    )
    try:
        result = adapter.launch(request)
    except AdapterError as exc:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=f"{normalized} launch error ({exc.code}): {exc}")
    report_text = _settle_text(artifacts.report(normalized), result.stdout, run)
    if result.exit_code != 0:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=f"{normalized} launch exited with {result.exit_code}",
            result=result, report_text=report_text)
    if not result.session_id:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=f"{normalized} launch returned no session id; the verdict envelope "
                   f"cannot be settled",
            result=result, report_text=report_text)

    envelope_request = LaunchRequest(
        role=normalized,
        task_id=spec.id,
        prompt=build_verdict_envelope_prompt(
            role=normalized, task_id=spec.id, attempt=attempt),
        report_path=artifacts.envelope(normalized),
        read_only=True,
        working_root=".",
        resume_session_id=result.session_id,
        no_tools=True,
    )
    try:
        envelope_result = adapter.launch(envelope_request)
    except AdapterError as exc:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=f"{normalized} verdict envelope request failed ({exc.code}): {exc}",
            result=result, report_text=report_text)
    envelope_text = _settle_text(artifacts.envelope(normalized), envelope_result.stdout, run)
    if envelope_result.exit_code != 0:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=f"{normalized} verdict envelope request exited with "
                   f"{envelope_result.exit_code}",
            result=result, envelope_result=envelope_result, report_text=report_text)

    try:
        resolution: VerdictResolution = settle_verifier_verdict(
            prose_text=report_text, envelope_text=envelope_text,
            role=normalized, task_id=spec.id, attempt=attempt,
        )
    except ReportError as exc:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=f"{exc.code}: {exc}",
            result=result, envelope_result=envelope_result, report_text=report_text)

    return _SettledVerifier(
        token=resolution.token, result=result, envelope_result=envelope_result,
        report_text=report_text, drift=resolution.drift, failure=None, diagnostic=None,
    )


def _block(run: Run, task_id: str, reason: str) -> str:
    record = run.task(task_id)
    if record.status != "blocked":
        run.transition_task(task_id, "blocked", actor=ACTOR_RUNNER, note=reason)
    record.blocker = reason
    run.record_event(f"verification-blocked:{task_id}", to="blocked", note=reason)
    return record.status


def orchestrate_verification(
    run: Run,
    spec: object,
    evidence: VerificationEvidence,
    *,
    launchers: VerifierLaunchers,
    anchors: VerifierAnchors,
    attempt: int,
    plan_path: str | None = None,
) -> VerificationOutcome:
    """Obtain two fresh, independent, read-only verdicts and let only their parsed combination
    change ``spec.id``'s state.

    The task must be ``implemented`` and ``evidence`` must be this exact task/attempt. Both
    verifiers receive :func:`verifier_evidence_payload` verbatim. Every failure mode — a launch
    that will not start or exits non-zero, a missing session id, a malformed verdict envelope,
    or a prose/envelope disagreement — writes a diagnostic and blocks the task; none can reach
    ``verified``. An unbacked executor check (:func:`evidence_forces_fail`) forces the test
    verdict to ``FAIL`` regardless of what the tool-less test verifier returned (AC-4).
    """
    task_id = spec.id
    record = run.task(task_id)
    if record.status != "implemented":
        raise VerificationError(
            f"{task_id} must be 'implemented' to verify independently, is '{record.status}'")
    if evidence.task_id != task_id or evidence.attempt != attempt:
        raise VerificationError(
            "verification evidence does not match the task/attempt being verified")

    artifacts = verifier_artifacts(run.run_dir, task_id, attempt)
    artifacts.directory.mkdir(parents=True, exist_ok=True)
    payload = verifier_evidence_payload(evidence)

    task_settled = _run_one_verifier(
        run, spec, "task_verifier", launchers.task, artifacts, anchors, payload, attempt,
        plan_path)
    if task_settled.failure:
        status = _block(run, task_id, f"task_verifier: {task_settled.failure}")
        return VerificationOutcome(
            task_id=task_id, attempt=attempt, status=status,
            task_result=task_settled.result, diagnostic=task_settled.diagnostic,
            failure=task_settled.failure)

    test_settled = _run_one_verifier(
        run, spec, "test_verifier", launchers.test, artifacts, anchors, payload, attempt,
        plan_path)
    if test_settled.failure:
        status = _block(run, task_id, f"test_verifier: {test_settled.failure}")
        return VerificationOutcome(
            task_id=task_id, attempt=attempt, status=status,
            task_result=task_settled.result, test_result=test_settled.result,
            diagnostic=test_settled.diagnostic, failure=test_settled.failure)

    task_verdict = task_settled.token
    test_verdict = test_settled.token
    forced = evidence_forces_fail(evidence)
    forced_reason: str | None = None
    if forced and test_verdict != "FAIL":
        forced_reason = forced
        test_verdict = "FAIL"

    status = run.record_verdicts(task_id, task_verdict, test_verdict)

    verdict_record = write_json_atomic(
        artifacts.verdict_record,
        {
            "task_id": task_id,
            "attempt": attempt,
            "task_verdict": task_verdict,
            "test_verdict": test_verdict,
            "status": status,
            "recorded_at": run.task(task_id).verification.get("verified_at"),
            "task_drift": task_settled.drift,
            "test_drift": test_settled.drift,
            "forced_fail_reason": forced_reason,
        },
        repo_root=run.repo_root,
    )
    for drift in (task_settled.drift, test_settled.drift):
        if drift:
            run.record_event(f"verifier-drift:{task_id}", to=str(attempt), note=drift)
    if forced_reason:
        run.record_event(
            f"verifier-forced-fail:{task_id}", to=str(attempt), note=forced_reason)

    return VerificationOutcome(
        task_id=task_id,
        attempt=attempt,
        status=status,
        task_verdict=task_verdict,
        test_verdict=test_verdict,
        task_result=task_settled.result,
        test_result=test_settled.result,
        verdict_record=verdict_record,
        task_drift=task_settled.drift,
        test_drift=test_settled.drift,
        forced_fail_reason=forced_reason,
    )
