"""Portable core runner CLI.

This is the project-independent entry point the compatibility launcher and every
acceptance gate wire to. It accepts explicit filesystem anchors and logical, anchor-relative
paths only; it never infers a project layout, and it carries no project identity.

What it does:

* resolves the profile through :mod:`pipeline_core.profiles` explicit-anchor resolution,
  rejecting absolute paths, ``~``, ``..`` and ``\\`` separators in the logical arguments;
* reads the plan as JSON, or - for a ``.md`` path - as a Markdown task table via
  :mod:`pipeline_core.plan_md`, so the core can be pointed at a real project's native plan;
* builds a run from profile data — task selection with dependency and lease gates, per-task
  routing through the profile's closed registry, generic non-executing stage construction, and
  the fixed delivery-gate order (``plan``, ``final-diff``, ``commit``, verification verdict);
* with ``--dry-run`` prints a deterministic, redacted plan (sections ``C1``–``C8`` of the
  parallel-acceptance comparison matrix) and writes nothing outside a temporary run directory;
  a non-blocked ``--dry-run`` exits ``10`` (a delivery gate is pending), matching the legacy
  runner and the MI-01 exit-code table;
* refuses ``--push`` before any run artifact exists, with the recorded baseline message and
  exit code, and never lets ``--commit`` / ``--commit-approved-manifest`` bypass the plan gate;
* accepts the legacy ``run`` operational surface the compatibility launcher forwards -
  implementing ``--approve-final-diff`` / ``--commit-approved-manifest`` / ``--status`` /
  ``--unattended`` and accepting the rest as documented no-ops (see ``scripts/README.md``);
* fails closed on ``design`` and unknown task types.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from schemas import SchemaError, validate_relative_path
from schemas.contracts import TASK_TYPES, TaskSpec

from .adapters import ClaudeAdapter
from .execution import (
    ExecuteControls,
    ExecuteRequest,
    ExecutionError,
    execute_run,
    _resolve_attestation,
    _validate_attestation_scope,
)
from .integrations import load_tool_integration
from .plan_md import MarkdownPlanError, load_markdown_plan
from .post_task import POST_TASK_STAGES
from .profiles import Anchors, resolve_route
from .project_profile import load_runnable_profile
from .prompt_envelope import EnvelopeAnchors
from .redaction import build_rules, redact_text
from .release import ReleasePolicy, load_release_policy
from .stages import plan_release_dry_run
from .state import Run, StateError, read_lease, pid_alive
from .task_files import load_task_spec
from .verification import VerifierAnchors, VerifierLaunchers

# Process exit codes preserved from the baseline compatibility promise.
EXIT_OK = 0
EXIT_GATE_PENDING = 10
EXIT_BLOCKED = 20
EXIT_ERROR = 30
# The `--push` refusal keeps the exact legacy string and its dedicated exit code.
EXIT_PUSH_DENIED = 1
PUSH_DENIED_MESSAGE = "push is denied at this stage and the runner has no push code path."

# Task types the core recognizes but deliberately leaves unrouted: a design task needs an
# explicit executor and never gets a default route.
UNROUTED_TASK_TYPES = frozenset({"design"})

DELIVERY_GATES = ("plan", "final-diff", "commit", "verification-verdict")

#: The ``--mode`` value that extends the plan-only dry run with the stage 10-16 post-task
#: lifecycle (documentation, its audit, the code-graph refresh + verification, final
#: verification, the human final-diff gate, and the release dry run). It always writes
#: nothing and always stops at the release dry-run boundary.
POST_TASK_MODE = "release-dry-run"

# One-line meaning of each exit code, for the C5 line of the dry-run plan.
EXIT_MEANINGS = {
    EXIT_OK: "ok",
    EXIT_GATE_PENDING: "a delivery gate is pending",
    EXIT_BLOCKED: "blocked",
    EXIT_ERROR: "error",
}

_FEATURE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: A safe ``--attest-dependency`` ``SOURCE_FEATURE`` token: a bare directory name under the
#: profile's run-storage root — no path separator, ``.``, ``..``, or absolute/drive path.
#: ``execution._resolve_source_run_dir`` re-checks containment independently of this check.
_SOURCE_FEATURE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CliError(Exception):
    """A fail-closed CLI failure carrying a stable exit code and a leak-free message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Portable feature-pipeline core runner. Every path is an explicit anchor "
                    "or a logical path resolved below one; nothing is inferred.",
    )
    anchors = parser.add_argument_group("explicit anchors (filesystem roots)")
    anchors.add_argument("--project-root", metavar="DIR",
                         help="filesystem root the project's logical paths resolve below")
    anchors.add_argument("--agents-root", metavar="DIR",
                         help="filesystem root the shared agents/skills tree resolves below")
    anchors.add_argument("--core-root", metavar="DIR",
                         help="filesystem root this portable core resolves below")

    logical = parser.add_argument_group("logical paths (relative to an anchor)")
    logical.add_argument("--profile", metavar="REL",
                         help="profile file, relative to --project-root")
    logical.add_argument("--project-skill", metavar="REL",
                         help="project skill directory, relative to --agents-root")

    run = parser.add_argument_group("run selection and scope")
    run.add_argument("--plan", metavar="REL",
                     help="plan file, relative to the resolved project directory: JSON, or "
                          "Markdown (a '| ID | Type | Depends on |' task table) for a '.md' path")
    run.add_argument("--task", metavar="ID", help="run only this task, if its dependencies pass")
    run.add_argument("--through", metavar="ID",
                     help="select tasks from the first up to and including this one")
    run.add_argument("--attest-dependency", metavar="DEP_ID=SOURCE_FEATURE", action="append",
                     dest="attest_dependency",
                     help="--task only: treat DEP_ID (one of the task's own dependencies) as "
                          "satisfied because the already-closed run SOURCE_FEATURE verified it, "
                          "without dispatching it in this run. Repeatable; real control for "
                          "--mode execute and for --dry-run (both resolve and validate the "
                          "source run read-only), accepted as a no-op otherwise")
    run.add_argument("--resume", action="store_true",
                     help="resume a previously recorded run instead of starting one")
    run.add_argument("--mode", default="plan-only",
                     choices=["plan-only", "unattended", "execute", POST_TASK_MODE],
                     help="run mode (default: plan-only). 'execute' runs stages 5-9 — executor "
                          "dispatch, independent verification, and bounded repair — and stops "
                          "before documentation/delivery. 'release-dry-run' extends the "
                          "plan-only dry run with the post-task lifecycle (stages 10–16) and "
                          "its gates in the C4/C6 plan, then stops at the final-diff / release "
                          "dry-run boundary; it still writes nothing")
    run.add_argument("--feature", metavar="NAME", help="override the plan's feature name")
    run.add_argument("--prompt", metavar="REL",
                     help="prompt file, relative to the resolved project directory "
                          "(defaults to the plan file)")

    gates = parser.add_argument_group("delivery gates (all closed by default)")
    gates.add_argument("--approve-plan", action="store_true", help="satisfy the plan gate")
    gates.add_argument("--approve-final-diff", action="store_true",
                       help="satisfy the final-diff gate; never bypasses an earlier gate")
    gates.add_argument("--commit", action="store_true",
                       help="request the commit gate; it never bypasses the plan gate and this "
                            "runner has no commit code path")
    gates.add_argument("--commit-approved-manifest", action="store_true",
                       help="request the commit gate for an approved manifest; never bypasses "
                            "an earlier gate and this runner has no commit code path")
    gates.add_argument("--dry-run", action="store_true",
                       help="print the deterministic, redacted plan and write nothing outside a "
                            "temporary run directory")
    gates.add_argument("--push", action="store_true",
                       help="denied before any run artifact is created")

    # MI-01 core-classified operational flags the compatibility launcher forwards verbatim.
    # Each is either given a faithful minimal behaviour here or accepted as a documented
    # no-op ("not wired in the portable core; the project launcher owns this"). The bar:
    # a forwarded legacy invocation never dies with an argparse error inside the core.
    # scripts/README.md carries the per-flag decision table.
    compat = parser.add_argument_group(
        "compatibility surface (forwarded from the legacy CLI; see scripts/README.md)")
    compat.add_argument("--status", action="store_true",
                        help="print the resolved run state and exit")
    compat.add_argument("--unattended", action="store_true",
                        help="alias of --mode unattended")
    compat.add_argument("--adapter", metavar="NAME", choices=["claude", "codex", "auto"],
                        help="execution adapter for --mode execute (real control there); "
                             "accepted as a no-op in plan-only/unattended")
    compat.add_argument("--max-repair-attempts", metavar="N", type=int,
                        help="repair-loop bound for --mode execute (overrides each task's "
                             "declared bound); accepted as a no-op in plan-only/unattended")
    compat.add_argument("--routine-output-byte-budget", metavar="N", type=int,
                        help="recorded control for --mode execute; accepted as a no-op in "
                             "plan-only/unattended")
    compat.add_argument("--diagnostic-output-byte-budget", metavar="N", type=int,
                        help="recorded control for --mode execute; accepted as a no-op in "
                             "plan-only/unattended")
    compat.add_argument("--verbose", action="store_true",
                        help="accepted; reporter verbosity is owned by the project launcher")
    compat.add_argument("--quiet", action="store_true",
                        help="accepted; reporter verbosity is owned by the project launcher")
    return parser


