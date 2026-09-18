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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from feature_pipeline.application.diagnostic_service import DiagnosticService
from feature_pipeline.application.work_items import WorkItemError, require_active_work_item

from .adapters import (
    Adapter,
    AdapterError,
    LaunchRequest,
    LaunchResult,
    grant_tool_names,
    normalize_role,
)
from .artifacts import write_json_atomic, write_text_atomic
from .commands import VerificationRun
from .reports import (
    ReportError,
    VerdictResolution,
    build_verdict_envelope_prompt,
    parse_verifier_verdict,
    settle_verifier_verdict,
    verifier_artifacts,
)
from .state import ACTOR_RUNNER, Run


VERDICTS = frozenset({"PASS", "FAIL", "BLOCKED"})

#: Every verifier-failure diagnostic is assembled through the one service path so it carries
#: real ``git diff`` / ``git status`` evidence — or an explicit collection failure (AC-2).
_DIAGNOSTICS = DiagnosticService()

#: The command-record keys carried into the evidence payload. Redacted, budgeted stdout/stderr
#: text stays in the persisted log files; the payload references those logs, it never inlines
#: their content (Implementation Notes).
_COMMAND_REFERENCE_KEYS = (
    "id", "stage", "cwd", "argv", "exit_code", "disposition", "duration",
    "stdout_log", "stderr_log", "reason", "command_index", "task_id", "attempt",
    "snapshot", "revision",
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


def remote_evidence_required(spec: object) -> bool:
    """Whether the task's acceptance contract requires a remote observation.

    Executors never obtain push or remote-inspection authority.  This deliberately narrow
    classifier identifies the acceptance language that instead needs a runner observation.
    It is intentionally based on the task contract, not on an executor report.
    """
    terms = ("push", "pull request", "github actions", "required check", "remote")
    criteria = getattr(spec, "acceptance_criteria", ()) or ()
    return any(any(term in str(criterion).casefold() for term in terms) for criterion in criteria)


def _executor_claims_remote_action(report: str | None) -> bool:
    if not report:
        return False
    text = report.casefold()
    return bool(re.search(
        r"\bpushed\b|\b(?:github actions|required checks?)\b.{0,80}\b(?:green|pass(?:ed)?|success)\b|"
        r"\b(?:pull request|pr)\b.{0,80}\b(?:publish(?:ed)?|create(?:d)?|update(?:d)?)\b",
        text,
    ))


def _executor_report_claimed_remote_action(run: object, report_reference: object) -> bool:
    """Read the runner-recorded executor report only to reject an unbacked remote claim.

    The report remains executor-controlled prose and is never evidence of a remote action.
    A missing or unreadable report returns ``False`` here; required remote evidence still fails
    closed independently, without inventing a claim or an observation.
    """
    if not isinstance(report_reference, str) or not report_reference:
        return False
    try:
        root = Path(run.repo_root).resolve()
        report = (root / report_reference).resolve()
        report.relative_to(root)
        return _executor_claims_remote_action(report.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False


def remote_evidence_failure(spec: object, evidence: "VerificationEvidence") -> str | None:
    """Validate runner-owned remote evidence required by an acceptance contract.

    Remote facts must be explicit, successful observations recorded by the runner.  Missing,
    malformed, or executor-only evidence is a fail-closed condition; no report prose can stand
    in for a remote readback.
    """
    if not remote_evidence_required(spec):
        return None
    if not evidence.remote_evidence:
        if evidence.executor_remote_claimed or _executor_claims_remote_action(evidence.executor_report):
            return "executor reported remote action without runner-recorded remote evidence"
        return "required remote acceptance evidence is missing"
    for record in evidence.remote_evidence:
        if (
            record.get("observed_by") != "runner"
            or record.get("outcome") != "succeeded"
            or not isinstance(record.get("kind"), str)
            or not record.get("kind")
            or not isinstance(record.get("subject"), str)
            or not record.get("subject")
            or not isinstance(record.get("recorded_at"), str)
            or not record.get("recorded_at")
        ):
            return "remote acceptance evidence is missing or inconsistent"
    criteria = "\n".join(str(criterion).casefold() for criterion in (
        getattr(spec, "acceptance_criteria", ()) or ()
    ))
    kinds = {str(record["kind"]) for record in evidence.remote_evidence}
    if ("push" in criteria or "pull request" in criteria) and "pull-request-publication" not in kinds:
        return "remote acceptance evidence is missing or inconsistent"
    if ("github actions" in criteria or "required check" in criteria) and "required-checks" not in kinds:
        return "remote acceptance evidence is missing or inconsistent"
    return None


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
    #: The runner's rejection-only classification of the recorded executor report.  It never
    #: proves a remote fact; it prevents executor prose from being mistaken for one.
    executor_remote_claimed: bool = False
    implementation_manifest: str | None = None
    implementation_diff: str | None = None
    changed_files: tuple[Mapping[str, object], ...] = ()
    #: Content-addressed lifecycle projections written by the runner before this executor
    #: window. They explain protected ambient files without turning them into executor work.
    runner_owned_writes: tuple[Mapping[str, object], ...] = ()
    #: Runner-recorded actions performed outside the local implementation window (for
    #: example a push, tag, or remote-ruleset mutation).  It is deliberately explicit
    #: even when empty: task history and ambient Git state are not substitutes for it.
    external_actions: tuple[Mapping[str, object], ...] = ()
    #: Read-only observations made and persisted by the runner after executor work.  These are
    #: facts about a remote system, never executor claims or capabilities.
    remote_evidence: tuple[Mapping[str, object], ...] = ()
    missing_evidence: tuple[Mapping[str, object], ...] = ()
    unrun_commands: tuple[Mapping[str, object], ...] = ()
    external_blocker: str | None = None
    #: The approved amendment revision currently governing this verification pass.  It is
    #: separate from an out-of-scope observation: the latter is a reason to request an
    #: amendment, never proof that one was approved.
    amendment: Mapping[str, object] | None = None
    #: Runner-observed expansion facts from the executor window.  Unlike ``amendment``, this
    #: cannot authorize anything; it remains visible so missing approval is reviewable.
    scope_observation: Mapping[str, object] | None = None
    #: The immutable identity of the isolated worktree snapshot the recorded commands ran
    #: against (:class:`pipeline_core.snapshot.SnapshotIdentity` as a dict), or ``None`` when
    #: the pass was not run against an isolated snapshot. An unrelated later worktree edit
    #: cannot move this value — it is a frozen field over a captured content digest.
    snapshot: Mapping[str, object] | None = None
    #: Durable, runner-owned operation transitions for this logical task.  Tool-less
    #: verifiers need these facts to assess resume/escalation criteria without treating
    #: historical Markdown projections as evidence.
    operation_history: tuple[Mapping[str, object], ...] = ()

    @property
    def complete(self) -> bool:
        """Every declared check produced a command record and nothing blocked the pass."""
        return self.external_blocker is None and not self.unrun_commands

    def as_dict(self) -> dict[str, object]:
        observed_paths = [
            str(entry.get("path", ""))
            for entry in self.changed_files
            if entry.get("classification") == "out_of_scope"
        ]
        amendment = dict(self.amendment) if self.amendment is not None else {}
        observation = dict(self.scope_observation) if self.scope_observation is not None else {}
        approved_paths = [str(path) for path in amendment.get("added_paths", ())]
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "executor_report": self.executor_report,
            "executor_remote_claimed": self.executor_remote_claimed,
            "implementation": {
                "manifest": self.implementation_manifest,
                "diff": self.implementation_diff,
                "changed_files": [dict(entry) for entry in self.changed_files],
            },
            "runner_owned_writes": [dict(entry) for entry in self.runner_owned_writes],
            "commands": [dict(entry) for entry in self.commands],
            "external_actions": [dict(entry) for entry in self.external_actions],
            "remote_evidence": [dict(entry) for entry in self.remote_evidence],
            "missing_evidence": [dict(entry) for entry in self.missing_evidence],
            "unrun_commands": [dict(entry) for entry in self.unrun_commands],
            "external_blocker": self.external_blocker,
            "durable_operation_history": [dict(entry) for entry in self.operation_history],
            "scope_amendment": {
                "present": bool(amendment or observation or observed_paths),
                "revision": amendment.get("revision"),
                "epoch": amendment.get("epoch"),
                "approved_by": amendment.get("approved_by"),
                "approved_paths": approved_paths,
                "observed_paths": observation.get("observed_paths", observed_paths),
                "observed_changes": observation.get("observed_changes", []),
                "original_allowed_scope": observation.get("original_allowed_scope", []),
                "original_out_of_scope": observation.get("original_out_of_scope", []),
                "original_acceptance_criteria": observation.get(
                    "original_acceptance_criteria", []),
                "approval": observation.get("approval"),
                "rationale": amendment.get("rationale") or observation.get("rationale") or (
                    "executor-owned paths outside the initial estimate require independent "
                    "amendment-justification review" if observed_paths else None
                ),
            },
            "snapshot": dict(self.snapshot) if self.snapshot is not None else None,
            "current_run_boundary": {
                "verification_snapshot": dict(self.snapshot) if self.snapshot is not None else None,
                "implementation": {
                    "manifest": self.implementation_manifest,
                    "diff": self.implementation_diff,
                    "changed_files": [dict(entry) for entry in self.changed_files],
                },
                "runner_owned_writes": [dict(entry) for entry in self.runner_owned_writes],
                "captured_commands": [dict(entry) for entry in self.commands],
                "durable_operation_history": [dict(entry) for entry in self.operation_history],
                "external_actions": [dict(entry) for entry in self.external_actions],
                "remote_evidence": [dict(entry) for entry in self.remote_evidence],
                "executor_remote_claimed": self.executor_remote_claimed,
            },
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
    runner_writes: list[dict[str, object]] = []
    # Runner lifecycle projections can belong to earlier tasks (for example TC-01/TC-02
    # updating the shared board before TC-03 starts).  Carry their durable owner into this
    # task's evidence so ambient protected files are explainable without treating them as
    # implementation.  The executor manifest remains the sole source of executor changes.
    for owner_id, owner in sorted(getattr(run, "tasks", {}).items()):
        for entry in getattr(owner, "runner_owned_writes", ()):
            row = dict(entry)
            row["task_id"] = owner_id
            runner_writes.append(row)
    amendment = None
    if record.current_revision:
        amendment = next(
            (dict(revision) for revision in record.amendment_revisions
             if revision.get("revision") == record.current_revision),
            None,
        )
        if amendment is None:
            raise VerificationError(
                f"task {task_id} has active amendment revision {record.current_revision} "
                "without its immutable revision record")

    return VerificationEvidence(
        task_id=task_id,
        attempt=attempt,
        commands=references,
        executor_report=execution.get("executor_report"),
        executor_remote_claimed=_executor_report_claimed_remote_action(
            run, execution.get("executor_report")),
        implementation_manifest=implementation.get("manifest"),
        implementation_diff=implementation.get("diff"),
        changed_files=tuple(dict(entry) for entry in implementation.get("changed_files") or ()),
        runner_owned_writes=tuple(runner_writes),
        external_actions=tuple(
            dict(entry) for entry in execution.get("external_actions") or ()
        ),
        remote_evidence=tuple(
            dict(entry) for entry in execution.get("remote_evidence") or ()
        ),
        missing_evidence=missing_command_evidence(claimed_checks, commands_run.records),
        unrun_commands=unrun,
        external_blocker=commands_run.stopped_reason,
        snapshot=commands_run.snapshot,
        amendment=amendment,
        scope_observation=(
            dict(implementation["scope_amendment"])
            if isinstance(implementation.get("scope_amendment"), Mapping) else None
        ),
        operation_history=tuple(
            dict(entry) for entry in getattr(record, "operation_history", ())
        ),
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


def current_run_mutation_reason(spec: object, evidence: VerificationEvidence) -> str | None:
    """Return a runner-fact failure for an explicit current-run no-mutation criterion.

    Ordinary criteria stay with the independent verifiers. A task author must both scope a
    criterion to this/current run and phrase it as a prohibition before captured mutations
    settle it.
    """
    criteria = getattr(spec, "acceptance_criteria", ()) or ()
    scoped_prohibition = any(
        _current_run_marker(text) and "must not" in str(text).casefold()
        for text in criteria
    )
    if not scoped_prohibition:
        return None
    if evidence.external_actions:
        return "runner captured current-run external action: " + str(
            evidence.external_actions[0].get("action", "mutation"))
    for changed in evidence.changed_files:
        path = str(changed.get("path", "")).replace("\\", "/")
        if path.startswith(".github/workflows/") or ".github/workflows/" in path:
            return f"runner captured current-run workflow change: {path}"
    return None


def combine_verdict_status(task_verdict: str, test_verdict: str) -> str:
    """Return product status implied by verdicts; only two PASS verdicts complete work."""
    return "done" if task_verdict == test_verdict == "PASS" else "in_progress"


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
        "Judge task requirements and acceptance criteria from the task-relevant snapshot and "
        "runner-owned command evidence below, not whole-worktree git diff as a completion proxy.",
        "Remote publication and required-check facts must come from runner-owned remote_evidence; "
        "executor report prose is never remote evidence.",
        "You may read the worktree; you may not modify anything and you may not run the "
        "verification commands — their outcomes are the runner-owned evidence below.",
        "Treat implementation manifest/diff deltas only as supplementary allowed-scope checks; "
        "they do not establish task completion by themselves.",
        "Runner-owned writes identify lifecycle projections. Never charge those earlier writes "
        "to the executor; only changes attributed inside this executor window may be scope "
        "violations. An unavailable executor attribution is BLOCKED.",
        "Do not tick acceptance-criteria checkboxes.",
        "Do not require the outcome of this same verification pass as evidence. Evaluate "
        "failure, escalation, and resume criteria from durable runner-owned operation history "
        "and task-scoped regression evidence; your fresh verdict is the output being settled.",
        "Your report must include a separate 'Amendment-justification finding:' that states "
        "whether the structured scope-amendment rationale and observed paths support the "
        "claimed functionality (or that no amendment is present). When amendment evidence is "
        "present, repeat its exact 'revision N' and 'epoch N' identities and its rationale.",
        "For an acceptance criterion marked CURRENT-RUN ONLY, assess mutations only from "
        "the current_run_boundary in the runner-owned evidence: its verification snapshot, "
        "implementation manifest/diff and changed files, captured commands, and external "
        "actions. Ignore historical ## Result sections, pre-existing commits/tags/pushes, "
        "and any other ambient worktree or remote history for that criterion. A current-run "
        "commit, tag, push, workflow change, ruleset mutation, or recorded external action "
        "inside that boundary remains evidence against it.",
    ),
    "test_verifier": (
        "You have no tools. Interpret only the runner-owned evidence below; run nothing.",
        "A declared verification command with no matching runner-recorded command is a FAIL, "
        "never a prompt to re-run the check.",
        "When remote acceptance is required, missing or inconsistent runner-owned remote_evidence "
        "is a FAIL; never infer it from executor report prose.",
        "Judge whether the recorded command evidence shows the task's verification commands "
        "passed.",
        "Do not require a PASS from either verifier in this same verification pass as evidence: "
        "your verdict and the companion verifier's verdict are the outputs being independently "
        "settled. Assess historical lifecycle criteria from durable runner-owned history and "
        "task-scoped regression evidence instead.",
        "Your verdict is limited to runner-recorded verification-command evidence: PASS when "
        "every declared command has a matching record with exit code 0 and there are no "
        "missing, unrun, or externally blocked commands; otherwise FAIL or BLOCKED as the "
        "evidence requires. Do not fail because the command records alone do not demonstrate "
        "functional failure/resume scenarios; the independent task verifier assesses those "
        "acceptance criteria from the durable operation evidence.",
        "Your report must include a separate 'Amendment-justification finding:' that states "
        "whether the structured scope-amendment rationale and observed paths support the "
        "claimed functionality (or that no amendment is present). When amendment evidence is "
        "present, repeat its exact 'revision N' and 'epoch N' identities and its rationale.",
    ),
}


def _acceptance_block(spec: object) -> str:
    criteria = getattr(spec, "acceptance_criteria", ()) or ()
    if not criteria:
        return "  none declared"
    return "\n".join(
        f"  - {ac.id}: {ac.text}{_current_run_marker(ac.text)}" for ac in criteria
    )


def _current_run_marker(text: object) -> str:
    """Label criteria whose wording deliberately limits their evidence to this run.

    The verifier still evaluates ordinary criteria exactly as before.  This marker merely
    makes the task author's explicit ``this run``, ``current run``, ``run-scoped``, or
    ``CURRENT-RUN ONLY`` boundary executable in the prompt, rather than leaving a historical
    task result to redefine that scope.
    """
    words = str(text).casefold().replace("-", " ")
    markers = ("this run", "current run", "run scoped", "current run only")
    if any(marker in words for marker in markers):
        return " [CURRENT-RUN ONLY]"
    return ""


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
        "- Amendment-justification finding:",
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


_EXPLICIT_REPORT_VERDICT = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:Verdict|Вердикт)\s*:\s*(?:[*_]\s*)*"
    r"(PASS|FAIL|BLOCKED)\b"
)


