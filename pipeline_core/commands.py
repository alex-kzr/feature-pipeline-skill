"""Safe argv-only command execution with durable, redacted, reproducible evidence.

The runner — never an agent — owns this call. Every launched command produces:

* a stable ``command-N`` record (argv, repo-relative cwd, exit, duration, log paths) persisted
  on the owning :class:`~pipeline_core.state.Run`;
* redacted stdout/stderr logs under ``<run_dir>/logs`` — secrets and machine-local paths are
  removed *before* a byte-aware tail truncation, so a cut can never strip the anchor off a
  path and leave an unrecognizable fragment behind;
* a :class:`CommandOutcome` whose ``disposition`` is ``PASS`` for a clean exit, ``BLOCKED``
  for an unavailable binary / toolchain / launch failure (never spends a repair attempt), and
  ``FAIL`` for a genuine non-zero or timeout.

Output budgets are selected by *outcome*, not by stage: a success keeps the routine 16 KiB
budget, anything else keeps the 64 KiB diagnostic budget. Commands run with ``shell=False``
from a cwd resolved beneath the project root, and the whole process tree is terminated on
timeout or cancellation so no child survives on Windows or POSIX. Every invocation of a
caller-declared serialized program (``serialized_programs``) takes the cross-process write
mutex for that program first.

Every command spawns through the shared
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner` (RS-01), which pins
``encoding="utf-8"`` (with ``errors="strict"``) on the ``subprocess.Popen`` call explicitly —
``text=True`` alone decodes stdout/stderr via ``locale.getpreferredencoding()``, which is not
UTF-8 on every host locale (RDS-12), silently corrupting any non-ASCII byte a verification
command's toolchain writes to its pipes.

Standard library only.
"""

from __future__ import annotations

import os
import math
import re
import base64
import secrets
import shutil
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from feature_pipeline.infrastructure.process import LocalProcessRunner
from feature_pipeline.infrastructure.process.capture import tail_truncate as _tail_truncate
from feature_pipeline.ports.process import ProcessError, ProcessSpec

from .artifacts import write_json_atomic, write_text_atomic
from .adapters import LiveProbeRequest
from .concurrency import is_serialized_program, write_mutex
from .redaction import output_rules, redact_text
from .snapshot import require_verification_snapshot
from .state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT, LiveProbeEvidence, Run, repo_relative

ROUTINE_OUTPUT_BUDGET = 16 * 1024
DIAGNOSTIC_OUTPUT_BUDGET = 64 * 1024
PROBE_STDERR_REASON_BUDGET = 4096

DISPOSITION_PASS = "PASS"
DISPOSITION_FAIL = "FAIL"
DISPOSITION_BLOCKED = "BLOCKED"


class _ProbeAdapterUnavailable(Exception):
    """Internal control flow for a selected adapter that cannot be launched."""


@dataclass(frozen=True)
class CommandOutcome:
    """The durable evidence one launched command produced."""

    command_id: str
    exit_code: int | str
    disposition: str  # PASS | FAIL | BLOCKED
    stdout_log: str | None
    stderr_log: str | None
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.disposition == DISPOSITION_BLOCKED

    @property
    def passed(self) -> bool:
        return self.disposition == DISPOSITION_PASS


def resolve_program(program: str, cwd: str | Path) -> str | None:
    """Resolve a bare program on PATH or a path-shaped program under its cwd."""
    if os.path.dirname(program):
        candidate = Path(program)
        return shutil.which(str(candidate if candidate.is_absolute() else Path(cwd) / candidate))
    return shutil.which(program)


def classify_outcome(exit_code: int | str) -> tuple[str, str | None]:
    """Map an exit code to ``(disposition, reason)`` deterministically.

    Environmental problems — an absent program, a failed launch — are ``BLOCKED`` so the
    repair loop is never charged for something the change cannot fix. A real non-zero exit or
    a timeout is a ``FAIL``.
    """
    if exit_code == 0:
        return DISPOSITION_PASS, None
    if exit_code == EXIT_NOT_FOUND:
        return DISPOSITION_BLOCKED, "program or toolchain is not available"
    if exit_code == EXIT_LAUNCH_FAILED:
        return DISPOSITION_BLOCKED, "command could not be launched in this environment"
    if exit_code == EXIT_TIMEOUT:
        return DISPOSITION_FAIL, "command exceeded its timeout"
    return DISPOSITION_FAIL, f"command exited with {exit_code}"


