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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifacts import write_text_atomic

#: The only two states an executor launch may report. There is deliberately no ``verified``
#: token: nothing an executor writes can carry a task past ``implemented``.
STATUS_TOKENS = ("implemented", "blocked")

#: The strict JSON status-envelope shape — exactly these keys, nothing missing, nothing extra.
ENVELOPE_KEYS = frozenset({"role", "status", "task_id", "attempt"})

REPORTS_DIRNAME = "reports"

#: A ``Status:`` line: an optional leading ``- ``, optional ``*``/``_`` emphasis around the
#: field name and/or the token, then one of the known tokens. Nothing else parses.
_STATUS_RE = re.compile(
    r"(?im)^\s*-?\s*[*_]*\s*Status\s*[*_]*\s*:\s*[*_]*\s*(implemented|blocked)\b"
)


class ReportError(RuntimeError):
    """A report/artifact failure. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


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
    """The lower-cased token from the prose ``- Status:`` line, or ``unparseable-report``."""
    match = _STATUS_RE.search(text or "")
    if not match:
        raise ReportError(
            "executor report has no parseable '- Status: implemented | blocked' line — a "
            "claim without the required field is not evidence",
            "unparseable-report",
        )
    return match.group(1).lower()


def parse_status_envelope(text: str, *, role: str, task_id: str, attempt: int) -> str:
    """Strictly parse one JSON status envelope. Never infers, never falls back to the prose."""
    stripped = (text or "").strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        raise ReportError(
            f"status envelope is not valid JSON: {stripped[:200]!r}",
            "unparseable-status-envelope",
        ) from None
    if not isinstance(data, dict):
        raise ReportError("status envelope is not a JSON object", "unparseable-status-envelope")
    if set(data.keys()) != set(ENVELOPE_KEYS):
        raise ReportError(
            f"status envelope has keys {sorted(data.keys())}, expected exactly "
            f"{sorted(ENVELOPE_KEYS)} — none missing, none extra",
            "unparseable-status-envelope",
        )
    token = data.get("status")
    if token not in STATUS_TOKENS:
        raise ReportError(
            f"status envelope 'status' is {token!r}, expected one of {list(STATUS_TOKENS)}",
            "unparseable-status-envelope",
        )
    if data.get("role") != role:
        raise ReportError(
            f"status envelope declares role {data.get('role')!r}, expected {role!r}",
            "unparseable-status-envelope",
        )
    if data.get("task_id") != task_id:
        raise ReportError(
            f"status envelope declares task_id {data.get('task_id')!r}, expected {task_id!r}",
            "unparseable-status-envelope",
        )
    if data.get("attempt") != attempt or isinstance(data.get("attempt"), bool):
        raise ReportError(
            f"status envelope declares attempt {data.get('attempt')!r}, expected {attempt!r}",
            "unparseable-status-envelope",
        )
    return token


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
    """
    envelope_token = parse_status_envelope(
        envelope_text, role=role, task_id=task_id, attempt=attempt
    )

    prose_token: str | None = None
    prose_error: str | None = None
    try:
        prose_token = parse_executor_status(prose_text)
    except ReportError as exc:
        prose_error = str(exc)

    if prose_token is not None and prose_token != envelope_token:
        raise ReportError(
            f"executor status disagreement for {task_id} attempt {attempt}: prose reported "
            f"{prose_token!r}, envelope reported {envelope_token!r} — not resolved "
            f"automatically",
            "status-envelope-mismatch",
        )

    drift = None
    if prose_token is None:
        drift = (
            f"prose status line unusable ({prose_error}); envelope status "
            f"{envelope_token!r} stands"
        )
    return StatusResolution(envelope_token, prose_token, envelope_token, drift)


# --- independent verifier artifacts and verdict settlement (VR-02) --------------------------

#: The only verdict tokens a verifier launch may report.
VERDICT_TOKENS = ("PASS", "FAIL", "BLOCKED")

