"""Executor and verifier launch artifact layout and strict token settlement.

Every executor launch *attempt* owns a generation directory
``reports/<TASK-ID>/launch-<generation>/``. Beneath it live the executor's prose report
(``executor-<generation>.md``), the strict JSON status envelope
(``executor-envelope-<generation>.json``), the prompt envelope the executor was handed
(``executor-prompt-<generation>.md``), and two *reserved* paths that RDS-05 — not this module —
fills in: ``implementation-manifest-<generation>.json`` and ``implementation-diff-<generation>.md``.

Status is accepted only from the strict contract. The prose ``- Status:`` line and the JSON
envelope must both parse; if both parse and disagree the launch fails closed
(``status-envelope-mismatch``). When the prose line is absent or malformed the envelope token
still stands and the drift is reported to the caller so it can be recorded — the envelope,
never the prose, is authoritative, and a malformed envelope is always
``unparseable-status-envelope`` regardless of what the prose next to it says.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from feature_pipeline.reports.settlement import (
    STATUS_CONTRACT as _STATUS_CONTRACT,
    VERDICT_CONTRACT as _VERDICT_CONTRACT,
    ReportProtocolError as _ReportProtocolError,
    parse_envelope as _parse_envelope,
    parse_prose_token as _parse_prose_token,
    settle as _settle,
)

from .artifacts import write_text_atomic

#: The only two states an executor launch may report. There is deliberately no ``verified``
#: token: nothing an executor writes can carry a task past ``implemented``. Sourced from the
#: one settlement contract (VP-02) so the vocabulary is defined in exactly one place.
STATUS_TOKENS = _STATUS_CONTRACT.token_values

#: The strict JSON status-envelope shape — exactly these keys, nothing missing, nothing extra.
ENVELOPE_KEYS = _STATUS_CONTRACT.keys

REPORTS_DIRNAME = "reports"


class ReportError(RuntimeError):
    """A report/artifact failure. ``code`` is a stable, machine-readable reason.

    Report-protocol settlement (VP-02) is done by
    :mod:`feature_pipeline.reports.settlement`; its :class:`ReportProtocolError` is re-raised
    here with the same ``code`` and message so every historical caller and golden is
    unaffected.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _reraise(exc: _ReportProtocolError) -> "ReportError":
    """Translate a settlement failure to the historical :class:`ReportError`."""
    return ReportError(str(exc), exc.code)


@dataclass(frozen=True)
class LaunchArtifacts:
    """The full set of paths one executor launch generation owns.

    ``implementation_manifest`` and ``implementation_diff`` are *reserved* — computed and
    exposed here so RDS-05 has a fixed home for them, but never created by this module or by
    :mod:`pipeline_core.dispatch`.
    """

    task_id: str
    generation: int
    directory: Path
    executor_report: Path
    status_envelope: Path
    prompt_envelope: Path
    implementation_manifest: Path
    implementation_diff: Path
    launch_failure: Path


def launch_artifacts(run_dir: str | Path, task_id: str, generation: int) -> LaunchArtifacts:
    """Resolve every artifact path for ``task_id``'s ``generation``-th executor launch."""
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ReportError(
            f"executor launch generation must be a positive integer, got {generation!r}",
            "invalid-launch-generation",
        )
    directory = Path(run_dir) / REPORTS_DIRNAME / str(task_id) / f"launch-{generation}"
    return LaunchArtifacts(
        task_id=str(task_id),
        generation=generation,
        directory=directory,
        executor_report=directory / f"executor-{generation}.md",
        status_envelope=directory / f"executor-envelope-{generation}.json",
        prompt_envelope=directory / f"executor-prompt-{generation}.md",
        implementation_manifest=directory / f"implementation-manifest-{generation}.json",
        implementation_diff=directory / f"implementation-diff-{generation}.md",
        launch_failure=directory / f"launch-failure-{generation}.json",
    )


def build_status_envelope_prompt(*, role: str, task_id: str, attempt: int) -> str:
    """The same-session, tool-free continuation that asks only for the JSON status envelope."""
    shape = (
        f'{{"role": "{role}", "status": <one of {list(STATUS_TOKENS)}>, '
        f'"task_id": "{task_id}", "attempt": {attempt}}}'
    )
    return "\n".join(
        [
            "Reply with only the following JSON object and nothing else — no other text, no "
            "code fence, no explanation, no markdown:",
            shape,
            "",
            "Use the real status from the report you just wrote; do not copy the placeholder "
            "token.",
        ]
    )


def parse_executor_status(text: str) -> str:
    """The lower-cased token from the prose ``- Status:`` line, or ``unparseable-report``.

    Delegates to the one parameterized prose parser (VP-02).
    """
    try:
        return _parse_prose_token(text or "", _STATUS_CONTRACT)
    except _ReportProtocolError as exc:
        raise _reraise(exc) from None


