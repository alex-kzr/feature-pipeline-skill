#!/usr/bin/env python3
"""Core-side dry-run acceptance harness for the S1-S7 parallel-run comparison.

For each scenario this module:

1. copies the shared-project fixture core inputs into a fresh temporary directory,
2. runs the core runner CLI (:mod:`pipeline_core.runner_cli`, the logic behind
   ``scripts/run_pipeline.py``) with the scenario's arguments,
3. captures the generated argv, stdout, stderr, and exit code,
4. redacts the capture with :mod:`pipeline_core.redaction` plus explicit anchor rules so
   every host path becomes a logical ``<project_root>`` / ``<agents_root>`` / ``<core_root>``
   token and no user name, machine name, credential, or token survives,
5. returns the capture as deterministic text — byte-identical across two runs from
   independent clean fixture copies.

The fixture is [`fixtures/shared-project/`](../fixtures/shared-project/) — the same task
graph the legacy runner consumes through its Markdown encoding under
``docs/acceptance/fixtures/shared-project/`` in the parent repository. These three JSON
files (``profile.json``, ``plan.json`` = SF-01/SF-02, ``plan-s5.json`` = SF-03/SF-04) are a
**vendored copy** of the canonical fixture's core inputs, kept here so the harness stays
self-contained; ``tests/test_dry_run_harness.py`` asserts they stay byte-identical to the
canonical copies whenever the parent path is resolvable.

The harness writes only under a temporary directory; it never writes under
``docs/acceptance/`` (that is CX-01's scope) and never passes ``--commit``. ``--push``
appears only in the S4 denial scenario, which asserts the refusal and that no artifact is
created.

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
from pipeline_core.redaction import build_rules, path_variants, redact_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: The one shared fixture both runners drive from. Only the core encoding lives here; the
#: legacy Markdown encoding of the same graph is the canonical fixture in the parent repo.
SHARED_FIXTURE = "shared-project"

#: One dedicated temp root for the whole process; per-run copies live below it and are
#: removed as soon as their capture is taken.
WORKDIR_ROOT = Path(tempfile.mkdtemp(prefix="core-dry-run-harness-"))
atexit.register(lambda: shutil.rmtree(WORKDIR_ROOT, ignore_errors=True))

#: Where the profile lives inside the fixture and which subdirectory the profile's
#: ``logical_paths.project`` points at.
_FIXTURE_LAYOUT = {SHARED_FIXTURE: ("profile.json", ".")}


@dataclass(frozen=True)
class RunSpec:
    """One CLI invocation inside a scenario."""

    label: str
    #: ``None`` means "do not seed a fixture" — ``raw_argv`` is passed to the CLI verbatim.
    fixture: str | None = None
    raw_argv: tuple[str, ...] = ()
    #: Which vendored plan file the scenario runs over. May also be an intentionally
    #: unsafe literal (the S5a absolute-path probe) that the CLI must reject.
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
        "plan-only dry run over the routable sub-graph (SF-01, SF-02)",
        (RunSpec("S1", fixture=SHARED_FIXTURE, extra_args=("--mode", "plan-only")),),
    ),
    "S2": Scenario(
        "SF-02 selected while its dependency SF-01 is not done -> fail closed",
        (RunSpec("S2", fixture=SHARED_FIXTURE, extra_args=("--task", "SF-02")),),
    ),
    "S3": Scenario(
        "--through selects the main plan up to the last routable task",
        (RunSpec("S3", fixture=SHARED_FIXTURE, extra_args=("--through", "SF-02")),),
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
                fixture=SHARED_FIXTURE,
                plan_arg="/srv/shared-project/content/SF-03.md",
            ),
            RunSpec(
                "S5b-unknown-type",
                fixture=SHARED_FIXTURE,
                plan_arg="plan-s5.json",
                extra_args=("--task", "SF-04"),
            ),
        ),
    ),
    "S6": Scenario(
        "--resume from a partially recorded run state (base task SF-01)",
        (
            RunSpec(
                "S6",
                fixture=SHARED_FIXTURE,
                extra_args=("--task", "SF-01", "--resume"),
                partial_state=True,
            ),
        ),
    ),
    "S7": Scenario(
        "profile-routed dry run of SF-01 (registry route resolution)",
        (RunSpec("S7", fixture=SHARED_FIXTURE, extra_args=("--task", "SF-01")),),
    ),
}


def _spellings(path: Path) -> list[str]:
    spellings: list[str] = []
    for variant in path_variants(path):
        native = str(variant)
        spellings.extend((native.replace("\\", "\\\\"), native, variant.as_posix()))
    return list(dict.fromkeys(spellings))


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
        for spelling in _spellings(path):
            if len(spelling) >= 4:
                rules.append((re.compile(re.escape(spelling), re.IGNORECASE), token))
    return rules


def _redact(text: str, extra_rules: list[tuple[re.Pattern[str], str]]) -> str:
    return redact_text(text, list(extra_rules) + build_rules())


def _plan_feature(plan_path: Path) -> str:
    """The ``feature`` field of a vendored plan file (the run-directory name the CLI uses)."""
    return str(json.loads(plan_path.read_text(encoding="utf-8"))["feature"])


def _plan_tasks(plan_path: Path) -> list[dict]:
    return list(json.loads(plan_path.read_text(encoding="utf-8"))["tasks"])


def _seed(fixture: str, run_dir: Path, spec: RunSpec) -> tuple[list[str], str, Path]:
    """Copy ``fixture`` into ``run_dir`` and return ``(anchor_argv, profile_rel, project_dir)``.

    The fixture already carries ``profile.json`` (with its own closed registry) and the
    ``plan*.json`` files verbatim — nothing is synthesised or overwritten here.
    """
    profile_rel, project_subdir = _FIXTURE_LAYOUT[fixture]
    project_root = run_dir / "project"
    agents_root = run_dir / "agents"
    core_root = run_dir / "core"
    shutil.copytree(FIXTURES / fixture, project_root)
    (agents_root / "skills").mkdir(parents=True, exist_ok=True)
    core_root.mkdir(parents=True, exist_ok=True)

    project_dir = project_root if project_subdir == "." else project_root / project_subdir

    if spec.partial_state:
        _write_partial_state(project_dir, spec)

    anchor_argv = [
        "--project-root", str(project_root),
        "--agents-root", str(agents_root),
        "--core-root", str(core_root),
    ]
    return anchor_argv, profile_rel, project_dir


def _write_partial_state(project_dir: Path, spec: RunSpec) -> None:
    """Record a minimal, loadable run so ``--resume`` has something to resume.

    Keyed off the real vendored plan the scenario resumes, so the run directory name and
    the task set match what the CLI derives from that plan.
    """
    plan_path = project_dir / spec.plan_arg
    feature = _plan_feature(plan_path)
    storage = "runs"  # profile registry storage key; see fixtures/shared-project/profile.json
    run_dir = project_dir / ".pipeline" / storage / feature
    run_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": task["id"], "status": "implemented",
         "depends_on": list(task.get("depends_on", [])), "attempts": 1, "blocker": None}
        for task in _plan_tasks(plan_path)
    ]
    run_dir.joinpath("run.json").write_text(
        json.dumps({
            "schema_version": 1,
            "feature": feature,
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
