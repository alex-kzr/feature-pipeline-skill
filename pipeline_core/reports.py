"""Executor launch artifact layout and strict executor-status settlement.

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
