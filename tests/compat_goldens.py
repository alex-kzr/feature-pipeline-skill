"""Compatibility golden harness (BL-01 — Phase 0 compatibility baseline).

This module regenerates every *declared public output* of the portable core as
deterministic, redacted text. :mod:`tests.test_compatibility_goldens` diffs each capture
against the reviewed fixture of the same name in ``tests/goldens/compatibility/``; that
directory's ``NORMALIZATIONS.md`` records, per golden, the source scenario and the exact
normalization applied to it.

The frozen surface (Phase 0 scope, "freeze accepted observable behavior before any
structural refactoring"):

* ``cli-help.txt`` — ``run_pipeline.py --help``.
* ``plan-only-plan.txt`` — the C1-C8 dry-run plan for ``--mode plan-only``.
* ``denial-execute-plan-gate.txt`` — ``--mode execute`` with the plan gate unsatisfied.
* ``denial-push.txt`` — ``--push`` refused before any artifact.
* ``error-unknown-task-type.txt`` — a task whose type has no registry route.
* ``error-absolute-logical-path.txt`` — an absolute ``--plan`` logical path.
* ``blocked-unmet-dependency.txt`` — a selected task whose dependency is not verified.
* ``status-no-recorded-run.txt`` — ``--status`` with nothing recorded yet.
* ``run-state-v2.json`` — a representative schema-v2 ``run.json`` built through the public
  :class:`pipeline_core.state.Run` API.
* ``diagnostic-report.md`` — a representative pre-transition ``diagnostic-report.md``.
* ``executor-status-envelope.json`` — the strict executor launch status envelope.
* ``verifier-verdict-envelope.json`` — the strict verifier verdict envelope.
* ``exit-code-table.txt`` — the observed exit code of each canonical scenario.

Regenerate the fixtures after a *reviewed* behavior change::

    python -m tests.compat_goldens --write

Standard library only. This module never imports from a test module; the suite imports
from here.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_core import runner_cli
from pipeline_core.commands import run_command
from pipeline_core.diagnostics import write_diagnostic_report
from pipeline_core.redaction import build_rules, path_variants, redact_text
from pipeline_core.reports import ENVELOPE_KEYS, STATUS_TOKENS
from pipeline_core.state import Run
from pipeline_core.verification import VERDICTS

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"
GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "compatibility"

#: The shared-project fixture already used by ``scripts/dry_run_harness.py`` — one closed
#: registry, a two-task routable graph (SF-01, SF-02) plus a ``plan-s5.json`` whose SF-04
#: has an unroutable type.
SHARED_FIXTURE = "shared-project"


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}[:-]\d{2}[:-]\d{2}Z")
_RUN_ID_RE = re.compile(r'("run_id":\s*")[^"]+(")')
_DURATION_RE = re.compile(r'("duration":\s*)[0-9]+(?:\.[0-9]+)?')
#: The pre-transition diagnostic renders a wall-clock command duration as ``(1.234s)``.
_DURATION_SUFFIX_RE = re.compile(r"\(\d+\.\d+s\)")
#: The recorded interpreter path inside a diagnostic's "Recent commands" line has already
#: had its home prefix redacted to ``<home>`` by the time it reaches this harness, so the
#: raw ``sys.executable`` replacement below no longer matches it — collapse whatever remains
#: up to ``python``/``python.exe`` to ``<python>`` (uv-managed interpreter paths have no
#: spaces).
_INTERPRETER_RE = re.compile(r"<(?:home|workdir|project_root)>[^\s`]*python(?:\.exe)?")


def _spellings(path: Path) -> list[str]:
    spellings: list[str] = []
    for variant in path_variants(path):
        native = str(variant)
        spellings.extend((native.replace("\\", "\\\\"), native, variant.as_posix()))
    return list(dict.fromkeys(spellings))


def _anchor_rules(*, project_root: Path, agents_root: Path, core_root: Path,
                  workdir: Path) -> list[tuple[re.Pattern[str], str]]:
    """Map this run's real anchor directories onto logical tokens, narrowest first."""
    ordered = [
        (agents_root, "<agents_root>"),
        (core_root, "<core_root>"),
        (project_root, "<project_root>"),
        (workdir, "<workdir>"),
    ]
    rules: list[tuple[re.Pattern[str], str]] = []
    for path, token in ordered:
        for spelling in _spellings(path):
            if len(spelling) >= 4:
                rules.append((re.compile(re.escape(spelling), re.IGNORECASE), token))
    return rules