def _require(value: str | None, field: str) -> str:
    if not value:
        raise CliError(EXIT_ERROR, f"{field} is required")
    return value


def _logical_relative(value: str, field: str) -> str:
    """Normalize an anchor-relative logical path, failing closed on an unsafe shape."""
    if "\\" in value:
        raise CliError(EXIT_ERROR, f"{field} must not contain '\\' path separators")
    try:
        return validate_relative_path(value, field)
    except SchemaError as exc:
        raise CliError(EXIT_ERROR, str(exc)) from None


def _resolve_under(anchor: Path, relative: str, field: str) -> Path:
    root = anchor.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise CliError(EXIT_ERROR, f"{field} escapes its explicit anchor") from None
    return candidate


def _load_plan(path: Path) -> tuple[str | None, list[dict]]:
    # A Markdown plan (a common task-board plan format) is read by the compatibility
    # reader; everything else is parsed as JSON. Downstream construction (selection,
    # routing, gates, exit codes) is identical for both.
    if path.suffix.lower() == ".md":
        try:
            return load_markdown_plan(path)
        except MarkdownPlanError as exc:
            raise CliError(EXIT_ERROR, str(exc)) from None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "plan file not found") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "plan file is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise CliError(EXIT_ERROR, "plan must be an object with a 'tasks' list")
    tasks: list[dict] = []
    seen: set[str] = set()
    for entry in data["tasks"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("type"):
            raise CliError(EXIT_ERROR, "every plan task needs an 'id' and a 'type'")
        tid = str(entry["id"])
        if tid in seen:
            raise CliError(EXIT_ERROR, f"duplicate task id in plan: {tid}")
        seen.add(tid)
        depends_on = entry.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise CliError(EXIT_ERROR, f"{tid}: depends_on must be a list")
        tasks.append({"id": tid, "type": str(entry["type"]),
                      "depends_on": [str(d) for d in depends_on]})
    if not tasks:
        raise CliError(EXIT_ERROR, "plan has no tasks")
    return (str(data["feature"]) if data.get("feature") else None), tasks


def _select(tasks: list[dict], task: str | None, through: str | None) -> list[dict]:
    ids = [t["id"] for t in tasks]
    if task is not None:
        if task not in ids:
            raise CliError(EXIT_ERROR, f"--task names an unknown task: {task}")
        return [t for t in tasks if t["id"] == task]
    if through is not None:
        if through not in ids:
            raise CliError(EXIT_ERROR, f"--through names an unknown task: {through}")
        cut = ids.index(through) + 1
        return tasks[:cut]
    return list(tasks)


def _route(profile, task_type: str, anchors: Anchors):
    """Return ``(resolved_route, None)`` or ``(None, reason)`` — never a guessed default."""
    if task_type not in TASK_TYPES:
        return None, "unknown-task-type"
    if task_type in UNROUTED_TASK_TYPES:
        return None, "unresolved-executor"
    try:
        return resolve_route(profile, task_type, anchors), None
    except SchemaError:
        return None, "unresolved-executor"


def _token(path: Path, project_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return "<outside-project>"
    return "<project_root>" if rel == "." else f"<project_root>/{rel}"


def _post_task_gate_lines(
    *,
    release_policy: ReleasePolicy | None,
    wrapper_dir: str | None,
    output_count: int,
    approve_plan: bool,
    approve_final_diff: bool,
    commit_requested: bool,
) -> list[str]:
    """Render the stage 10-16 post-task lifecycle for the C4 plan.

    Every work stage is ``planned`` (a plan can never run it); the human final-diff gate is
    ``satisfied`` only when the plan gate is also approved; and the release stage is *always*
    a dry run here — a plan can never assert a passing final verification, so ``would_commit``
    can never be true. This mirrors :func:`pipeline_core.release.release_plan_result` without
    any commit or push code path.
    """
    fd_ok = approve_plan and approve_final_diff
    out: list[str] = ["  post-task lifecycle (stages 10-16, in order):"]
    for number, name in POST_TASK_STAGES:
        if number == 12:
            suffix = (f" (wrapper: bash {wrapper_dir}/build.sh)" if wrapper_dir else "")
            out.append(f"  {number}. {name} - planned{suffix}")
        elif number == 13:
            out.append(
                f"  {number}. {name} - planned ({output_count} configured output(s) re-checked)")
        elif number == 14:
            checks = "; ".join(
                " ".join(command.argv) for command in
                (release_policy.final_verification if release_policy else ()))
            out.append(f"  {number}. {name} - planned (checks: {checks})")
        elif number == 15:
            out.append(f"  {number}. {name} - {'satisfied' if fd_ok else 'pending'}")
        elif number == 16:
            blockers: list[str] = []
            if not commit_requested:
                blockers.append("no explicit commit control was given")
            if not fd_ok:
                blockers.append("the final diff is not approved")
            blockers.append("final verification has not run in a plan")
            out.append(f"  {number}. {name} - dry-run")
            if release_policy is not None:
                out.append(f"     stage paths: {', '.join(release_policy.stage_paths)}")
                if release_policy.submodule_pointer:
                    out.append(f"     submodule pointer: {release_policy.submodule_pointer}")
                out.append(
                    f"     proposed commit message: {release_policy.commit_message_template}")
            out.append(f"     reasons release stays a dry run: {'; '.join(blockers)}")
            out.append(
                "     commit: the runner has no commit code path - nothing is executed even "
                "when authorized")
            out.append("     push: never - the runner has no push code path")
        else:
            out.append(f"  {number}. {name} - planned")
    return out


def _plan_text(
    *,
    profile,
    profile_name: str,
    mode: str,
    anchors: Anchors,
    project_dir: Path,
    profile_rel: str,
    project_skill_rel: str | None,
    selected: list[dict],
    all_ids: list[str],
    routes: dict[str, tuple],
    dep_blocked: dict[str, list[str]],
    lease_blocked: bool,
    approve_plan: bool,
    approve_final_diff: bool,
    commit_approved_manifest: bool,
    commit_requested: bool,
    exit_code: int,
    post_task: bool = False,
    release_policy: ReleasePolicy | None = None,
    wrapper_dir: str | None = None,
    output_count: int = 0,
) -> str:
    lines: list[str] = []
    lines.append("feature-pipeline core runner - dry run")
    lines.append(f"mode: {mode}")
    lines.append(f"profile: {profile_name}")
    lines.append("anchors:")
    lines.append("  project-root: <project_root>")
    lines.append("  agents-root: <agents_root>")
    lines.append("  core-root: <core_root>")
    lines.append(f"  profile: <project_root>/{profile_rel}")
    lines.append(f"  project-skill: <agents_root>/{project_skill_rel}"
                 if project_skill_rel else "  project-skill: (none)")
    lines.append("")

    lines.append("C1. Selected tasks:")
    for index, task in enumerate(selected, start=1):
        lines.append(f"  {index}. {task['id']}")
    lines.append("")

    lines.append("C2. Routing:")
    for task in selected:
        resolved, reason = routes[task["id"]]
        if reason is not None:
            lines.append(f"  {task['id']}: {reason} (type {task['type']})")
            continue
        checks = ",".join(resolved.route.checks)
        lines.append(
            f"  {task['id']}: stack={resolved.route.stack} "
            f"root={_token(resolved.root, project_dir)} "
            f"storage={_token(resolved.storage, project_dir)} "
            f"checks=[{checks}] "
            f"subagents=[{','.join(resolved.route.subagents)}]"
        )
    lines.append("")

    lines.append("C3. Generated argv (shell-free):")
    for task in selected:
        lines.append(f"  {task['id']}:")
        resolved, reason = routes[task["id"]]
        root = resolved.root if reason is None else project_dir
        for planned in plan_release_dry_run(profile.stages, root):
            lines.append(f"    stage {planned.name}: {' '.join(planned.argv)}")
        if reason is None:
            for name, argv in zip(resolved.route.checks, resolved.checks):
                lines.append(f"    check {name}: {' '.join(argv)}")
    lines.append("")

    lines.append("C4. Pending delivery gates (in order):")
    # A gate is satisfied only when it is approved AND every earlier gate already is:
    # an approval flag never bypasses an earlier, still-pending gate.
    approved = {
        "plan": approve_plan,
        "final-diff": approve_final_diff,
        "commit": commit_approved_manifest,
        "verification-verdict": False,
    }
    earlier_satisfied = True
    for position, gate in enumerate(DELIVERY_GATES, start=1):
        satisfied = earlier_satisfied and approved.get(gate, False)
        earlier_satisfied = satisfied
        state = "satisfied" if satisfied else "pending"
        note = " (requested)" if gate == "commit" and commit_requested else ""
        lines.append(f"  {position}. {gate} - {state}{note}")
    if post_task:
        lines.extend(_post_task_gate_lines(
            release_policy=release_policy, wrapper_dir=wrapper_dir, output_count=output_count,
            approve_plan=approve_plan, approve_final_diff=approve_final_diff,
            commit_requested=commit_requested))
    lines.append("")

    lines.append(f"C5. Exit code: {exit_code} ({EXIT_MEANINGS.get(exit_code, 'see safety posture')})")
    lines.append("")

    lines.append("C6. Planned state transitions:")
    for task in selected:
        resolved, reason = routes[task["id"]]
        if task["id"] in dep_blocked:
            deps = ",".join(dep_blocked[task["id"]])
            lines.append(f"  {task['id']}: pending -> blocked (dependency-not-satisfied: {deps})")
        elif lease_blocked:
            lines.append(f"  {task['id']}: pending -> blocked (lease-held)")
        elif reason is not None:
            lines.append(f"  {task['id']}: pending -> blocked ({reason})")
        else:
            lines.append(f"  {task['id']}: pending -> ready -> running -> implemented -> verified")
    if post_task:
        chain = " -> ".join(name for _, name in POST_TASK_STAGES)
        lines.append(f"  post-task: verified -> {chain} (release stays a dry run)")
    lines.append("")

    lines.append("C7. Redacted evidence manifest:")
    storage = None
    for task in selected:
        resolved, reason = routes[task["id"]]
        if reason is None:
            storage = _token(resolved.storage, project_dir)
            break
    storage = storage or "<project_root>/.pipeline/runs"
    lines.append(f"  {storage}/<feature>/run.json")
    lines.append("")

    lines.append("C8. Safety posture:")
    lines.append(f'  push: denied before any artifact ("{PUSH_DENIED_MESSAGE}") [exit {EXIT_PUSH_DENIED}]')
    lines.append("  commit: never bypasses the plan gate; this runner has no commit code path")
    lines.append("  logical paths: absolute, ~, .. and \\ separators are rejected")
    lines.append("  unknown task type: fail closed (unknown-task-type)")
    lines.append("  design / no registry route: fail closed (unresolved-executor)")
    lines.append("  verifier roles: read-only, enforced at profile load")
    lines.append("  unmet dependency: fail closed (dependency-not-satisfied)")
    if post_task:
        lines.append(
            "  release stage: dry-run plan only; a commit needs the commit control, an "
            "approved final diff, and a passing final verification, and even then no commit "
            "or push subprocess exists")

    text = "\n".join(lines) + "\n"
    # Defense in depth: normalize any machine-local path spelling that slipped through.
    return redact_text(text, build_rules())


def _build(args: argparse.Namespace) -> tuple[str, int]:
    project_root = Path(_require(args.project_root, "--project-root"))
    agents_root = Path(_require(args.agents_root, "--agents-root"))
    core_root = Path(_require(args.core_root, "--core-root"))
    anchors = Anchors(project_root, agents_root, core_root)

    # ``release-dry-run`` is a plan-only preview by construction: it never runs an executor
    # and never writes, so it always implies ``--dry-run``.
    post_task = args.mode == POST_TASK_MODE
    if post_task:
        args.dry_run = True

    profile_rel = _logical_relative(_require(args.profile, "--profile"), "--profile")
    profile_path = _resolve_under(project_root, profile_rel, "--profile")
    project_skill_rel = None
    if args.project_skill:
        project_skill_rel = _logical_relative(args.project_skill, "--project-skill")
        _resolve_under(agents_root, project_skill_rel, "--project-skill")

    try:
        profile = load_runnable_profile(profile_path)
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "profile file not found") from None
    except SchemaError as exc:
        raise CliError(EXIT_ERROR, f"profile is invalid: {exc}") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "profile file is not valid JSON") from None

    # ``release-dry-run`` consumes the project's post-task contract as-is: the tool-integration
    # block and the release policy declared beside the profile. Loading is fail-closed.
    release_policy: ReleasePolicy | None = None
    wrapper_dir: str | None = None
    output_count = 0
    if post_task:
        config_dir = profile_path.parent
        try:
            integration = load_tool_integration(config_dir / "integrations.json")
            release_policy = load_release_policy(config_dir / "release.json")
        except FileNotFoundError:
            raise CliError(
                EXIT_ERROR,
                "release-dry-run needs an integrations.json and a release.json beside the "
                "profile") from None
        except SchemaError as exc:
            raise CliError(EXIT_ERROR, f"post-task config is invalid: {exc}") from None
        except json.JSONDecodeError:
            raise CliError(EXIT_ERROR, "post-task config is not valid JSON") from None
        wrapper_dir = integration.wrapper_dir
        output_count = len(integration.expected_outputs)

    project_dir = _resolve_under(project_root, profile.logical_paths.project, "project path")
    plan_rel = _logical_relative(_require(args.plan, "--plan"), "--plan")
    plan_path = _resolve_under(project_dir, plan_rel, "--plan")
    prompt_rel = _logical_relative(args.prompt, "--prompt") if args.prompt else plan_rel
    _resolve_under(project_dir, prompt_rel, "--prompt")

    if args.mode == "execute" and not args.dry_run:
        return _execute(args, anchors, agents_root, project_dir, profile, plan_path, prompt_rel)

    plan_feature, plan_tasks = _load_plan(plan_path)
    feature = args.feature or plan_feature
    if not feature or not _FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    selected = _select(plan_tasks, args.task, args.through)
    all_ids = [t["id"] for t in plan_tasks]
    verified: set[str] = set()  # a fresh run has verified nothing

    routes: dict[str, tuple] = {t["id"]: _route(profile, t["type"], anchors) for t in selected}

    storage_dir = None
    for task in selected:
        resolved, reason = routes[task["id"]]
        if reason is None:
            storage_dir = resolved.storage
            break
    lease_dir = (storage_dir or project_dir / ".pipeline" / "runs") / feature

    # `--dry-run` mirrors `_execute`'s attestation handling read-only: same syntax/scope
    # checks, and the same source-run resolution, so a preview never claims success for an
    # attestation the real run would refuse (or silently ignores a bad one).
    attested_ids: set[str] = set()
    if args.dry_run:
        attestations = _parse_attestations(args.attest_dependency)
        if attestations:
            by_id = {t["id"]: SimpleNamespace(depends_on=t["depends_on"]) for t in plan_tasks}
            attestation_controls = ExecuteControls(
                task=args.task, attested_dependencies=attestations)
            try:
                _validate_attestation_scope(attestation_controls, by_id)
                for dep_id, source_feature in attestations:
                    _resolve_attestation(
                        dep_id=dep_id, source_feature=source_feature, run_dir=lease_dir,
                        repo_root=project_dir, prompt_path=project_dir / prompt_rel,
                        plan_path=plan_path)
                    attested_ids.add(dep_id)
            except ExecutionError as exc:
                raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None

    dep_blocked: dict[str, list[str]] = {}
    if args.task is not None:
        unmet = [d for d in selected[0]["depends_on"]
                 if d not in verified and d not in attested_ids]
        if unmet:
            dep_blocked[args.task] = unmet

    if args.status:
        # A read-only state inspector: resolve, report the recorded run, and exit.
        return _status_text(lease_dir, project_dir, feature), EXIT_OK

    lease_path = lease_dir / "pipeline.lock"
    held = read_lease(lease_path)
    lease_blocked = bool(held) and (held.get("unreadable") or pid_alive(held.get("pid")))

    unresolved = any(reason is not None for _, reason in routes.values())

    if dep_blocked:
        exit_code = EXIT_BLOCKED
    elif lease_blocked:
        exit_code = EXIT_BLOCKED
    elif args.resume:
        exit_code = _resume_exit(lease_dir, project_dir, feature)
    elif unresolved:
        exit_code = EXIT_ERROR
    else:
        # A non-blocked run - dry or real - stops with a delivery gate still pending.
        # The legacy runner and the MI-01 exit-code table ("10 ... preserve") return
        # EXIT_GATE_PENDING for every non-blocked --dry-run; the core preserves that.
        exit_code = EXIT_GATE_PENDING

    mode = "unattended" if args.unattended else args.mode

    if args.dry_run:
        text = _plan_text(
            profile=profile, profile_name=profile.name, mode=mode, anchors=anchors,
            project_dir=project_dir, profile_rel=profile_rel,
            project_skill_rel=project_skill_rel, selected=selected, all_ids=all_ids,
            routes=routes, dep_blocked=dep_blocked, lease_blocked=lease_blocked,
            approve_plan=args.approve_plan, approve_final_diff=args.approve_final_diff,
            commit_approved_manifest=args.commit_approved_manifest,
            commit_requested=args.commit or args.commit_approved_manifest, exit_code=exit_code,
            post_task=post_task, release_policy=release_policy, wrapper_dir=wrapper_dir,
            output_count=output_count,
        )
        # A dry run persists nothing: the temporary directory is created and discarded.
        tempfile.mkdtemp(prefix="pipeline-dry-run-")
        return text, exit_code

    return _non_dry_run_text(args, exit_code, dep_blocked, lease_blocked, unresolved), exit_code


