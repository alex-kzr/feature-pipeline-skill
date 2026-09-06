"""Topology and drift contract for the core ``quality-gates`` workflow.

UGA-01 (docs/plans/tasks/UGA-01_restore-standalone-quality-workflow.md). The core
workflow is executed from the root of the standalone ``feature-pipeline-skill``
repository. Every job used to declare ``working-directory: feature-pipeline-skill``,
so GitHub tried to start each shell below a directory that does not exist in a
standalone checkout and no gate ran.

These tests parse the committed workflow with the standard library only (the runtime
and its CI tooling never depend on a YAML package) and assert:

* AC-1 - a synthetic standalone checkout resolves the core source root to the
  checkout root and finds ``pyproject.toml`` and ``tests/``;
* AC-2 - no core job declares a repository-name-derived ``working-directory``;
* AC-3 - every core job reaches a preflight before its first quality command;
* AC-4 - the gate commands and matrix cells are unchanged apart from the removed
  directory assumption and the added preflight.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)

# Copied verbatim from tests/README.md / the QG-02 workflow: the gate argv this task
# must preserve byte-for-byte.
EXPECTED_GATE_COMMANDS = [
    "uv run --with ruff ruff check .",
    "uv run --with mypy mypy",
    "uv run --with coverage coverage run -m unittest discover -s tests -t .",
    "uv run --with coverage coverage report",
    "uv run python -m unittest tests.test_process_runner tests.test_worktree "
    "tests.test_worktree_bounded_attribution",
    "uv run python -m unittest tests.test_concurrency tests.test_scope_gate "
    "tests.test_git_safety_allowlist tests.test_launch_controls "
    "tests.test_critical_behavior_characterization",
    "uv run python -m unittest tests.test_worktree_performance_baseline",
]

EXPECTED_JOBS = {"lint-and-types", "coverage", "policy-suites"}

PREFLIGHT_TOKENS = (
    "GITHUB_WORKSPACE",
    "GITHUB_REPOSITORY",
    "GITHUB_SHA",
    "pyproject.toml",
    "tests",
)


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _jobs(text: str) -> dict[str, str]:
    """Map each ``jobs:`` entry to the raw text of its block."""

    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    blocks: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in lines[start + 1 :]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            if current is not None:
                blocks[current] = "\n".join(buffer)
            current = match.group(1)
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        blocks[current] = "\n".join(buffer)
    return blocks


def _steps(job_text: str) -> list[str]:
    """Return each step of a job as text, without the trailing comment lines that
    actually document the *next* step."""

    marker = "\n    steps:"
    body = job_text[job_text.index(marker) + len(marker) :]
    steps: list[str] = []
    for chunk in re.split(r"\n      - ", body):
        lines = chunk.splitlines()
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
            lines.pop()
        text = "\n".join(lines).strip()
        if text:
            steps.append(text)
    return steps


def _step_name(step: str) -> str:
    match = re.search(r"name:\s*(.+)", step)
    if match:
        return match.group(1).strip()
    match = re.search(r"uses:\s*(.+)", step)
    if match:
        return match.group(1).strip()
    return step.splitlines()[0]


def _working_directory(job_text: str) -> str | None:
    match = re.search(r"working-directory:\s*(\S+)", job_text)
    return match.group(1) if match else None


class CoreWorkflowTopologyTests(unittest.TestCase):
    def test_workflow_file_exists(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), WORKFLOW)

    def test_defines_the_expected_jobs(self) -> None:
        self.assertEqual(set(_jobs(_text())), EXPECTED_JOBS)

    def test_standalone_checkout_resolves_source_root(self) -> None:
        """AC-1: in a standalone checkout every job's source root is the checkout root."""

        text = _text()
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "pyproject.toml").write_text("", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "README.md").write_text("", encoding="utf-8")

            for job, block in _jobs(text).items():
                working_directory = _working_directory(block)
                resolved = root if working_directory is None else root / working_directory
                with self.subTest(job=job):
                    self.assertTrue(resolved.is_dir(), resolved)
                    self.assertTrue((resolved / "pyproject.toml").is_file())
                    self.assertTrue((resolved / "tests").is_dir())

    def test_no_repository_name_derived_working_directory(self) -> None:
        """AC-2."""

        text = _text()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = re.search(r"working-directory:\s*(\S+)", line)
            if match is None:
                continue
            value = match.group(1)
            with self.subTest(value=value):
                self.assertFalse(value.startswith("/"), value)
                self.assertNotIn("feature-pipeline", value)

    def test_every_job_preflights_before_its_first_gate(self) -> None:
        """AC-3."""

        text = _text()
        for job, block in _jobs(text).items():
            steps = _steps(block)
            names = [_step_name(step) for step in steps]
            preflight_index = next(
                (i for i, name in enumerate(names) if "preflight" in name.lower()),
                None,
            )
            gate_index = next(
                (
                    i
                    for i, step in enumerate(steps)
                    if re.search(r"run:\s*uv run ", step)
                    and any(command in step for command in EXPECTED_GATE_COMMANDS)
                ),
                None,
            )
            with self.subTest(job=job):
                self.assertIsNotNone(preflight_index, f"{job}: no preflight step")
                self.assertIsNotNone(gate_index, f"{job}: no gate command step")
                self.assertLess(preflight_index, gate_index)
                preflight = steps[preflight_index]
                self.assertIn("uv run python", preflight)
                for token in PREFLIGHT_TOKENS:
                    self.assertIn(token, preflight, token)

    def test_preflight_is_identical_across_jobs(self) -> None:
        """AC-3: the same first preflight is added to every job."""

        text = _text()
        preflights = set()
        for block in _jobs(text).values():
            for step in _steps(block):
                if "preflight" in _step_name(step).lower():
                    preflights.add(step)
        self.assertEqual(len(preflights), 1, preflights)

    def test_gate_commands_are_unchanged(self) -> None:
        """AC-4: every original gate argv is still present, and nothing new was added."""

        found = re.findall(r"^\s*run:\s*(uv run .+?)\s*$", _text(), re.M)
        self.assertEqual(sorted(found), sorted(EXPECTED_GATE_COMMANDS))

    def test_matrix_cells_are_unchanged(self) -> None:
        """AC-4."""

        text = _text()
        self.assertIn("os: [ubuntu-latest, windows-latest]", text)
        self.assertIn("suite: [platform, fault-injection, performance]", text)
        self.assertEqual(text.count("runs-on: ubuntu-latest"), 2)


if __name__ == "__main__":
    unittest.main()
