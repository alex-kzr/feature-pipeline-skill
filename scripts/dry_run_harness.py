#!/usr/bin/env python3
"""Core-side dry-run acceptance harness for the S1-S7 parallel-run comparison.

For each scenario this module:

1. copies a portable fixture into a fresh temporary directory,
2. runs the core runner CLI (:mod:`pipeline_core.runner_cli`, the logic behind
   ``scripts/run_pipeline.py``) with the scenario's arguments,
3. captures the generated argv, stdout, stderr, and exit code,
4. redacts the capture with :mod:`pipeline_core.redaction` plus explicit anchor rules so
   every host path becomes a logical ``<project_root>`` / ``<agents_root>`` / ``<core_root>``
   token and no user name, machine name, credential, or token survives,
5. returns the capture as deterministic text — byte-identical across two runs from
   independent clean fixture copies.

The scenario argument table (:data:`SCENARIOS`) is the single source of truth so CX-01 and
the legacy baseline drive both runners with identical arguments. The harness writes only
under a temporary directory; it never writes under ``docs/acceptance/`` (that is CX-01's
scope) and never passes ``--commit``. ``--push`` appears only in the S4 denial scenario,
which asserts the refusal and that no artifact is created.

Usage::

    python scripts/dry_run_harness.py            # print every S1-S7 capture
    python scripts/dry_run_harness.py --scenario S3

Standard library only.
"""

from __future__ import annotations

import argparse
import atexit
import io
import json
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_core import runner_cli
from pipeline_core.redaction import build_rules, redact_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: One dedicated temp root for the whole process; per-run copies live below it and are
#: removed as soon as their capture is taken.
WORKDIR_ROOT = Path(tempfile.mkdtemp(prefix="core-dry-run-harness-"))
atexit.register(lambda: shutil.rmtree(WORKDIR_ROOT, ignore_errors=True))

#: Feature name every seeded plan carries. A single ``[A-Za-z0-9._-]`` token.
FEATURE = "acceptance-feature"

#: Closed project registry added to every seeded profile so routing resolves the same way
#: the legacy baseline expects. Kept here, beside the scenario table, so the comparison
#: inputs stay identical for both runners.
REGISTRY = {
    "task_types": {
        "docs": {
            "stack": "text",
            "subagents": ["executor"],
            "root": "content",
            "checks": ["lint"],
            "storage": "runs",
        }
    },
    "stacks": {"text": {"runtime": "text"}},
    "subagents": {"executor": {"grant": "executor"}},
    "roots": {"content": "content"},
    "checks": {"lint": ["lint", "run"]},
    "storage": {"runs": ".pipeline/runs"},
}

# Per-fixture layout: where the profile lives and which subdirectory the profile's
# ``logical_paths.project`` points at.
_FIXTURE_LAYOUT = {
    "library-guide": ("profile.json", "."),
    "incident-notes": ("config/pipeline.json", "workspace"),
}

_TWO_TASKS = (
    {"id": "T-01", "type": "docs"},
    {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
)


@dataclass(frozen=True)
class RunSpec:
    """One CLI invocation inside a scenario."""

    label: str
    #: ``None`` means "do not seed a fixture" — ``raw_argv`` is passed to the CLI verbatim.
    fixture: str | None = None
    raw_argv: tuple[str, ...] = ()
    tasks: tuple[dict, ...] = _TWO_TASKS
    plan_arg: str = "plan.json"
    prompt_arg: str | None = None
    #: Arguments appended after the anchors, ``--profile``, ``--plan`` and ``--dry-run``.
    extra_args: tuple[str, ...] = ()
    #: Pre-write a minimal ``run.json`` so ``--resume`` finds a partially recorded run.
    partial_state: bool = False


@dataclass(frozen=True)
class Scenario:
    """A named scenario: one or more CLI invocations captured back to back."""

    summary: str
    runs: tuple[RunSpec, ...]

    @property
    def cli_args(self) -> tuple[str, ...]:
        """Every scenario-declared argument, for safety assertions."""
        out: list[str] = []
        for run in self.runs:
            out.extend(run.raw_argv)
            out.extend(run.extra_args)
        return tuple(out)


SCENARIOS: dict[str, Scenario] = {
    "S1": Scenario(
        "plan-only dry run over a multi-task plan",
        (RunSpec("S1", fixture="library-guide", extra_args=("--mode", "plan-only")),),
    ),
    "S2": Scenario(
        "dependency not yet done -> fail closed",
        (RunSpec("S2", fixture="library-guide", extra_args=("--task", "T-02")),),
    ),
    "S3": Scenario(
        "--through selects the plan up to the last task",
        (RunSpec("S3", fixture="library-guide", extra_args=("--through", "T-02")),),
    ),
    "S4": Scenario(
        "--push is refused before any run artifact exists",
        (RunSpec("S4", fixture=None, raw_argv=("--push",)),),
    ),
    "S5": Scenario(
        "absolute logical path rejected, then unknown task type rejected",
        (
            RunSpec(
                "S5a-absolute-path",
                fixture="library-guide",
                tasks=({"id": "T-01", "type": "docs"},),
                prompt_arg="/etc/pipeline/prompt.md",
            ),
            RunSpec(
                "S5b-unknown-type",
                fixture="library-guide",
                tasks=({"id": "X-01", "type": "sculpture"},),
            ),
        ),
    ),
    "S6": Scenario(
        "--resume from a partially recorded run state",
        (
            RunSpec(
                "S6",
                fixture="library-guide",
                tasks=({"id": "T-01", "type": "docs"},),
                extra_args=("--resume",),
                partial_state=True,
            ),
        ),
    ),
    "S7": Scenario(
        "profile-routed dry run on the second portable fixture",
        (
            RunSpec(
                "S7",
                fixture="incident-notes",
                tasks=({"id": "N-01", "type": "docs"},),
                extra_args=("--mode", "plan-only"),
            ),
        ),
    ),
}


def _spellings(path: Path) -> list[str]:
    native = str(path)
    return [native.replace("\\", "\\\\"), native, path.as_posix()]


def _anchor_rules(*, project_root: Path, agents_root: Path, core_root: Path,
                  run_dir: Path) -> list[tuple[re.Pattern[str], str]]:
    """Rules that map this run's real anchor directories onto logical tokens.

    Ordered narrowest first: the three anchors are replaced before the run directory that
    contains them, so nothing collapses to the wrong token.
    """
    ordered = [
        (agents_root, "<agents_root>"),
        (core_root, "<core_root>"),
        (project_root, "<project_root>"),
        (run_dir, "<workdir>"),
        (WORKDIR_ROOT, "<workdir>"),
    ]
    rules: list[tuple[re.Pattern[str], str]] = []
    for path, token in ordered:
        for spelling in _spellings(path.resolve()):
            if len(spelling) >= 4:
                rules.append((re.compile(re.escape(spelling), re.IGNORECASE), token))
    return rules


def _redact(text: str, extra_rules: list[tuple[re.Pattern[str], str]]) -> str:
    return redact_text(text, list(extra_rules) + build_rules())


def _seed(fixture: str, run_dir: Path, spec: RunSpec) -> tuple[list[str], str, Path]:
    """Copy ``fixture`` into ``run_dir`` and return ``(anchor_argv, profile_rel, project_dir)``."""
    profile_rel, project_subdir = _FIXTURE_LAYOUT[fixture]
    project_root = run_dir / "project"
    agents_root = run_dir / "agents"
    core_root = run_dir / "core"
    shutil.copytree(FIXTURES / fixture, project_root)
    (agents_root / "skills").mkdir(parents=True, exist_ok=True)
    core_root.mkdir(parents=True, exist_ok=True)

    profile_path = project_root / profile_rel
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["registry"] = json.loads(json.dumps(REGISTRY))
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

    project_dir = project_root if project_subdir == "." else project_root / project_subdir
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / spec.plan_arg).write_text(
        json.dumps({"feature": FEATURE, "tasks": list(spec.tasks)}, indent=2) + "\n",
        encoding="utf-8",
    )

    if spec.partial_state:
        _write_partial_state(project_dir, spec)

    anchor_argv = [
        "--project-root", str(project_root),
        "--agents-root", str(agents_root),
        "--core-root", str(core_root),
    ]
    return anchor_argv, profile_rel, project_dir