def _status_text(run_dir: Path, project_dir: Path, feature: str) -> str:
    """Render the recorded run state for ``--status``; redacted, host-path-free."""
    try:
        run = Run.load(run_dir, project_dir)
    except StateError:
        return redact_text(f"feature: {feature}\nno recorded run\n", build_rules())
    lines = [f"feature: {run.feature}", f"status: {run.status}", "tasks:"]
    for record in run.tasks.values():
        lines.append(f"  {record.id}: {record.status}")
    return redact_text("\n".join(lines) + "\n", build_rules())


def _resume_exit(lease_dir: Path, project_dir: Path, feature: str) -> int:
    try:
        Run.load(lease_dir, project_dir)
    except StateError:
        return EXIT_BLOCKED
    return EXIT_GATE_PENDING


# --- execute mode (stages 5-9) -----------------------------------------------------------


def _project_relative(path: Path, project_dir: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return None


def _load_execute_specs(
    plan_path: Path, feature_override: str | None
) -> tuple[str | None, list[TaskSpec]]:
    """Resolve the plan to complete :class:`TaskSpec` values for execute mode.

    A ``.md`` plan reads its task table and normalizes each ``tasks/<ID>_*.md`` file through
    :func:`pipeline_core.task_files.load_task_spec`. A ``.json`` plan must carry the full
    execution metadata per task (``task_type``/``executor``/``allowed_scope``/…) — the
    id+type-only shape the dry run accepts is not enough to launch anything.
    """
    if plan_path.suffix.lower() == ".md":
        try:
            feature, entries = load_markdown_plan(plan_path)
        except MarkdownPlanError as exc:
            raise CliError(EXIT_ERROR, str(exc)) from None
        tasks_dir = plan_path.parent / "tasks"
        specs: list[TaskSpec] = []
        for entry in entries:
            tid = entry["id"]
            matches = sorted(tasks_dir.glob(f"{tid}_*.md"))
            if not matches:
                raise CliError(
                    EXIT_ERROR,
                    f"execute mode: no task file 'tasks/{tid}_*.md' beside the plan")
            try:
                specs.append(load_task_spec(matches[0]))
            except SchemaError as exc:
                raise CliError(EXIT_ERROR, f"{tid}: {exc}") from None
        return (feature_override or feature), specs

    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "plan file not found") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "plan file is not valid JSON") from None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise CliError(EXIT_ERROR, "plan must be an object with a non-empty 'tasks' list")
    specs = []
    seen: set[str] = set()
    for entry in tasks:
        if not isinstance(entry, dict):
            raise CliError(EXIT_ERROR, "every plan task must be an object")
        kwargs = dict(entry)
        if "task_type" not in kwargs and "type" in kwargs:
            kwargs["task_type"] = kwargs.pop("type")
        if "allowed_scope" not in kwargs:
            raise CliError(
                EXIT_ERROR,
                "execute mode needs full task metadata: give each JSON task 'task_type', "
                "'executor', 'allowed_scope', and 'acceptance_criteria', or point --plan at a "
                "Markdown plan with task files")
        try:
            spec = TaskSpec.build(**kwargs)
        except (SchemaError, TypeError) as exc:
            raise CliError(EXIT_ERROR, f"{entry.get('id', '?')}: {exc}") from None
        if spec.id in seen:
            raise CliError(EXIT_ERROR, f"duplicate task id in plan: {spec.id}")
        seen.add(spec.id)
        specs.append(spec)
    feature = str(data["feature"]) if data.get("feature") else None
    return (feature_override or feature), specs


