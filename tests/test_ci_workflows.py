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

UGA-04 (docs/plans/tasks/UGA-04_topology-drift-validator.md) generalizes this one
hand-written regression into ``ci/workflows.py``, a reusable validator covering every
committed and future workflow. The classes below this module's original UGA-01 suite
cover that validator's own rejection rules, the two committed topology fixtures
(``tests/fixtures/ci/standalone``, ``tests/fixtures/ci/nested``), and the retained
UGA-01 regression as a fixture (``tests/fixtures/ci/broken``). ``ci/workflows.py``
needs a maintained YAML parser (PyYAML, ``[project.optional-dependencies].dev`` only -
never a runtime dependency, docs/adr/003); the classes that actually parse workflow
YAML skip deterministically when that dev extra is not installed (see ``_HAS_YAML``
below), same as this project's other environment-dependent suites (tests/README.md
"Known exceptions").
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ci import workflows

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci"
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

try:
    import yaml as _yaml_probe  # noqa: F401 - existence probe only

    _HAS_YAML = True
except ImportError:  # pragma: no cover - exercised only without the dev extra
    _HAS_YAML = False

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


# ---------------------------------------------------------------------------------------------
# UGA-04 - ci/workflows.py: the reusable topology and drift validator.
# ---------------------------------------------------------------------------------------------


class UnsafeWorkingDirectoryRule(unittest.TestCase):
    """Requirements bullet 2 - one rejection reason at a time, and a safe value passes."""

    def test_absolute_path_is_rejected(self) -> None:
        self.assertEqual(
            workflows.unsafe_working_directory_reason("/etc/passwd"), "absolute path"
        )

    def test_windows_drive_letter_path_is_rejected(self) -> None:
        self.assertEqual(
            workflows.unsafe_working_directory_reason("C:\\repo"),
            "absolute path",
        )

    def test_home_prefixed_path_is_rejected(self) -> None:
        self.assertEqual(
            workflows.unsafe_working_directory_reason("~/repo"), "home-prefixed path"
        )

    def test_backslash_path_is_rejected(self) -> None:
        self.assertEqual(
            workflows.unsafe_working_directory_reason("dependency-b\\source-a"),
            "backslash path separator",
        )

    def test_traversal_path_is_rejected(self) -> None:
        self.assertEqual(
            workflows.unsafe_working_directory_reason("source-a/../other"), "path traversal"
        )

    def test_repository_name_derived_path_is_rejected(self) -> None:
        reason = workflows.unsafe_working_directory_reason(
            "feature-pipeline-skill", repository_names=("feature-pipeline-skill",)
        )
        self.assertIsNotNone(reason)
        self.assertIn("repository-name-derived", reason)

    def test_repository_name_derived_nested_path_is_rejected(self) -> None:
        reason = workflows.unsafe_working_directory_reason(
            "feature-pipeline-skill/nested", repository_names=("feature-pipeline-skill",)
        )
        self.assertIsNotNone(reason)

    def test_arbitrary_safe_value_is_accepted(self) -> None:
        self.assertIsNone(
            workflows.unsafe_working_directory_reason(
                "source-a", repository_names=("feature-pipeline-skill", "feature-pipeline")
            )
        )

    def test_unset_value_is_accepted(self) -> None:
        self.assertIsNone(workflows.unsafe_working_directory_reason(None))