#: The strict JSON verdict-envelope shape — exactly these keys, nothing missing, nothing extra.
VERDICT_ENVELOPE_KEYS = frozenset({"role", "verdict", "task_id", "attempt"})

#: The two independent verifier roles. ``verify-<attempt>/`` is their per-attempt home so a
#: repair pass never overwrites the previous attempt's reports (AC-3).
VERIFIER_ROLES = ("task_verifier", "test_verifier")

_VERIFIER_ROLE_KEY = {"task_verifier": "task", "test_verifier": "test"}

#: A ``Verdict:`` line: an optional leading ``- ``, optional ``*``/``_`` emphasis around the
#: field name and/or the token, then one of the known tokens. Nothing else parses.
_VERDICT_RE = re.compile(
    r"(?im)^\s*-?\s*[*_]*\s*Verdict\s*[*_]*\s*:\s*[*_]*\s*(PASS|FAIL|BLOCKED)\b"
)


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
    """The upper-cased token from the prose ``- Verdict:`` line, or ``unparseable-report``."""
    match = _VERDICT_RE.search(text or "")
    if not match:
        raise ReportError(
            "verifier report has no parseable '- Verdict: PASS | FAIL | BLOCKED' line — a "
            "claim without the required field is not evidence",
            "unparseable-report",
        )
    return match.group(1).upper()


def parse_verdict_envelope(text: str, *, role: str, task_id: str, attempt: int) -> str:
    """Strictly parse one JSON verdict envelope. Never infers, never falls back to the prose."""
    stripped = (text or "").strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        raise ReportError(
            f"verdict envelope is not valid JSON: {stripped[:200]!r}",
            "unparseable-verdict-envelope",
        ) from None
    if not isinstance(data, dict):
        raise ReportError(
            "verdict envelope is not a JSON object", "unparseable-verdict-envelope")
    if set(data.keys()) != set(VERDICT_ENVELOPE_KEYS):
        raise ReportError(
            f"verdict envelope has keys {sorted(data.keys())}, expected exactly "
            f"{sorted(VERDICT_ENVELOPE_KEYS)} — none missing, none extra",
            "unparseable-verdict-envelope",
        )
    verdict = data.get("verdict")
    if verdict not in VERDICT_TOKENS:
        raise ReportError(
            f"verdict envelope 'verdict' is {verdict!r}, expected one of "
            f"{list(VERDICT_TOKENS)}",
            "unparseable-verdict-envelope",
        )
    if data.get("role") != role:
        raise ReportError(
            f"verdict envelope declares role {data.get('role')!r}, expected {role!r}",
            "unparseable-verdict-envelope",
        )
    if data.get("task_id") != task_id:
        raise ReportError(
            f"verdict envelope declares task_id {data.get('task_id')!r}, expected {task_id!r}",
            "unparseable-verdict-envelope",
        )
    if data.get("attempt") != attempt or isinstance(data.get("attempt"), bool):
        raise ReportError(
            f"verdict envelope declares attempt {data.get('attempt')!r}, expected {attempt!r}",
            "unparseable-verdict-envelope",
        )
    return verdict


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
    """
    envelope_token = parse_verdict_envelope(
        envelope_text, role=role, task_id=task_id, attempt=attempt
    )

    prose_token: str | None = None
    prose_error: str | None = None
    try:
        prose_token = parse_verifier_verdict(prose_text)
    except ReportError as exc:
        prose_error = str(exc)

    if prose_token is not None and prose_token != envelope_token:
        raise ReportError(
            f"verifier verdict disagreement for {task_id} attempt {attempt}: prose reported "
            f"{prose_token!r}, envelope reported {envelope_token!r} — not resolved "
            f"automatically",
            "verdict-envelope-mismatch",
        )

    drift = None
    if prose_token is None:
        drift = (
            f"prose verdict line unusable ({prose_error}); envelope verdict "
            f"{envelope_token!r} stands"
        )
    return VerdictResolution(envelope_token, prose_token, envelope_token, drift)


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
