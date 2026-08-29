"""Portable core runner CLI.

This is the project-independent entry point the compatibility launcher and every
acceptance gate wire to. It accepts explicit filesystem anchors and logical, anchor-relative
paths only; it never infers a project layout, and it carries no project identity.

What it does:

* resolves the profile through :mod:`pipeline_core.profiles` explicit-anchor resolution,
  rejecting absolute paths, ``~``, ``..`` and ``\\`` separators in the logical arguments;
* builds a run from profile data — task selection with dependency and lease gates, per-task
  routing through the profile's closed registry, generic non-executing stage construction, and
  the fixed delivery-gate order (``plan``, ``final-diff``, ``commit``, verification verdict);
* with ``--dry-run`` prints a deterministic, redacted plan (sections ``C1``–``C8`` of the
  parallel-acceptance comparison matrix) and writes nothing outside a temporary run directory;
* refuses ``--push`` before any run artifact exists, with the recorded baseline message and
  exit code, and never lets ``--commit`` bypass the plan gate;
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
from typing import Sequence

from schemas import SchemaError, load_profile, validate_relative_path
from schemas.contracts import TASK_TYPES

from .profiles import Anchors, resolve_route
from .redaction import build_rules, redact_text
from .stages import plan_release_dry_run
from .state import Run, StateError, read_lease, pid_alive

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

_FEATURE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
                     help="plan file (JSON), relative to the resolved project directory")
    run.add_argument("--task", metavar="ID", help="run only this task, if its dependencies pass")
    run.add_argument("--through", metavar="ID",
                     help="select tasks from the first up to and including this one")
    run.add_argument("--resume", action="store_true",
                     help="resume a previously recorded run instead of starting one")
    run.add_argument("--mode", default="plan-only", choices=["plan-only", "unattended"],
                     help="run mode (default: plan-only)")
    run.add_argument("--feature", metavar="NAME", help="override the plan's feature name")
    run.add_argument("--prompt", metavar="REL",
                     help="prompt file, relative to the resolved project directory "
                          "(defaults to the plan file)")

    gates = parser.add_argument_group("delivery gates (all closed by default)")
    gates.add_argument("--approve-plan", action="store_true", help="satisfy the plan gate")
    gates.add_argument("--commit", action="store_true",
                       help="request the commit gate; it never bypasses the plan gate and this "
                            "runner has no commit code path")
    gates.add_argument("--dry-run", action="store_true",
                       help="print the deterministic, redacted plan and write nothing outside a "
                            "temporary run directory")
    gates.add_argument("--push", action="store_true",
                       help="denied before any run artifact is created")
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
    commit_requested: bool,
    exit_code: int,
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
    for position, gate in enumerate(DELIVERY_GATES, start=1):
        satisfied = gate == "plan" and approve_plan
        state = "satisfied" if satisfied else "pending"
        note = " (requested)" if gate == "commit" and commit_requested else ""
        lines.append(f"  {position}. {gate} - {state}{note}")
    lines.append("")

    lines.append(f"C5. Exit code: {exit_code}")
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

    text = "\n".join(lines) + "\n"
    # Defense in depth: normalize any machine-local path spelling that slipped through.
    return redact_text(text, build_rules())


def _build(args: argparse.Namespace) -> tuple[str, int]:
    project_root = Path(_require(args.project_root, "--project-root"))
    agents_root = Path(_require(args.agents_root, "--agents-root"))
    core_root = Path(_require(args.core_root, "--core-root"))
    anchors = Anchors(project_root, agents_root, core_root)

    profile_rel = _logical_relative(_require(args.profile, "--profile"), "--profile")
    profile_path = _resolve_under(project_root, profile_rel, "--profile")
    project_skill_rel = None
    if args.project_skill:
        project_skill_rel = _logical_relative(args.project_skill, "--project-skill")
        _resolve_under(agents_root, project_skill_rel, "--project-skill")

    try:
        profile = load_profile(profile_path)
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "profile file not found") from None
    except SchemaError as exc:
        raise CliError(EXIT_ERROR, f"profile is invalid: {exc}") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "profile file is not valid JSON") from None

    project_dir = _resolve_under(project_root, profile.logical_paths.project, "project path")
    plan_rel = _logical_relative(_require(args.plan, "--plan"), "--plan")
    plan_path = _resolve_under(project_dir, plan_rel, "--plan")
    prompt_rel = _logical_relative(args.prompt, "--prompt") if args.prompt else plan_rel
    _resolve_under(project_dir, prompt_rel, "--prompt")

    plan_feature, plan_tasks = _load_plan(plan_path)
    feature = args.feature or plan_feature
    if not feature or not _FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    selected = _select(plan_tasks, args.task, args.through)
    all_ids = [t["id"] for t in plan_tasks]
    verified: set[str] = set()  # a fresh run has verified nothing

    dep_blocked: dict[str, list[str]] = {}
    if args.task is not None:
        unmet = [d for d in selected[0]["depends_on"] if d not in verified]
        if unmet:
            dep_blocked[args.task] = unmet

    routes: dict[str, tuple] = {t["id"]: _route(profile, t["type"], anchors) for t in selected}

    storage_dir = None
    for task in selected:
        resolved, reason = routes[task["id"]]
        if reason is None:
            storage_dir = resolved.storage
            break
    lease_dir = (storage_dir or project_dir / ".pipeline" / "runs") / feature
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
    elif args.dry_run:
        exit_code = EXIT_OK
    else:
        exit_code = EXIT_GATE_PENDING

    if args.dry_run:
        text = _plan_text(
            profile=profile, profile_name=profile.name, mode=args.mode, anchors=anchors,
            project_dir=project_dir, profile_rel=profile_rel,
            project_skill_rel=project_skill_rel, selected=selected, all_ids=all_ids,
            routes=routes, dep_blocked=dep_blocked, lease_blocked=lease_blocked,
            approve_plan=args.approve_plan, commit_requested=args.commit, exit_code=exit_code,
        )
        # A dry run persists nothing: the temporary directory is created and discarded.
        tempfile.mkdtemp(prefix="pipeline-dry-run-")
        return text, exit_code

    return _non_dry_run_text(args, exit_code, dep_blocked, lease_blocked, unresolved), exit_code


def _resume_exit(lease_dir: Path, project_dir: Path, feature: str) -> int:
    try:
        Run.load(lease_dir, project_dir)
    except StateError:
        return EXIT_BLOCKED
    return EXIT_GATE_PENDING


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