def _write_partial_state(project_dir: Path, spec: RunSpec) -> None:
    """Record a minimal, loadable run so ``--resume`` has something to resume."""
    storage = REGISTRY["storage"]["runs"]  # ".pipeline/runs"
    run_dir = project_dir.joinpath(*storage.split("/"), FEATURE)
    run_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": task["id"], "status": "implemented", "depends_on": list(task.get("depends_on", [])),
         "attempts": 1, "blocker": None}
        for task in spec.tasks
    ]
    run_dir.joinpath("run.json").write_text(
        json.dumps({
            "schema_version": 1,
            "feature": FEATURE,
            "prompt_path": spec.plan_arg,
            "plan_path": spec.plan_arg,
            "run_id": "harness-fixed-run-id",
            "status": "running",
            "tasks": tasks,
            "history": [],
            "commands": [],
        }, indent=2) + "\n",
        encoding="utf-8",
    )


def _invoke(argv: list[str]) -> tuple[object, str, str]:
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code: object = runner_cli.main(argv)
    except SystemExit as exit_call:  # argparse usage errors
        code = exit_call.code
    return code, out.getvalue(), err.getvalue()


def _capture_run(spec: RunSpec) -> str:
    run_dir = Path(tempfile.mkdtemp(dir=WORKDIR_ROOT, prefix=f"{spec.label}-"))
    try:
        if spec.fixture is None:
            argv = list(spec.raw_argv)
            rules = _anchor_rules(project_root=run_dir, agents_root=run_dir,
                                  core_root=run_dir, run_dir=run_dir)
        else:
            anchors, profile_rel, _ = _seed(spec.fixture, run_dir, spec)
            argv = anchors + ["--profile", profile_rel, "--plan", spec.plan_arg, "--dry-run"]
            if spec.prompt_arg is not None:
                argv += ["--prompt", spec.prompt_arg]
            argv += list(spec.extra_args)
            rules = _anchor_rules(
                project_root=run_dir / "project",
                agents_root=run_dir / "agents",
                core_root=run_dir / "core",
                run_dir=run_dir,
            )
        code, out, err = _invoke(argv)
        block = (
            f"### {spec.label}\n"
            f"$ run_pipeline.py {' '.join(argv)}\n"
            f"exit: {code}\n"
            f"--- stdout ---\n{out}"
            f"--- stderr ---\n{err}"
        )
        return _redact(block, rules)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def capture_scenario(name: str) -> str:
    """Return the deterministic, redacted capture for one scenario."""
    scenario = SCENARIOS[name]
    parts = [f"## {name} - {scenario.summary}"]
    parts.extend(_capture_run(run) for run in scenario.runs)
    return "\n\n".join(parts) + "\n"


def regenerate() -> dict[str, str]:
    return {name: capture_scenario(name) for name in SCENARIOS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dry_run_harness.py",
        description="Print the deterministic, redacted core-side S1-S7 dry-run captures.",
    )
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), action="append",
                        help="limit output to this scenario (repeatable); default: all")
    args = parser.parse_args(argv)
    names = args.scenario or list(SCENARIOS)
    for name in names:
        sys.stdout.write(capture_scenario(name))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