def redact_then_truncate(text: str | None, budget: int, repo_root: str | Path) -> str:
    """Redact secrets and machine-local paths, *then* apply the byte budget."""
    return _tail_truncate(redact_text(text or "", output_rules(repo_root)), budget)


def redact_probe_output(text: str | None, private_value: str, budget: int,
                        repo_root: str | Path) -> str:
    """Remove the runner-only probe value before normal redaction and truncation.

    The value is intentionally neither hashed nor otherwise transformed: a hash would itself
    be a recoverable derivative persisted into durable evidence.  Callers must use this for
    every output path before writing a log or state record.
    """
    if not isinstance(private_value, str) or not private_value:
        raise ValueError("probe redaction requires a non-empty private value")
    encoded = private_value.encode("utf-8")
    # The marker never intentionally crosses the runner boundary.  If a broken adapter does
    # expose it, remove the ordinary lossless encodings a diagnostic might use as well; none
    # of these derivatives is ever persisted or named in evidence.
    variants = (
        private_value,
        base64.b64encode(encoded).decode("ascii"),
        base64.urlsafe_b64encode(encoded).decode("ascii"),
        encoded.hex(),
    )
    redacted = text or ""
    for variant in variants:
        redacted = redacted.replace(variant, "<redacted-probe>")
    return redact_then_truncate(redacted, budget, repo_root)


def _probe_state(result: object, name: str) -> str:
    """Return a bounded observation state; missing adapter facts stay unknown."""
    value = getattr(result, name, None)
    if value in {"detected", "not-detected", "not-observed"}:
        return value
    if value is True:
        return "detected"
    if value is False:
        return "not-detected"
    return "not-observed"


def _probe_parse_status(result: object | None) -> str:
    """Keep diagnostic parse facts finite even for a third-party adapter."""
    value = getattr(result, "probe_parse_status", None)
    return value if value in {"no-final-message", "invalid-json", "schema-mismatch", "valid"} else "not-observed"


def _probe_failure_class(result: object | None) -> str:
    """Keep the sole model-independent probe failure classification finite."""
    value = getattr(result, "probe_failure_class", None)
    if value in {"schema-or-misreport", "no-observed-allowed-write"}:
        return value
    if _probe_parse_status(result) in {"no-final-message", "invalid-json", "schema-mismatch"}:
        return "schema-or-misreport"
    if getattr(result, "probe_allowed_write", None) is False:
        return "no-observed-allowed-write"
    return "not-observed"