def _parse_attestations(raw: list[str] | None) -> tuple[tuple[str, str], ...]:
    """Parse repeated ``--attest-dependency DEP_ID=SOURCE_FEATURE`` flags, failing closed on
    unsafe syntax before any source-run path is built. ``execution._validate_attestation_scope``
    and ``_resolve_attestation`` own every other denial reason (scope, duplicate,
    not-a-dependency, source resolution)."""
    if not raw:
        return ()
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if "=" not in item:
            raise CliError(
                EXIT_ERROR, f"--attest-dependency must be DEP_ID=SOURCE_FEATURE: '{item}'")
        dep_id, source_feature = item.split("=", 1)
        if not dep_id:
            raise CliError(EXIT_ERROR, f"--attest-dependency has an empty DEP_ID: '{item}'")
        if not _SOURCE_FEATURE_RE.match(source_feature):
            raise CliError(
                EXIT_ERROR,
                "--attest-dependency source feature must be a bare directory name (letters, "
                f"digits, '-', '_' only; no path separator, '.', '..', or absolute/drive path): "
                f"'{source_feature}'")
        parsed.append((dep_id, source_feature))
    return tuple(parsed)


def _resolve_add_dirs(project_dir: Path, *anchors: Path) -> tuple[Path, ...]:
    """Resolve each anchor and keep only those not already reachable from a granted root.

    The adapter's ``working_root`` (the resolved ``project_dir``) is always reachable, so it
    seeds the covered set without being emitted. Each further anchor is resolved once
    (symlink/junction-following) and emitted only when it does not sit under ``project_dir``
    or under an anchor already granted in this call — a core checkout nested under the agents
    root needs no separate ``--add-dir``; one in an unrelated filesystem tree does. Input
    order is preserved and no resolved path is emitted twice.
    """
    covered: list[Path] = [Path(project_dir).resolve()]
    granted: list[Path] = []
    for anchor in anchors:
        resolved = Path(anchor).resolve()
        if any(resolved.is_relative_to(root) for root in covered):
            continue
        granted.append(resolved)
        covered.append(resolved)
    return tuple(granted)