class CheckoutLayoutRule(unittest.TestCase):
    """Requirements bullet 3 - a literal checkout path is only valid if it resolves on disk."""

    def test_nonexistent_root_is_a_deterministic_diagnostic(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            problem = workflows.checkout_layout_problem(root, "feature-pipeline-skill")
            self.assertIsNotNone(problem)
            self.assertIn("nonexistent root", problem)
            self.assertIn("feature-pipeline-skill", problem)

    def test_missing_markers_under_an_existing_directory_is_reported(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source-a").mkdir()
            problem = workflows.checkout_layout_problem(root, "source-a")
            self.assertIsNotNone(problem)
            self.assertIn("pyproject.toml", problem)

    def test_a_resolving_checkout_reports_no_problem(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            nested = root / "source-a"
            nested.mkdir()
            (nested / "pyproject.toml").write_text("", encoding="utf-8")
            (nested / "tests").mkdir()
            self.assertIsNone(workflows.checkout_layout_problem(root, "source-a"))

    def test_unset_working_directory_resolves_to_the_root_itself(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "pyproject.toml").write_text("", encoding="utf-8")
            (root / "tests").mkdir()
            self.assertIsNone(workflows.checkout_layout_problem(root, None))


class MatrixAndDocumentationDriftRule(unittest.TestCase):
    """Requirements bullet 5 - compare the workflow matrix, manifest, and README suites."""

    def test_missing_manifest_suite_is_reported(self) -> None:
        violations = workflows._matrix_and_documentation_drift(
            "quality-gates.yml",
            {"platform"},
            {"platform", "fault-injection"},
            {"platform", "fault-injection"},
        )
        rules = {v.rule for v in violations}
        self.assertIn("matrix-drift", rules)

    def test_extra_workflow_suite_is_reported(self) -> None:
        violations = workflows._matrix_and_documentation_drift(
            "quality-gates.yml", {"platform", "made-up"}, {"platform"}, {"platform"}
        )
        self.assertTrue(
            any("made-up" in v.detail for v in violations if v.rule == "matrix-drift")
        )

    def test_undocumented_manifest_suite_is_reported(self) -> None:
        violations = workflows._matrix_and_documentation_drift(
            "quality-gates.yml", {"platform"}, {"platform"}, set()
        )
        self.assertTrue(any(v.rule == "documentation-drift" for v in violations))

    def test_matching_sets_report_nothing(self) -> None:
        violations = workflows._matrix_and_documentation_drift(
            "quality-gates.yml", {"platform"}, {"platform"}, {"platform"}
        )
        self.assertEqual(violations, [])


class LazyYamlImportDiscipline(unittest.TestCase):
    """AC-5 - a source root with no workflows never needs the dev-only YAML parser."""

    def test_no_workflow_directory_returns_no_violations_without_importing_yaml(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "pyproject.toml").write_text("", encoding="utf-8")
            (root / "tests").mkdir()
            original = workflows._yaml_module
            workflows._yaml_module = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
                AssertionError("must not import yaml with no workflow files present")
            )
            try:
                self.assertEqual(workflows.validate_topology(root), [])
            finally:
                workflows._yaml_module = original

    def test_missing_parser_raises_a_deterministic_error(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            workflow_dir = root / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            (workflow_dir / "quality-gates.yml").write_text("name: x\n", encoding="utf-8")
            original = workflows._yaml_module
            workflows._yaml_module = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
                workflows.YamlUnavailable("pyyaml is not installed")
            )
            try:
                with self.assertRaises(workflows.YamlUnavailable):
                    workflows.validate_topology(root)
            finally:
                workflows._yaml_module = original


class YamlDependencyIsDevOnly(unittest.TestCase):
    """AC-5 - the installed wheel carries no runtime dependency on the YAML parser."""

    def test_dependencies_stay_empty_and_pyyaml_is_dev_only(self) -> None:
        with _PYPROJECT.open("rb") as handle:
            data = tomllib.load(handle)
        project = data["project"]
        self.assertEqual(project.get("dependencies", []), [])
        dev = " ".join(project.get("optional-dependencies", {}).get("dev", [])).lower()
        self.assertIn("pyyaml", dev)


@unittest.skipUnless(_HAS_YAML, "pyyaml dev dependency (uv sync --extra dev) not installed")
class GateUsageRules(unittest.TestCase):
    """Requirements bullet 4 and 6 - unknown gates, driver bypass, and the SHA binding."""

    def _root(self, tmp: Path, workflow_text: str) -> Path:
        (tmp / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp / "tests").mkdir()
        workflow_dir = tmp / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "quality-gates.yml").write_text(workflow_text, encoding="utf-8")
        return tmp

    def test_unknown_gate_id_is_rejected(self) -> None:
        workflow = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: run it
        run: uv run python -m ci.run run made-up-gate --source-root . --expected-source-sha x
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertTrue(any(v.rule == "unknown-gate" for v in violations))

    def test_missing_expected_source_sha_is_rejected(self) -> None:
        workflow = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: run it
        run: uv run python -m ci.run run lint --source-root .
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertTrue(any(v.rule == "missing-expected-sha" for v in violations))

    def test_direct_gate_command_bypasses_the_driver(self) -> None:
        workflow = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: run it
        run: uv run --with ruff ruff check .
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertTrue(any(v.rule == "bypasses-driver" for v in violations))

    def test_driver_invocation_with_a_known_gate_and_sha_is_clean(self) -> None:
        workflow = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: run it
        run: uv run python -m ci.run run lint --source-root . --expected-source-sha x
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertEqual(violations, [])

    def test_unparseable_yaml_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), "jobs: [unterminated")
            violations = workflows.validate_topology(root)
        self.assertTrue(any(v.rule == "unparseable-yaml" for v in violations))