def parse_status_envelope(text: str, *, role: str, task_id: str, attempt: int) -> str:
    """Strictly parse one JSON status envelope. Never infers, never falls back to the prose.

    Delegates to the one parameterized envelope parser (VP-02).
    """
    try:
        return _parse_envelope(
            text or "", _STATUS_CONTRACT, role=role, task_id=task_id, attempt=attempt
        )
    except _ReportProtocolError as exc:
        raise _reraise(exc) from None


@dataclass(frozen=True)
class StatusResolution:
    """The settled executor status plus how it was reached."""

    token: str  # 'implemented' | 'blocked'
    prose_token: str | None
    envelope_token: str
    drift: str | None  # set when the prose line was absent or malformed


def settle_executor_status(
    *, prose_text: str, envelope_text: str, role: str, task_id: str, attempt: int
) -> StatusResolution:
    """Reconcile the strict prose parser and the authoritative JSON envelope.

    * envelope invalid → ``unparseable-status-envelope`` (whatever the prose says);
    * envelope valid, prose valid, they agree → that token;
    * envelope valid, prose valid, they disagree → ``status-envelope-mismatch`` (fail closed);
    * envelope valid, prose absent/malformed → the envelope token, with ``drift`` set.

    Delegates to the one parameterized settlement function (VP-02); the return shape is kept
    for every historical caller.
    """
    try:
        settled = _settle(
            _STATUS_CONTRACT,
            prose_text=prose_text,
            envelope_text=envelope_text,
            role=role,
            task_id=task_id,
            attempt=attempt,
        )
    except _ReportProtocolError as exc:
        raise _reraise(exc) from None
    return StatusResolution(
        settled.token, settled.prose_token, settled.envelope_token, settled.drift
    )


# --- independent verifier artifacts and verdict settlement (VR-02) --------------------------

#: The only verdict tokens a verifier launch may report. Sourced from the one settlement
#: contract (VP-02).
VERDICT_TOKENS = _VERDICT_CONTRACT.token_values

#: The strict JSON verdict-envelope shape — exactly these keys, nothing missing, nothing extra.
VERDICT_ENVELOPE_KEYS = _VERDICT_CONTRACT.keys

#: The two independent verifier roles. ``verify-<attempt>/`` is their per-attempt home so a
#: repair pass never overwrites the previous attempt's reports (AC-3).
VERIFIER_ROLES = ("task_verifier", "test_verifier")

_VERIFIER_ROLE_KEY = {"task_verifier": "task", "test_verifier": "test"}


def _verifier_role_key(role: str) -> str:
    key = _VERIFIER_ROLE_KEY.get(str(role).strip().lower().replace("-", "_"))
    if key is None:
        raise ReportError(
            f"unknown verifier role {role!r}; expected one of {list(VERIFIER_ROLES)}",
            "unknown-verifier-role",
        )
    return key


@dataclass(frozen=True)
class VerifierArtifacts:
    """Every path one independent-verification attempt owns.

    Each of the two roles gets a prose report, a strict JSON verdict envelope, and the prompt
    envelope it was handed. ``verdict_record`` is the runner's combined, persisted verdict
    (both tokens plus the timestamp); ``verifier_failure`` holds a launch/settlement failure.
    """

    task_id: str
    attempt: int
    directory: Path
    task_report: Path
    task_envelope: Path
    task_prompt: Path
    test_report: Path
    test_envelope: Path
    test_prompt: Path
    verdict_record: Path
    verifier_failure: Path

    def report(self, role: str) -> Path:
        return getattr(self, f"{_verifier_role_key(role)}_report")

    def envelope(self, role: str) -> Path:
        return getattr(self, f"{_verifier_role_key(role)}_envelope")

    def prompt(self, role: str) -> Path:
        return getattr(self, f"{_verifier_role_key(role)}_prompt")


def verifier_artifacts(
    run_dir: str | Path, task_id: str, attempt: int
) -> VerifierArtifacts:
    """Resolve every artifact path for ``task_id``'s ``attempt``-th independent verification."""
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ReportError(
            f"verification attempt must be a positive integer, got {attempt!r}",
            "invalid-verification-attempt",
        )
    directory = Path(run_dir) / REPORTS_DIRNAME / str(task_id) / f"verify-{attempt}"
    return VerifierArtifacts(
        task_id=str(task_id),
        attempt=attempt,
        directory=directory,
        task_report=directory / f"task-verifier-{attempt}.md",
        task_envelope=directory / f"task-verifier-envelope-{attempt}.json",
        task_prompt=directory / f"task-verifier-prompt-{attempt}.md",
        test_report=directory / f"test-verifier-{attempt}.md",
        test_envelope=directory / f"test-verifier-envelope-{attempt}.json",
        test_prompt=directory / f"test-verifier-prompt-{attempt}.md",
        verdict_record=directory / f"verdict-{attempt}.json",
        verifier_failure=directory / f"verifier-failure-{attempt}.json",
    )