def make_execute_adapters(project_dir: Path, agents_root: Path, core_root: Path):
    """Build the production executor adapter and the (identical) read-only verifier pair.

    A module-level seam: a test replaces this with deterministic fake adapters.

    ``add_dirs`` carries the fully resolved, symlink/junction-following real paths of the
    anchors the run was given — ``agents_root`` (RDS-10) and ``core_root`` (RDS-17) — that
    are not already reachable from the adapter's ``working_root`` (the resolved
    ``project_dir``). The Claude CLI's own directory sandbox resolves the *requested* path
    and compares that resolved form against its allowed-directory list, so an unresolved
    logical path is silently denied even though it looks like a legitimate subpath: a
    symlinked ``.agents`` that reaches outside ``project_dir`` (a shared Syncthing-replicated
    skills library), or — one link deeper — a ``.agents/skills/software-development/
    feature-pipeline`` discovery link whose real target is a *separate* core checkout, not a
    subdirectory of the resolved ``agents_root``. Resolving here, in Python, before the CLI
    ever sees the argv, is the only way to grant the directory it will actually check. A
    resolved anchor that already sits under ``working_root`` (or under an anchor already
    granted) adds nothing — this emits exactly the roots genuinely outside every other
    grant, never a duplicate, whether the core resolves under ``agents_root``, beside it, or
    in an unrelated tree.
    """
    add_dirs = _resolve_add_dirs(project_dir, agents_root, core_root)
    executor = ClaudeAdapter(working_root=str(project_dir), add_dirs=add_dirs)
    launchers = VerifierLaunchers(task=executor, test=executor)
    environment = {"claude": executor.available(), "codex": False}
    return executor, launchers, environment