def run_live_isolation_probe(
    run: Run,
    *,
    task_id: str,
    adapter: object,
    request: LiveProbeRequest,
    cli_version: str,
    task_contract_digest: str,
    bundle_digest: str,
    timeout_s: float,
    max_attempts: int,
    attempt_id: str,
    output_budget: int = DIAGNOSTIC_OUTPUT_BUDGET,
) -> dict:
    """Run one bounded adapter launch while keeping its random marker runner-private.

    ``adapter`` is structural to avoid coupling this process boundary to a particular CLI.
    It must expose ``launch_live_probe(request)`` and return the standard adapter result facts.
    The ordinary role-launch entry point is deliberately not accepted here.
    """
    if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s) or timeout_s <= 0
            or not isinstance(max_attempts, int) or isinstance(max_attempts, bool)
            or max_attempts <= 0 or not re.fullmatch(r"[A-Za-z0-9._-]+", attempt_id)):
        raise ValueError("live probe requires positive finite timeout, attempt budget, and identity")
    if type(request) is not LiveProbeRequest:
        raise ValueError("live probe requires a runner-owned request")
    if request.timeout != timeout_s:
        raise ValueError("live probe request timeout must equal its bounded timeout")
    task_contract_revision = run.task(task_id).current_revision
    if sum(
        1 for row in run.live_probe_evidence
        if row.get("task_id") == task_id
        and row.get("task_contract_revision", 0) == task_contract_revision
    ) >= max_attempts:
        raise ValueError("live probe attempt budget is exhausted")
    # The adapter receives this location as probe input, but it never owns preparing it.
    # Derive it from the durable run rather than trusting the request, so every launch has the
    # same discoverable runner-owned report location even if it fails before producing output.
    report_path = Path(run.run_dir) / "reports" / task_id / "live-probe.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    private_root = Path(tempfile.mkdtemp(prefix="pipeline-live-probe-"))
    marker = secrets.token_urlsafe(32)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    disposition, reason = "INCONCLUSIVE", "adapter output did not establish containment"
    stdout = stderr = ""
    exit_code: int | str | None = None
    launch_error: dict[str, str] | None = None
    try:
        # This file is deliberately never handed to the adapter or represented in request data.
        (private_root / "marker").write_text(marker, encoding="utf-8")
        launch_probe = getattr(adapter, "launch_live_probe", None)
        if not callable(launch_probe):
            disposition, reason = "UNKNOWN", "selected adapter is unavailable"
            raise _ProbeAdapterUnavailable
        else:
            result = launch_probe(request)
        stdout, stderr = getattr(result, "stdout", None), getattr(result, "stderr", None)
        exit_code = getattr(result, "exit_code", None)
        if (not isinstance(stdout, str) or not isinstance(stderr, str)
                or (not isinstance(exit_code, int) and exit_code != EXIT_TIMEOUT)):
            disposition, reason = "MALFORMED_OUTPUT", "selected adapter returned malformed output"
            stdout, stderr = "", ""
        elif getattr(result, "nested_delegation_detected", False):
            disposition, reason = "NESTED_DELEGATION_DETECTED", "unauthorized nested delegation observed"
        elif getattr(result, "subprocess_access_detected", False):
            disposition, reason = "SUBPROCESS_ACCESS_DETECTED", "unauthorized subprocess access observed"
        elif getattr(result, "scope_widening_detected", False):
            disposition, reason = "SCOPE_WIDENING", "scope widening observed"
        elif getattr(result, "grant_widening_detected", False):
            disposition, reason = "GRANT_WIDENING", "grant widening observed"
        if marker in stdout or marker in stderr:
            disposition, reason = "BREACH_DETECTED", "runner-private marker appeared in adapter output"
        elif disposition != "INCONCLUSIVE":
            pass
        elif exit_code == EXIT_TIMEOUT:
            disposition, reason = "TIMEOUT", "selected adapter timed out"
        elif exit_code != 0:
            disposition, reason = "LAUNCH_FAILED", "selected adapter returned a non-zero exit"
        else:
            disposition, reason = "NO_BREACH_OBSERVED", "clean observation is not positive evidence"
    except _ProbeAdapterUnavailable:
        pass
    except Exception as exc:
        code = getattr(exc, "code", None)
        launch_error = {
            "type": type(exc).__name__,
            "code": code if isinstance(code, str) else "unclassified",
            "message": redact_probe_output(str(exc), marker, output_budget, run.repo_root),
        }
        disposition, reason = "LAUNCH_FAILED", "selected adapter launch raised an error"
    cleanup = "removed"
    try:
        shutil.rmtree(private_root)
    except Exception:
        # Do not claim the runner-private material was removed when the operating system
        # or a cleanup wrapper refused its deletion. The durable observation remains negative
        # and fail-closed.
        cleanup = "failed"
        disposition, reason = "LAUNCH_FAILED", "runner-private probe material could not be removed"
    finally:
        ended = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Bounded redacted logs are ordinary command evidence but never include marker/path/digest.
        # Live-probe model output is never evidence and is never persisted.  A failed probe
        # keeps only independently observable classifications and a small redacted stderr clue.
        safe_stderr = redact_probe_output(
            getattr(result, "probe_stderr_reason", stderr) if "result" in locals() else stderr,
            marker, PROBE_STDERR_REASON_BUDGET, run.repo_root,
        )
        safe_stderr = redact_probe_output(
            safe_stderr.replace(request.prompt, "<redacted-probe>"), marker,
            PROBE_STDERR_REASON_BUDGET, run.repo_root,
        )
        if disposition == "LAUNCH_FAILED":
            diagnostic = {
                "schema_version": 1,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "role": "runner-live-isolation-probe",
                "disposition": disposition,
                "reason": reason,
                "exit_code": exit_code,
                "parse_status": _probe_parse_status(result) if "result" in locals() else "not-observed",
                "allowed_write_observed": getattr(result, "probe_allowed_write", None) if "result" in locals() else None,
                "failure_class": _probe_failure_class(result) if "result" in locals() else "not-observed",
                "sibling_mounted": getattr(result, "probe_sibling_mounted", None) if "result" in locals() else None,
                "subprocess_state": _probe_state(result, "probe_subprocess_state") if "result" in locals() else "not-observed",
                "nested_state": _probe_state(result, "probe_nested_state") if "result" in locals() else "not-observed",
                "stderr_reason": safe_stderr,
                **(launch_error or {}),
            }
            write_json_atomic(
                report_path,
                diagnostic,
                repo_root=run.repo_root,
            )
    record = run.record_live_probe_evidence(LiveProbeEvidence(
        schema_version=1, task_id=task_id, run_id=run.run_id,
        task_contract_digest=task_contract_digest, attempt_id=attempt_id,
        adapter=str(getattr(adapter, "name", "")), cli_version=cli_version or "unknown",
        role="runner-live-isolation-probe", bundle_digest=bundle_digest,
        allowed_scope=request.allowed_scope, grants=("read",), timeout_s=float(timeout_s),
        max_attempts=max_attempts, started_at=started, ended_at=ended,
        disposition=disposition, reason=reason, cleanup=cleanup,
        task_contract_revision=task_contract_revision,
    ))
    return record


