"""Stages 14–16: final verification, the human final-diff gate, and a non-mutating release plan.

These three stages continue the one ordered post-task lifecycle
(:mod:`pipeline_core.post_task`) once Graphify verification (stage 13) has passed:

===== ===================== ================================================================
Stage Name                  Guard / behaviour
===== ===================== ================================================================
14    ``final-verification`` unreachable until stage 13 (``graphify-verification``) is
                            ``PASS`` (AC-1); runs **only** the project-declared final
                            verification commands, sequentially, recording each one's exact
                            argv / cwd / exit code; a command that cannot be launched is
                            reported as absent, never substituted
15    ``final-diff-approval`` a human gate: ``PASS`` only with an explicit approval; without
                            one it records ``BLOCKED`` (pending) but does **not** halt the
                            lifecycle — stage 16 still emits its dry run (AC-2)
16    ``release``           emits a deterministic, non-mutating release plan (files to stage,
                            proposed message, submodule pointer). A real commit would need an
                            explicit commit control **and** an approved final diff **and** a
                            passing final verification — and even then this module has no
                            commit or push code path, so nothing is executed (AC-2, AC-3)
===== ===================== ================================================================

The project's final verification set and release policy are declared in
``tools/feature-pipeline/config/release.json`` and loaded by :func:`load_release_policy`; the
final verification commands are resolved by name against the sibling ``checks.json`` so there
is one source of truth. Loading is fail-closed.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from schemas import SchemaError, validate_relative_path
from schemas.contracts import CommandSpec

from .commands import run_command
from .state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT
from .stages import BLOCKED, FAIL, PASS

#: The only ``schema_version`` this loader understands.
SUPPORTED_RELEASE_SCHEMA = 1

FINAL_VERIFICATION = "final-verification"
FINAL_DIFF_APPROVAL = "final-diff-approval"
RELEASE = "release"

#: The explicit, ordered release tail: ``(stage-number, stage-name)``.
RELEASE_STAGES: tuple[tuple[int, str], ...] = (
    (14, FINAL_VERIFICATION),
    (15, FINAL_DIFF_APPROVAL),
    (16, RELEASE),
)

#: Verbs a release plan must never name — this stage produces a plan, never a mutation.
FORBIDDEN_RELEASE_VERBS = frozenset({"push", "commit", "tag"})

#: ``run_command`` exit sentinels that mean "the command could not run" rather than "it ran
#: and failed" — stage 14 reports these as an absent check, never as a substituted pass.
_UNRUNNABLE = frozenset({EXIT_NOT_FOUND, EXIT_TIMEOUT, EXIT_LAUNCH_FAILED})

DiffProbe = Callable[[], str]
PointerProbe = Callable[[], str]

#: ``(verdict, reasons, evidence)`` — the raw shape a release stage computes;
#: :mod:`pipeline_core.post_task` wraps it with the stage name and number.
StageComputation = tuple[str, tuple[str, ...], dict[str, Any]]


class ReleaseError(RuntimeError):
    """A release-policy precondition failure. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReleasePolicy:
    """The project's declared final verification set and release staging policy."""

    final_verification: tuple[CommandSpec, ...]
    stage_paths: tuple[str, ...]
    submodule_pointer: str | None
    commit_message_template: str


# --- loading ---------------------------------------------------------------------------


def load_release_policy(path: str | Path) -> ReleasePolicy:
    """Read and validate ``release.json`` into a :class:`ReleasePolicy`.

    ``final_verification`` is a non-empty list of check names that must each be defined in the
    sibling ``checks.json``; every other document shape raises :class:`schemas.SchemaError`.
    Propagates :class:`FileNotFoundError` / :class:`json.JSONDecodeError` for the caller.
    """
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SchemaError("release.json must be an object")
    if raw.get("schema_version") != SUPPORTED_RELEASE_SCHEMA:
        raise SchemaError(
            f"release.json schema_version must be {SUPPORTED_RELEASE_SCHEMA}, "
            f"got {raw.get('schema_version')!r}"
        )

    checks = _load_sibling_checks(config_path.parent / "checks.json")
    names = raw.get("final_verification")
    if not isinstance(names, list) or not names:
        raise SchemaError(
            "release.json final_verification must be a non-empty list of check names")
    commands: list[CommandSpec] = []
    for name in names:
        if not isinstance(name, str) or name not in checks:
            raise SchemaError(
                f"release.json final_verification names a check not defined in checks.json: "
                f"{name!r}"
            )
        command = checks[name]
        _reject_mutating(command.argv, f"release.json final_verification[{name!r}]")
        commands.append(command)

    release = raw.get("release")
    if not isinstance(release, Mapping):
        raise SchemaError("release.json must carry a 'release' object")
    stage_paths_raw = release.get("stage_paths")
    if not isinstance(stage_paths_raw, list) or not stage_paths_raw:
        raise SchemaError("release.release.stage_paths must be a non-empty list")
    stage_paths = tuple(
        validate_relative_path(item, "release.release.stage_paths[]")
        for item in stage_paths_raw
    )
    pointer = release.get("submodule_pointer")
    if pointer is not None:
        pointer = validate_relative_path(pointer, "release.release.submodule_pointer")
    template = release.get("commit_message_template")
    if not isinstance(template, str) or not template.strip():
        raise SchemaError(
            "release.release.commit_message_template must be a non-empty string")
    if "\n" in template:
        raise SchemaError(
            "release.release.commit_message_template must be a single line")

    return ReleasePolicy(tuple(commands), stage_paths, pointer, template)