def _normalize(text: str, extra_rules: list[tuple[re.Pattern[str], str]]) -> str:
    """Redact host paths / secrets, then blank the intentionally nondeterministic values.

    Four value classes are normalized, each documented in ``NORMALIZATIONS.md``: the test
    interpreter path -> ``<python>``, UTC timestamps -> ``<timestamp>``, the run id ->
    ``<run-id>``, and command durations -> ``0`` / ``(<duration>s)``.
    """
    exe = Path(sys.executable)
    for spelling in (str(exe), exe.as_posix(), str(exe).replace("\\", "\\\\")):
        text = text.replace(spelling, "<python>")
    out = redact_text(text, list(extra_rules) + build_rules())
    out = _RUN_ID_RE.sub(r"\1<run-id>\2", out)
    out = _TIMESTAMP_RE.sub("<timestamp>", out)
    out = _DURATION_RE.sub(r"\g<1>0", out)
    out = _DURATION_SUFFIX_RE.sub("(<duration>s)", out)
    out = _INTERPRETER_RE.sub("<python>", out)
    return out.replace("\r\n", "\n")


# --------------------------------------------------------------------------------------
# CLI invocation
# --------------------------------------------------------------------------------------

def _invoke(argv: list[str]) -> tuple[object, str, str]:
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code: object = runner_cli.main(argv)
    except SystemExit as exit_call:  # --help and argparse usage errors
        code = exit_call.code
    return code, out.getvalue(), err.getvalue()


def _seed_shared(workdir: Path) -> tuple[list[str], Path]:
    """Copy the shared-project fixture under ``workdir``; return anchor argv + project dir."""
    project_root = workdir / "project"
    agents_root = workdir / "agents"
    core_root = workdir / "core"
    shutil.copytree(FIXTURES / SHARED_FIXTURE, project_root)
    (agents_root / "skills").mkdir(parents=True, exist_ok=True)
    core_root.mkdir(parents=True, exist_ok=True)
    anchors = [
        "--project-root", str(project_root),
        "--agents-root", str(agents_root),
        "--core-root", str(core_root),
    ]
    return anchors, project_root


#: A full-metadata JSON plan (execute mode rejects the id+type-only shape). Its one task
#: routes through the shared-project registry's ``docs`` type.
_EXECUTE_PLAN = {
    "feature": "compat-execute",
    "tasks": [
        {
            "id": "CE-01",
            "task_type": "docs",
            "executor": "executor",
            "depends_on": [],
            "allowed_scope": ["content/ce-01.txt"],
            "acceptance_criteria": ["The task reaches verified through two PASS verdicts."],
        }
    ],
}


def _write_execute_plan(project_root: Path) -> None:
    (project_root / "plan-exec.json").write_text(
        json.dumps(_EXECUTE_PLAN, indent=2) + "\n", encoding="utf-8")


def _capture_cli(label: str, extra_argv: list[str], *, seed: bool = True,
                 prep=None) -> tuple[str, object]:
    """Run one CLI invocation under a throwaway workdir and return ``(block, exit_code)``."""
    workdir = Path(tempfile.mkdtemp(prefix=f"compat-{label}-"))
    try:
        if seed:
            anchors, project_root = _seed_shared(workdir)
            if prep is not None:
                prep(project_root)
            argv = anchors + ["--profile", "profile.json"] + extra_argv
            rules = _anchor_rules(
                project_root=project_root,
                agents_root=workdir / "agents",
                core_root=workdir / "core",
                workdir=workdir,
            )
        else:
            argv = list(extra_argv)
            rules = _anchor_rules(project_root=workdir, agents_root=workdir,
                                  core_root=workdir, workdir=workdir)
        code, out, err = _invoke(argv)
        block = (
            f"$ run_pipeline.py {' '.join(argv)}\n"
            f"exit: {code}\n"
            f"--- stdout ---\n{out}"
            f"--- stderr ---\n{err}"
        )
        return _normalize(block, rules), code
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------------------
# Non-CLI public outputs
# --------------------------------------------------------------------------------------