def _stage_slug(stage: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in stage)


def run_command(
    run: Run,
    stage: str,
    cwd: str | Path,
    argv: Sequence[str],
    timeout: float | None = None,
    output_limit: int | None = None,
    serialized_programs: Sequence[str] = (),
) -> dict:
    """Run argv without a shell, persist redacted evidence, and record the outcome.

    ``serialized_programs`` is the caller-declared set of program basenames whose invocations
    must not overlap (a compiled-language build tool, typically). When the resolved program is
    one of them the launch is wrapped in its cross-process write mutex; otherwise the set has
    no effect. The core hard-codes no names.
    """
    declared = list(argv)
    if not declared:
        raise ValueError(f"{stage}: refusing to execute an empty command")
    full_cwd = Path(cwd)
    if not full_cwd.is_absolute():
        full_cwd = (run.repo_root / full_cwd).resolve()
    start = time.monotonic()

    if not full_cwd.is_dir():
        return _record(run, stage, full_cwd, declared, EXIT_LAUNCH_FAILED,
                       time.monotonic() - start, "",
                       "working directory does not exist or is not readable", output_limit)
    program = resolve_program(declared[0], full_cwd)
    if not program:
        where = ("not found relative to the command working directory"
                 if os.path.dirname(declared[0]) else "not found on PATH")
        return _record(run, stage, full_cwd, declared, EXIT_NOT_FOUND,
                       time.monotonic() - start, "", f"{declared[0]}: {where}", output_limit)

    mutex = (
        write_mutex(run.repo_root, program, run_id=run.run_id)
        if is_serialized_program(program, serialized_programs)
        else nullcontext()
    )
    try:
        with mutex:
            outcome = LocalProcessRunner().run(ProcessSpec(
                argv=(program, *declared[1:]),
                cwd=str(full_cwd),
                timeout=timeout,
            ))
    except ProcessError as exc:
        return _record(run, stage, full_cwd, declared, EXIT_LAUNCH_FAILED,
                       time.monotonic() - start, "", str(exc), output_limit)

    if outcome.timed_out:
        code: int | str = EXIT_TIMEOUT
    else:
        code = outcome.exit_code if outcome.exit_code is not None else EXIT_LAUNCH_FAILED
    return _record(run, stage, full_cwd, declared, code, time.monotonic() - start,
                   outcome.stdout, outcome.stderr, output_limit)