def _load_sibling_checks(checks_path: Path) -> dict[str, CommandSpec]:
    if not checks_path.is_file():
        raise SchemaError(
            "release.json needs a sibling checks.json to resolve final_verification names")
    try:
        doc = json.loads(checks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"checks.json beside release.json is not valid JSON: {exc}") from None
    if not isinstance(doc, Mapping):
        raise SchemaError("checks.json must be an object")
    resolved: dict[str, CommandSpec] = {}
    for index, entry in enumerate(doc.get("checks", [])):
        if not isinstance(entry, Mapping):
            raise SchemaError(f"checks.json checks[{index}] must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise SchemaError(f"checks.json checks[{index}].name must be a non-empty string")
        resolved[name] = CommandSpec.from_data(
            {"cwd": entry.get("cwd", "."), "argv": entry.get("argv")},
            field=f"checks.json checks[{index}]")
    return resolved


def _reject_mutating(argv: tuple[str, ...], field: str) -> None:
    bad = sorted({token.lower() for token in argv} & FORBIDDEN_RELEASE_VERBS)
    if bad:
        raise SchemaError(f"{field} names a mutating verb: {', '.join(bad)}")


# --- stage 14: final verification ----------------------------------------------------


def final_verification_result(
    run: object,
    policy: ReleasePolicy,
    *,
    command_runner: Callable[..., Mapping[str, object]] = run_command,
    timeout: float | None = None,
) -> StageComputation:
    """Run the declared final verification commands sequentially and judge the set.

    ``BLOCKED`` — a command could not be launched (reported as absent, never substituted) or
    the policy declares no commands. ``FAIL`` — a command ran and returned non-zero.
    """
    if not policy.final_verification:
        return (
            BLOCKED,
            ("no final verification commands are represented in the project profile "
             "(release.json final_verification is empty)",),
            {"commands": []},
        )

    records: list[dict[str, Any]] = []
    verdict = PASS
    reasons: list[str] = []
    for command in policy.final_verification:
        outcome = command_runner(
            run, f"stage:{FINAL_VERIFICATION}", command.cwd, command.argv, timeout=timeout)
        exit_code = outcome.get("exit_code") if isinstance(outcome, Mapping) else None
        records.append(
            {"cwd": command.cwd, "argv": list(command.argv), "exit_code": exit_code})
        printable = " ".join(command.argv)
        if exit_code in _UNRUNNABLE:
            verdict = BLOCKED
            reasons.append(f"{printable} could not be launched (exit {exit_code})")
        elif exit_code != 0:
            if verdict != BLOCKED:
                verdict = FAIL
            reasons.append(f"{printable} exited with {exit_code}")

    return verdict, tuple(reasons), {"commands": records}


# --- stage 15: final-diff approval -------------------------------------------------


def final_diff_result(*, approved: bool, diff_probe: DiffProbe | None) -> StageComputation:
    """Record the complete-diff digest and gate on an explicit human approval."""
    evidence: dict[str, Any] = {}
    if diff_probe is not None:
        text = diff_probe() or ""
        encoded = text.encode("utf-8", "replace")
        evidence["diff_digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        evidence["diff_bytes"] = len(encoded)
    if approved:
        return PASS, (), evidence
    return (
        BLOCKED,
        ("final-diff approval is pending — a human must approve the complete diff before "
         "any commit",),
        evidence,
    )


# --- stage 16: release plan (dry run by default) ---------------------------------


def release_plan_result(
    policy: ReleasePolicy,
    *,
    final_verification_pass: bool,
    final_diff_approved: bool,
    commit_control: bool,
    submodule_pointer_probe: PointerProbe | None = None,
) -> StageComputation:
    """Build the non-mutating release plan. Always ``PASS``: it plans, it never mutates.

    A real commit would require ``commit_control`` **and** ``final_diff_approved`` **and**
    ``final_verification_pass`` — and even then no commit or push is executed here.
    """
    blockers: list[str] = []
    if not commit_control:
        blockers.append("no explicit commit control was given")
    if not final_diff_approved:
        blockers.append("the final diff is not approved")
    if not final_verification_pass:
        blockers.append("final verification did not pass")
    would_commit = not blockers

    pointer_target = None
    if submodule_pointer_probe is not None and policy.submodule_pointer is not None:
        pointer_target = str(submodule_pointer_probe())

    plan = {
        "mode": "commit-authorized" if would_commit else "dry-run",
        "would_commit": would_commit,
        "reasons_release_is_dry_run": blockers,
        "stage_paths": list(policy.stage_paths),
        "submodule_pointer": policy.submodule_pointer,
        "submodule_pointer_target": pointer_target,
        "proposed_commit_message": policy.commit_message_template,
        "commit": ("the runner has no commit code path — nothing is executed even when "
                   "authorized"),
        "push": "never — the runner has no push code path",
    }
    return PASS, (), plan


__all__ = [
    "FINAL_DIFF_APPROVAL",
    "FINAL_VERIFICATION",
    "RELEASE",
    "RELEASE_STAGES",
    "ReleaseError",
    "ReleasePolicy",
    "final_diff_result",
    "final_verification_result",
    "load_release_policy",
    "release_plan_result",
]