def build_verdict_envelope_prompt(*, role: str, task_id: str, attempt: int) -> str:
    """The same-session, tool-free continuation that asks only for the JSON verdict envelope."""
    shape = (
        f'{{"role": "{role}", "verdict": <one of {list(VERDICT_TOKENS)}>, '
        f'"task_id": "{task_id}", "attempt": {attempt}}}'
    )
    return "\n".join(
        [
            "Reply with only the following JSON object and nothing else — no other text, no "
            "code fence, no explanation, no markdown:",
            shape,
            "",
            "Use the real verdict from the report you just wrote; do not copy the placeholder "
            "token.",
        ]
    )


def parse_verifier_verdict(text: str) -> str:
    """The upper-cased token from the prose ``- Verdict:`` line, or ``unparseable-report``.

    Delegates to the one parameterized prose parser (VP-02).
    """
    try:
        return _parse_prose_token(text or "", _VERDICT_CONTRACT)
    except _ReportProtocolError as exc:
        raise _reraise(exc) from None


def parse_verdict_envelope(text: str, *, role: str, task_id: str, attempt: int) -> str:
    """Strictly parse one JSON verdict envelope. Never infers, never falls back to the prose.

    Delegates to the one parameterized envelope parser (VP-02).
    """
    try:
        return _parse_envelope(
            text or "", _VERDICT_CONTRACT, role=role, task_id=task_id, attempt=attempt
        )
    except _ReportProtocolError as exc:
        raise _reraise(exc) from None


@dataclass(frozen=True)
class VerdictResolution:
    """The settled verifier verdict plus how it was reached."""

    token: str  # 'PASS' | 'FAIL' | 'BLOCKED'
    prose_token: str | None
    envelope_token: str
    drift: str | None  # set when the prose line was absent or malformed


def settle_verifier_verdict(
    *, prose_text: str, envelope_text: str, role: str, task_id: str, attempt: int
) -> VerdictResolution:
    """Reconcile the strict prose verdict parser and the authoritative JSON envelope.

    * envelope invalid → ``unparseable-verdict-envelope`` (whatever the prose says);
    * envelope valid, prose valid, they agree → that token;
    * envelope valid, prose valid, they disagree → ``verdict-envelope-mismatch`` (fail closed);
    * envelope valid, prose absent/malformed → the envelope token, with ``drift`` set.

    Delegates to the one parameterized settlement function (VP-02); the return shape is kept
    for every historical caller.
    """
    try:
        settled = _settle(
            _VERDICT_CONTRACT,
            prose_text=prose_text,
            envelope_text=envelope_text,
            role=role,
            task_id=task_id,
            attempt=attempt,
        )
    except _ReportProtocolError as exc:
        raise _reraise(exc) from None
    return VerdictResolution(
        settled.token, settled.prose_token, settled.envelope_token, settled.drift
    )


# --- bounded-repair report consolidation (VR-03) -------------------------------------------

#: A repair report lives beside the launch and verify directories at
#: ``reports/<TASK-ID>/repair-<attempt>.md`` and is immutable once written — a later attempt
#: writes a new number, it never rewrites an earlier one.
REPAIR_REPORT_STEM = "repair"

_REPAIR_NAME_RE = re.compile(rf"^{REPAIR_REPORT_STEM}-([1-9][0-9]*)\.md$")


@dataclass(frozen=True)
class RepairReport:
    """The consolidated repair report one failed verification gate produced."""

    path: Path
    attempt: int
    source_attempt: int
    findings: tuple[str, ...]
    product_defects: tuple[str, ...]
    environment_problems: tuple[str, ...]
    regression_tests: tuple[str, ...]


def repair_report_path(run_dir: str | Path, task_id: str, attempt: int) -> Path:
    """Resolve the immutable path for ``task_id``'s ``attempt``-numbered repair report."""
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ReportError(
            f"repair attempt must be a positive integer, got {attempt!r}",
            "invalid-repair-attempt",
        )
    return (
        Path(run_dir) / REPORTS_DIRNAME / str(task_id) / f"{REPAIR_REPORT_STEM}-{attempt}.md"
    )


def newest_repair_report(run_dir: str | Path, task_id: str) -> Path | None:
    """The highest-numbered persisted repair report for ``task_id``, or ``None``.

    A resume that lands back on ``verification_failed`` uses this to continue the exact repair
    round already opened rather than deriving a fresh one and double-counting the attempt.
    """
    base = Path(run_dir) / REPORTS_DIRNAME / str(task_id)
    if not base.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for candidate in base.glob(f"{REPAIR_REPORT_STEM}-*.md"):
        match = _REPAIR_NAME_RE.match(candidate.name)
        if match and candidate.is_file():
            number = int(match.group(1))
            if best is None or number > best[0]:
                best = (number, candidate)
    return None if best is None else best[1]