def _execute(
    args: argparse.Namespace,
    anchors: Anchors,
    agents_root: Path,
    project_dir: Path,
    profile,
    plan_path: Path,
    prompt_rel: str,
) -> tuple[str, int]:
    feature, specs = _load_execute_specs(plan_path, args.feature)
    if not feature or not _FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    storage_dir = None
    for spec in specs:
        resolved, reason = _route(profile, spec.task_type, anchors)
        if reason is None:
            storage_dir = resolved.storage
            break
    run_dir = (storage_dir or project_dir / ".pipeline" / "runs") / feature

    executor, launchers, environment = make_execute_adapters(
        project_dir, agents_root, anchors.core_root)
    controls = ExecuteControls(
        plan_approved=args.approve_plan,
        unattended=args.unattended,
        resume=args.resume,
        adapter=args.adapter,
        adapter_explicit=args.adapter is not None,
        max_repair_attempts=args.max_repair_attempts,
        routine_output_byte_budget=args.routine_output_byte_budget,
        diagnostic_output_byte_budget=args.diagnostic_output_byte_budget,
        task=args.task,
        through=args.through,
        attested_dependencies=_parse_attestations(args.attest_dependency),
    )
    request = ExecuteRequest(
        feature=feature,
        repo_root=project_dir,
        run_dir=run_dir,
        prompt_path=project_dir / prompt_rel,
        plan_path=plan_path,
        specs=tuple(specs),
        adapter=executor,
        launchers=launchers,
        envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
        verifier_anchors=VerifierAnchors(
            project_root=str(project_dir), agents_root=str(agents_root)),
        environment=environment,
        controls=controls,
        plan_prompt_path=_project_relative(plan_path, project_dir),
    )
    result = execute_run(request)
    if result.status == "error":
        raise CliError(result.exit_code, result.message)
    return redact_text(result.message.rstrip("\n") + "\n", build_rules()), result.exit_code


def _non_dry_run_text(args, exit_code, dep_blocked, lease_blocked, unresolved) -> str:
    if dep_blocked:
        return "blocked: a selected task has an unsatisfied dependency (dependency-not-satisfied)\n"
    if lease_blocked:
        return "blocked: a live run lease is held for this feature (lease-held)\n"
    if unresolved:
        return "error: a selected task has no registry route (unresolved-executor)\n"
    if args.resume and exit_code == EXIT_BLOCKED:
        return "blocked: no matching recorded run to resume\n"
    if args.approve_plan:
        return ("plan gate satisfied; commit gate pending - this runner has no commit or push "
                "code path, so the run stops here.\n")
    return ("plan gate pending - approve with --approve-plan. No delivery gate is auto-passed; "
            "--commit does not bypass this gate.\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.push:
        # Refused before anything is resolved, loaded, or written.
        print(PUSH_DENIED_MESSAGE, file=sys.stderr)
        return EXIT_PUSH_DENIED

    try:
        text, code = _build(args)
    except CliError as exc:
        print(redact_text(exc.message, build_rules()), file=sys.stderr)
        return exc.code
    print(text, end="")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