def _explicit_report_verdicts(text: str) -> frozenset[str]:
    """Return every labelled, normalized verdict stated in a verifier report."""
    return frozenset(match.group(1).upper() for match in _EXPLICIT_REPORT_VERDICT.finditer(text))


def _amendment_assessment_failure(
    report_text: str, amendment: Mapping[str, object] | None,
    scope_observation: Mapping[str, object] | None = None,
) -> str | None:
    """Require a PASS report to identify and assess its governing amendment revision."""
    if not amendment and not scope_observation:
        return None
    normalized = report_text.casefold()
    if "amendment-justification finding:" not in normalized:
        return "missing-amendment-justification-finding"
    if not amendment:
        # An observation never authorizes the executor by itself. It is the runner-owned
        # record the independent verifier must assess; a PASS with the required finding is
        # that assessment, rather than an external wait.
        return None
    # Revision identity and epoch are immutable runner-owned evidence. A verifier must repeat
    # the exact governing pair; otherwise a PASS could settle a different amendment epoch.
    revision = amendment.get("revision")
    epoch = amendment.get("epoch")
    identity = re.compile(
        rf"revision\D*{re.escape(str(revision))}\D{{0,120}}?epoch\D*{re.escape(str(epoch))}\b",
        re.IGNORECASE,
    )
    if not identity.search(report_text):
        return "amendment-justification-missing-revision-identity"
    return None


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
    diagnostic = _DIAGNOSTICS.collect(
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
    model: str | None = None, effort: str | None = None,
    amendment: Mapping[str, object] | None = None,
    scope_observation: Mapping[str, object] | None = None,
) -> _SettledVerifier:
    normalized = normalize_role(role)
    tool_less = normalized == "test_verifier"
    # The task verifier's prompt says "You may read the worktree"; grant exactly that and
    # nothing that writes. The test verifier stays deliberately tool-free (`no_tools=True`).
    verifier_grant: tuple[str, ...] = () if tool_less else ("read",)
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
        role_grant=verifier_grant,
        allowed_scope=tuple(getattr(spec, "allowed_scope", ()) or ()),
        fresh_session=True,
        resume_session_id=None,
        tools=grant_tool_names(verifier_grant),
        no_tools=tool_less,
        model=model,
        effort=effort,
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

    observed_verdict = None
    if getattr(adapter, "requires_fresh_envelope_context", False):
        try:
            observed_verdict = parse_verifier_verdict(report_text)
        except ReportError:
            pass

    envelope_request = LaunchRequest(
        role=normalized,
        task_id=spec.id,
        prompt=build_verdict_envelope_prompt(
            role=normalized,
            task_id=spec.id,
            attempt=attempt,
            observed_verdict=observed_verdict),
        report_path=artifacts.envelope(normalized),
        read_only=True,
        working_root=".",
        resume_session_id=result.session_id,
        no_tools=True,
        model=model,
        effort=effort,
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

    explicit_verdicts = _explicit_report_verdicts(report_text)
    if explicit_verdicts and explicit_verdicts != {resolution.token}:
        return _diagnose(
            run, spec, artifacts, role=normalized, attempt=attempt,
            reason=(f"verdict-envelope-mismatch: {normalized} report explicitly reported "
                    f"{sorted(explicit_verdicts)!r}, envelope reported {resolution.token!r}"),
            result=result, envelope_result=envelope_result, report_text=report_text)

    if resolution.token == "PASS":
        assessment_failure = _amendment_assessment_failure(
            report_text, amendment, scope_observation)
        if assessment_failure:
            return _diagnose(
                run, spec, artifacts, role=normalized, attempt=attempt,
                reason=assessment_failure, result=result,
                envelope_result=envelope_result, report_text=report_text)

    return _SettledVerifier(
        token=resolution.token, result=result, envelope_result=envelope_result,
        report_text=report_text, drift=resolution.drift, failure=None, diagnostic=None,
    )


def _block(run: Run, task_id: str, reason: str) -> str:
    """Record an unavailable verifier as an operation, leaving the task resumable."""
    record = run.task(task_id)
    if record.status == "to_do":
        run.transition_task(task_id, "in_progress", actor=ACTOR_RUNNER,
                            note="verification operation started")
    run.record_operation(task_id, "verification", "blocked", reason)
    run.record_event(f"verification-wait:{task_id}", to="blocked", note=reason)
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
    model: str | None = None,
    effort: str | None = None,
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
    try:
        require_active_work_item(run, task_id)
    except WorkItemError as exc:
        raise VerificationError(f"{exc.code}: {exc}") from None
    record = run.task(task_id)
    if record.status != "in_progress":
        raise VerificationError(
            f"{task_id} must be 'in_progress' to verify independently, is '{record.status}'")
    if evidence.task_id != task_id or evidence.attempt != attempt:
        raise VerificationError(
            "verification evidence does not match the task/attempt being verified")

    revision = None
    if evidence.amendment is not None:
        revision = evidence.amendment.get("revision")
    artifacts = verifier_artifacts(run.run_dir, task_id, attempt, revision=revision)
    artifacts.directory.mkdir(parents=True, exist_ok=True)
    payload = verifier_evidence_payload(evidence)

    task_settled = _run_one_verifier(
        run, spec, "task_verifier", launchers.task, artifacts, anchors, payload, attempt,
        plan_path, model, effort, evidence.amendment, evidence.scope_observation)
    if task_settled.failure:
        status = _block(run, task_id, f"task_verifier: {task_settled.failure}")
        return VerificationOutcome(
            task_id=task_id, attempt=attempt, status=status,
            task_result=task_settled.result, diagnostic=task_settled.diagnostic,
            failure=task_settled.failure)

    test_settled = _run_one_verifier(
        run, spec, "test_verifier", launchers.test, artifacts, anchors, payload, attempt,
        plan_path, model, effort, evidence.amendment, evidence.scope_observation)
    if test_settled.failure:
        status = _block(run, task_id, f"test_verifier: {test_settled.failure}")
        return VerificationOutcome(
            task_id=task_id, attempt=attempt, status=status,
            task_result=task_settled.result, test_result=test_settled.result,
            diagnostic=test_settled.diagnostic, failure=test_settled.failure)

    task_verdict = task_settled.token
    test_verdict = test_settled.token
    mutation_forced = current_run_mutation_reason(spec, evidence)
    test_forced = evidence_forces_fail(evidence)
    remote_forced = remote_evidence_failure(spec, evidence)
    forced_reason: str | None = None
    if mutation_forced and (task_verdict != "FAIL" or test_verdict != "FAIL"):
        forced_reason = mutation_forced
        task_verdict = "FAIL"
        test_verdict = "FAIL"
    elif remote_forced and (task_verdict != "FAIL" or test_verdict != "FAIL"):
        forced_reason = remote_forced
        task_verdict = "FAIL"
        test_verdict = "FAIL"
    elif test_forced and test_verdict != "FAIL":
        # An executor's unbacked verification claim is evidence only for the
        # tool-less test verifier.  It must not alter the task verifier's
        # ordinary acceptance-criteria verdict.
        forced_reason = test_forced
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