def consolidate_findings(*sources: str) -> tuple[str, ...]:
    """Order-preserving, whitespace-insensitive de-duplication of the verifier finding lines.

    Both verifiers' verbatim reports are still carried in the repair report; this is the
    single merged, de-duplicated list the repairing executor works from.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for text in sources:
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not line.strip("-*_ 0123456789."):
                continue
            key = " ".join(line.lower().split())
            if key in seen:
                continue
            seen.add(key)
            merged.append(line)
    return tuple(merged)


def _bullet_list(items: Sequence[str], empty: str) -> list[str]:
    rows = [str(item).strip() for item in items if str(item).strip()]
    return [f"- {row}" for row in rows] if rows else [f"- {empty}"]


def write_repair_report(
    run: object,
    spec: object,
    attempt: int,
    *,
    task_verifier_text: str,
    test_verifier_text: str,
    source_attempt: int | None = None,
    product_defects: Sequence[str] = (),
    environment_problems: Sequence[str] = (),
    regression_tests: Sequence[str] = (),
) -> RepairReport:
    """Consolidate both verifier reports into one attempt-numbered repair report.

    The report always cites both source reports, restates the original allowed/out-of-scope
    verbatim, separates product defects from environment problems, and lists the regression
    tests the repair must rerun. Deduplication of the *finding lines* is done here;
    interpretation (which finding is which kind) is the caller's, passed in explicitly. The
    two source reports are also embedded verbatim so nothing is lost.
    """
    if source_attempt is None:
        source_attempt = max(attempt - 1, 1)
    path = repair_report_path(run.run_dir, spec.id, attempt)
    findings = consolidate_findings(task_verifier_text, test_verifier_text)
    maximum = getattr(spec, "max_repair_attempts", "?")
    scope = ", ".join(getattr(spec, "allowed_scope", ()) or ()) or "none"
    out_of_scope = ", ".join(getattr(spec, "out_of_scope", ()) or ()) or "none"
    verify_dir = f"{REPORTS_DIRNAME}/{spec.id}/verify-{source_attempt}"

    lines: list[str] = [
        f"# Repair Report — {spec.id} — attempt {attempt} of {maximum}",
        "",
        f"- Task: {spec.id} — {getattr(spec, 'title', '') or spec.id}",
        f"- Consolidated from: {verify_dir}/task-verifier-{source_attempt}.md, "
        f"{verify_dir}/test-verifier-{source_attempt}.md",
        f"- Attempt: {attempt} of {maximum}",
        f"- Source verification gate: {source_attempt}",
        f"- Original scope (unchanged): {scope}",
        f"- Out of scope (unchanged): {out_of_scope}",
        "",
        "## Consolidated findings (deduplicated)",
        "",
        *_bullet_list(findings, "no discrete finding lines parsed — read the verbatim reports"),
        "",
        "## Product defects to fix",
        "",
        *_bullet_list(product_defects, "none itemised — treat every consolidated finding above as a defect"),
        "",
        "## Environment problems (out of this task's control — do not fix here)",
        "",
        *_bullet_list(environment_problems, "none"),
        "",
        "## Required regression tests",
        "",
        *_bullet_list(
            regression_tests,
            "rerun every declared verification command and re-check every acceptance criterion",
        ),
        "",
        "## Scope constraints (verbatim, unchanged)",
        "",
        f"- Write only inside: {scope}",
        f"- Never touch: {out_of_scope}",
        "- Do not widen scope to satisfy a finding; an out-of-scope need is a blocker or a "
        "discovery, never a change in this task.",
        "",
        "## Task-verifier report (verbatim)",
        "",
        (task_verifier_text or "").strip() or "_(the task-verifier report was empty)_",
        "",
        "## Test-verifier report (verbatim)",
        "",
        (test_verifier_text or "").strip() or "_(the test-verifier report was empty)_",
        "",
    ]
    document = "\n".join(line.rstrip() for line in lines).rstrip("\n") + "\n"
    written = write_text_atomic(path, document, repo_root=getattr(run, "repo_root", None))
    return RepairReport(
        path=written,
        attempt=attempt,
        source_attempt=source_attempt,
        findings=findings,
        product_defects=tuple(str(x).strip() for x in product_defects if str(x).strip()),
        environment_problems=tuple(
            str(x).strip() for x in environment_problems if str(x).strip()),
        regression_tests=tuple(str(x).strip() for x in regression_tests if str(x).strip()),
    )