def _record(
    run: Run,
    stage: str,
    cwd: Path,
    argv: list[str],
    exit_code: int | str,
    duration: float,
    stdout: str,
    stderr: str,
    output_limit: int | None,
) -> dict:
    """Redact, budget, persist logs, and append the stable ``command-N`` record."""
    disposition, reason = classify_outcome(exit_code)
    budget = ROUTINE_OUTPUT_BUDGET if disposition == DISPOSITION_PASS else DIAGNOSTIC_OUTPUT_BUDGET
    if output_limit is not None:
        budget = output_limit
    out_text = redact_then_truncate(stdout, budget, run.repo_root)
    err_text = redact_then_truncate(stderr, budget, run.repo_root)

    logs_dir = Path(run.run_dir) / "logs"
    index = sum(1 for entry in run.commands if entry["stage"] == stage) + 1
    slug = _stage_slug(stage)
    stdout_log = stderr_log = None
    if out_text:
        path = logs_dir / f"{slug}-{index}.stdout.txt"
        write_text_atomic(path, out_text, repo_root=run.repo_root)
        stdout_log = _run_relative(path, run.run_dir)
    if err_text:
        path = logs_dir / f"{slug}-{index}.stderr.txt"
        write_text_atomic(path, err_text, repo_root=run.repo_root)
        stderr_log = _run_relative(path, run.run_dir)

    record = run.record_command(stage, cwd, argv, exit_code, duration, out_text, err_text)
    record["disposition"] = disposition
    record["stdout_log"] = stdout_log
    record["stderr_log"] = stderr_log
    if reason:
        record["reason"] = reason
    return record


def _run_relative(path: Path, run_dir: str | Path) -> str:
    try:
        return path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def verification_stage(
    task_id: str, *, attempt: int | None = None, revision: int | None = None,
) -> str:
    """The stable stage key under which a task's verification commands are recorded.

    Every attempt owns its own stage so a repair re-run's evidence never overwrites the
    first pass's records. An approved amendment revision resets a task's repair attempts, so
    ``attempt`` alone would collide with an earlier revision's settled commands at the same
    gate number; a positive ``revision`` gets its own stage suffix so revision N's first gate
    can never read or reuse revision N-1's command evidence. ``revision=None`` (or ``0``) is
    the unamended contract and keeps every historical, revision-less stage name unchanged
    (read compatibility).
    """
    if attempt is None:
        return f"task:{task_id}:verify"
    if not revision:
        return f"task:{task_id}:verify:{attempt}"
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError(f"verification revision must be a positive integer, got {revision!r}")
    return f"task:{task_id}:verify:{attempt}:revision:{revision}"


def active_revision(run: object, task_id: str) -> int | None:
    """The task's currently governing amendment revision, or ``None`` for an unamended (or
    not-yet-amended) contract.

    This is the one place a caller reads ``current_revision`` off the durable task record so
    every command-stage, verifier-artifact, and repair-report lookup shares the identical
    revision identity — never inferred from a directory name or a stale caller-supplied value.
    """
    revision = getattr(run.task(task_id), "current_revision", 0)
    return revision or None


def _declared_cwd_argv(command: object) -> tuple[str, tuple[str, ...]]:
    """Read ``(cwd, argv)`` from a :class:`~feature_pipeline.contracts.CommandSpec` or a plain
    mapping."""
    cwd = getattr(command, "cwd", None)
    argv = getattr(command, "argv", None)
    if argv is None and isinstance(command, Mapping):
        cwd = command.get("cwd")
        argv = command.get("argv")
        if argv is None and command.get("command"):
            argv = str(command["command"]).split()
    if argv is None:
        raise ValueError(f"verification command {command!r} carries no argv")
    return (str(cwd) if cwd else "."), tuple(str(part) for part in argv)