def _representative_run_state() -> str:
    """A schema-v2 ``run.json`` covering the terminal and in-flight task states.

    Built only through the public :class:`Run` API so the frozen shape stays honest: a
    verified task (two PASS verdicts), an implemented task, a blocked task, and a still
    pending task, plus one recorded command and one sourced control.
    """
    workdir = Path(tempfile.mkdtemp(prefix="compat-run-state-"))
    try:
        prompt = workdir / "prompt.md"
        prompt.write_text("compatibility baseline prompt\n", encoding="utf-8")
        plan = workdir / "plan.json"
        plan.write_text("{}\n", encoding="utf-8")
        run = Run.create("compat-sample", prompt, plan, workdir / "runs" / "compat-sample", workdir)
        run.status = "running"
        for task_id in ("T-01", "T-02", "T-03", "T-04"):
            run.add_task(task_id, depends_on=["T-01"] if task_id != "T-01" else None)

        # T-01: pending -> ready -> running -> implemented -> verified
        for state in ("ready", "running", "implemented"):
            run.transition_task("T-01", state, actor="executor" if state == "implemented" else "runner")
        run.record_verdicts("T-01", "PASS", "PASS")

        # T-02: implemented, awaiting verification
        for state in ("ready", "running", "implemented"):
            run.transition_task("T-02", state, actor="executor" if state == "implemented" else "runner")

        # T-03: blocked by an external cause
        run.transition_task("T-03", "ready")
        run.transition_task("T-03", "blocked", note="external dependency unavailable")
        run.task("T-03").blocker = "external dependency unavailable"

        # T-04: still pending
        run.set_control("max_repair_attempts", 2, sourced="default")
        run.record_command("task:T-01", workdir, ["true"], 0, 0.0, "", "")
        run.current_task = "T-02"

        return _normalize(
            json.dumps(run.to_dict(), indent=2) + "\n",
            _anchor_rules(project_root=workdir, agents_root=workdir,
                          core_root=workdir, workdir=workdir),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _representative_diagnostic() -> str:
    """A representative pre-transition ``diagnostic-report.md`` (five sections, fixed order)."""
    workdir = Path(tempfile.mkdtemp(prefix="compat-diag-"))
    try:
        prompt = workdir / "prompt.md"
        prompt.write_text("prompt", encoding="utf-8")
        run = Run.create("compat-diagnostic", prompt, None, workdir / "runs" / "compat-diagnostic", workdir)
        run.add_task("T-1")
        for state in ("ready", "running"):
            run.transition_task("T-1", state)
        run_command(run, "task:T-1", ".", [sys.executable, "-c", "print('tool output line')"])
        path = write_diagnostic_report(
            run, task_id="T-1", attempt=1,
            git_diff="diff --git a/pipeline_core/x.py b/pipeline_core/x.py",
            git_status="M pipeline_core/x.py",
            note="verifier said FAIL")
        return _normalize(
            path.read_text(encoding="utf-8"),
            _anchor_rules(project_root=workdir, agents_root=workdir,
                          core_root=workdir, workdir=workdir),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _executor_status_envelope() -> str:
    """The strict executor launch status envelope — exactly :data:`ENVELOPE_KEYS`."""
    envelope = {"role": "executor", "status": STATUS_TOKENS[0], "task_id": "T-01", "attempt": 1}
    assert set(envelope) == set(ENVELOPE_KEYS), "envelope keys drifted from ENVELOPE_KEYS"
    return json.dumps(envelope, indent=2) + "\n"


def _verifier_verdict_envelope() -> str:
    """The strict verifier verdict envelope — a single ``verdict`` key, one known token."""
    assert "PASS" in VERDICTS
    return json.dumps({"verdict": "PASS"}, indent=2) + "\n"


# --------------------------------------------------------------------------------------
# Scenario table
# --------------------------------------------------------------------------------------

#: name -> (extra argv, seed a shared-project fixture?, prep callback or None)
_CLI_SCENARIOS: dict[str, tuple[list[str], bool, object]] = {
    "cli-help": (["--help"], False, None),
    "plan-only-plan": (
        ["--plan", "plan.json", "--dry-run", "--mode", "plan-only"], True, None),
    "denial-execute-plan-gate": (
        ["--plan", "plan-exec.json", "--mode", "execute"], True, _write_execute_plan),
    "denial-push": (["--push"], False, None),
    "error-unknown-task-type": (
        ["--plan", "plan-s5.json", "--task", "SF-04", "--dry-run"], True, None),
    "error-absolute-logical-path": (
        ["--plan", "/srv/shared-project/plan.json", "--dry-run"], True, None),
    "blocked-unmet-dependency": (
        ["--plan", "plan.json", "--task", "SF-02", "--dry-run"], True, None),
    "status-no-recorded-run": (["--plan", "plan.json", "--status"], True, None),
}


def regenerate() -> dict[str, str]:
    """Return ``{golden filename: text}`` for every frozen public output."""
    goldens: dict[str, str] = {}
    exit_rows: list[str] = []
    for name, (argv, seed, prep) in _CLI_SCENARIOS.items():
        text, code = _capture_cli(name, argv, seed=seed, prep=prep)
        goldens[f"{name}.txt"] = text
        exit_rows.append(f"{name}: {code}")

    goldens["run-state-v2.json"] = _representative_run_state()
    goldens["diagnostic-report.md"] = _representative_diagnostic()
    goldens["executor-status-envelope.json"] = _executor_status_envelope()
    goldens["verifier-verdict-envelope.json"] = _verifier_verdict_envelope()
    goldens["exit-code-table.txt"] = "\n".join(exit_rows) + "\n"
    return goldens


def load_golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compat_goldens.py",
        description="Print or (re)write the compatibility goldens.",
    )
    parser.add_argument("--write", action="store_true",
                        help="write the fixtures under tests/goldens/compatibility/")
    args = parser.parse_args(argv)
    goldens = regenerate()
    if args.write:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in goldens.items():
            (GOLDEN_DIR / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {len(goldens)} goldens to {GOLDEN_DIR}")
    else:
        for name, text in goldens.items():
            print(f"===== {name} =====")
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