@unittest.skipUnless(_HAS_YAML, "pyyaml dev dependency (uv sync --extra dev) not installed")
class CommittedTopologyFixtures(unittest.TestCase):
    """AC-1, AC-2 - the two migrated fixtures pass; the retained UGA-01 bug fixture fails."""

    def test_standalone_fixture_has_no_violations(self) -> None:
        root = FIXTURES / "standalone" / "source-a"
        self.assertEqual(workflows.validate_topology(root), [])

    def test_nested_fixture_has_no_violations(self) -> None:
        root = FIXTURES / "nested" / "dependency-b"
        self.assertEqual(workflows.validate_topology(root), [])

    def test_broken_fixture_fails_with_a_nonexistent_root_diagnostic(self) -> None:
        """AC-1: the original core `working-directory: feature-pipeline-skill` bug."""

        root = FIXTURES / "broken" / "feature-pipeline-skill"
        violations = workflows.validate_topology(root)
        layout_violations = [v for v in violations if v.rule == "checkout-path-drift"]
        self.assertTrue(layout_violations, violations)
        self.assertIn("nonexistent root", layout_violations[0].detail)
        # Independently, the same value is also flagged as repository-name-derived (bullet 2).
        self.assertTrue(any(v.rule == "unsafe-working-directory" for v in violations))

    def test_broken_fixture_raises_through_check(self) -> None:
        root = FIXTURES / "broken" / "feature-pipeline-skill"
        with self.assertRaises(workflows.WorkflowValidationError) as ctx:
            workflows.check(root)
        self.assertTrue(ctx.exception.violations)


@unittest.skipUnless(_HAS_YAML, "pyyaml dev dependency (uv sync --extra dev) not installed")
class RunCliValidateIntegration(unittest.TestCase):
    """Integration Note - ``ci/run.py validate`` is the only CLI surface (no second CLI)."""

    def test_validate_reports_clean_topology_for_the_standalone_fixture(self) -> None:
        import io

        from ci import run as run_cli

        out = io.StringIO()
        root = FIXTURES / "standalone" / "source-a"
        exit_code = run_cli.main(["validate", "--source-root", str(root)], stdout=out)
        self.assertEqual(exit_code, 0)
        self.assertIn("workflow topology: no violations", out.getvalue())

    def test_validate_fails_for_the_broken_fixture(self) -> None:
        import io

        from ci import run as run_cli

        out = io.StringIO()
        root = FIXTURES / "broken" / "feature-pipeline-skill"
        exit_code = run_cli.main(["validate", "--source-root", str(root)], stdout=out)
        self.assertEqual(exit_code, 1)
        self.assertIn("workflow topology violations", out.getvalue())


if __name__ == "__main__":
    unittest.main()