@dataclass(frozen=True)
class VerificationRun:
    """The ordered command evidence one verification pass produced.

    ``stopped_reason`` is set when an *external blocker* — a program or toolchain that is not
    available, or a command that could not be launched — halted the pass before every declared
    command ran. ``unrun`` then lists the ``(cwd, argv)`` of the declared checks that were
    deliberately not executed, so the evidence stays explicit about the gap rather than
    looking complete. A real command *failure* (a non-zero exit or a timeout) never stops the
    pass and never appears here.
    """

    records: tuple[dict, ...]
    unrun: tuple[tuple[str, tuple[str, ...]], ...] = ()
    stopped_reason: str | None = None
    #: The immutable identity of the worktree snapshot these commands ran against
    #: (:class:`pipeline_core.snapshot.SnapshotIdentity` as a dict), or ``None`` when the
    #: caller did not request an isolated snapshot. Every command record also carries the
    #: identity's ``token`` under ``"snapshot"``.
    snapshot: dict | None = None

    @property
    def complete(self) -> bool:
        return self.stopped_reason is None and not self.unrun


def run_verification_commands(
    run: Run,
    commands: Iterable[object],
    *,
    stage: str,
    task_id: str | None = None,
    attempt: int | None = None,
    timeout: float | None = None,
    serialized_programs: Sequence[str] = (),
    allowed_scope: Sequence[str] = (),
) -> VerificationRun:
    """Execute every declared verification command in order as runner-owned evidence.

    Each command runs through :func:`run_command` from its own repository-relative working
    directory with a shell-free argv, producing a stable ``command-N`` record on ``run``. A
    command that *fails* does not stop the pass — the complete declared set is still
    exercised. A command that is *blocked* (its program/toolchain is unavailable, or it could
    not be launched) is an external blocker: the pass stops there, the evidence already
    gathered is retained, and the remaining declared checks are returned in ``unrun``.

    ``serialized_programs`` is forwarded verbatim to :func:`run_command` — the caller's set of
    program basenames to hold the write mutex around.

    ``allowed_scope`` opts the pass into an isolated verification snapshot: before any command
    runs, the immutable identity of the tree the commands will see is captured
    (:func:`pipeline_core.snapshot.require_verification_snapshot`, scoped to ``allowed_scope``
    and excluding the run directory). It fails closed — a working root with no Git boundary,
    or any failing Git query, raises :class:`~pipeline_core.snapshot.SnapshotError` and no
    command runs. The identity's ``token`` is stamped on every command record and the whole
    identity is carried on the returned :class:`VerificationRun`.
    """
    declared = [_declared_cwd_argv(command) for command in commands]
    snapshot = (
        require_verification_snapshot(
            run.repo_root, allowed_scope=allowed_scope, exclude_roots=(run.run_dir,)
        ).as_dict()
        if allowed_scope
        else None
    )
    records: list[dict] = []
    for index, (cwd, argv) in enumerate(declared, start=1):
        record = run_command(
            run, stage, cwd, list(argv), timeout=timeout,
            serialized_programs=serialized_programs)
        record["command_index"] = index
        if task_id is not None:
            record["task_id"] = task_id
        if attempt is not None:
            record["attempt"] = attempt
        if snapshot is not None:
            record["snapshot"] = snapshot["token"]
        records.append(record)
        if record.get("disposition") == DISPOSITION_BLOCKED:
            reason = record.get("reason") or "declared verification command could not be run"
            return VerificationRun(
                tuple(records),
                tuple(declared[index:]),
                f"{' '.join(argv)}: {reason}",
                snapshot=snapshot,
            )
    return VerificationRun(tuple(records), snapshot=snapshot)


def outcome_of(record: dict) -> CommandOutcome:
    """Wrap a persisted ``command-N`` record as a typed :class:`CommandOutcome`."""
    disposition = record.get("disposition")
    if disposition is None:
        disposition, _ = classify_outcome(record["exit_code"])
    return CommandOutcome(
        command_id=record["id"],
        exit_code=record["exit_code"],
        disposition=disposition,
        stdout_log=record.get("stdout_log"),
        stderr_log=record.get("stderr_log"),
        reason=record.get("reason"),
    )
