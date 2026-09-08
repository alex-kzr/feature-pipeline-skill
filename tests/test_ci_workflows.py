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

from ci import contract, workflows

from tests._umbrella import require_umbrella, umbrella_root

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)

# UGA-06: the umbrella (``feature-pipeline``) consumer workflow lives one level above the core
# submodule checkout, not inside it.
UMBRELLA_WORKFLOW = (
    umbrella_root()
    / ".github"
    / "workflows"
    / "installed-package.yml"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci"
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

try:
    import yaml as _yaml_probe  # noqa: F401 - existence probe only

    _HAS_YAML = True
except ImportError:  # pragma: no cover - exercised only without the dev extra
    _HAS_YAML = False

# The UGA-01 transitional workflow copied these argv into YAML. UGA-05 must keep
# them owned exclusively by ci/gates.toml and reach them through the driver.
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

EXPECTED_JOBS = {"prepare-core-gates", "core-gates"}

EXPECTED_REQUIRED_CHECK_NAMES = (
    "ruff + mypy (incl. complexity)",
    "coverage (branch, ratcheted floor)",
    "format('{0} · {1}', matrix.suite, matrix.os)",
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

    def test_core_gates_are_a_manifest_driven_adapter(self) -> None:
        """AC-1 through AC-4: only the driver owns core gate execution."""

        text = _text()
        self.assertIn("ci.run list --json --group core", text)
        self.assertIn("ci.run validate --source-root \"$GITHUB_WORKSPACE\"", text)
        self.assertIn("needs: prepare-core-gates", text)
        self.assertIn("fromJSON(needs.prepare-core-gates.outputs.matrix)", text)
        for name in EXPECTED_REQUIRED_CHECK_NAMES:
            self.assertIn(name, text)
        self.assertIn("ci.run run ${{ matrix.gate.id }}", text)
        for token in (
            "--source-root \"$GITHUB_WORKSPACE\"",
            "--expected-source-sha \"${{ github.sha }}\"",
            "--workflow-repo \"${{ github.repository }}\"",
            "--workflow-sha \"${{ github.sha }}\"",
        ):
            self.assertIn(token, text)
        for command in EXPECTED_GATE_COMMANDS:
            self.assertNotIn(command, text)


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


# ---------------------------------------------------------------------------------------------
# UGA-16 - `strategy.matrix` / `matrix.include` may be a GitHub Actions expression string.
# ---------------------------------------------------------------------------------------------


@unittest.skipUnless(_HAS_YAML, "pyyaml dev dependency (uv sync --extra dev) not installed")
class ExpressionValuedMatrixRule(unittest.TestCase):
    """UGA-16 AC-2 - a job whose ``strategy.matrix`` (or ``matrix.include``) is a
    ``${{ ... }}`` expression string, resolved only at run time, must not raise and must
    not emit a spurious matrix/documentation drift violation. Jobs that still declare a
    literal mapping keep their drift checks (AC-3)."""

    def _root(self, tmp: Path, workflow_text: str) -> Path:
        (tmp / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp / "tests").mkdir()
        workflow_dir = tmp / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "quality-gates.yml").write_text(workflow_text, encoding="utf-8")
        return tmp

    def test_expression_valued_matrix_does_not_raise_or_drift(self) -> None:
        workflow = """
jobs:
  core-gates:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix: ${{ fromJSON(needs.prepare-core-gates.outputs.matrix) }}
    steps:
      - name: run it
        run: uv run python -m ci.run run lint --source-root . --expected-source-sha x
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertEqual(
            [v for v in violations if v.rule in ("matrix-drift", "documentation-drift")],
            [],
            violations,
        )

    def test_expression_valued_matrix_include_does_not_raise(self) -> None:
        workflow = """
jobs:
  isolated:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include: ${{ fromJSON(needs.prepare-consumer-gates.outputs.include) }}
    steps:
      - name: run it
        run: uv run python -m ci.run run installed-package --source-root . --expected-source-sha x
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertNotIn(
            "matrix-drift", {v.rule for v in violations}, violations
        )

    def test_literal_matrix_drift_is_still_reported(self) -> None:
        workflow = """
jobs:
  core-gates:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        suite: [made-up-suite]
    steps:
      - name: run it
        run: uv run python -m ci.run run lint --source-root . --expected-source-sha x
"""
        with TemporaryDirectory() as raw:
            root = self._root(Path(raw), workflow)
            violations = workflows.validate_topology(root)
        self.assertTrue(
            any("made-up-suite" in v.detail for v in violations if v.rule == "matrix-drift"),
            violations,
        )


# ---------------------------------------------------------------------------------------------
# UGA-06 - the umbrella `installed-package.yml` as a thin nested-topology adapter.
# ---------------------------------------------------------------------------------------------


class UmbrellaConsumerAdapterTests(unittest.TestCase):
    """UGA-06 AC-1..AC-4: the umbrella consumer workflow is a thin adapter over the same
    driver/contract the core workflow uses - explicit submodule source root, the exact
    ``git rev-parse HEAD:feature-pipeline-skill`` gitlink SHA, driver-only gate execution,
    a driver-expanded matrix, and no unconditional evidence step."""

    def setUp(self) -> None:
        # installed-package.yml lives in the umbrella (feature-pipeline) repo, never in a
        # standalone feature-pipeline-skill checkout.
        require_umbrella(".github/workflows/installed-package.yml")
        if not UMBRELLA_WORKFLOW.is_file():
            self.fail(f"missing umbrella workflow {UMBRELLA_WORKFLOW}")
        self.text = UMBRELLA_WORKFLOW.read_text(encoding="utf-8")

    def test_source_root_is_the_explicit_submodule_path(self) -> None:
        # AC-2: the driver is told exactly where the core is; nothing is inferred from cwd.
        self.assertIn(
            '--source-root "$GITHUB_WORKSPACE/feature-pipeline-skill"', self.text
        )

    def test_no_repository_name_derived_working_directory(self) -> None:
        # AC-2: renaming a checkout directory cannot change behaviour because no job or step
        # pins one (a `working-directory: feature-pipeline-skill` is the exact UGA-01 bug).
        self.assertNotIn("working-directory:", self.text)

    def test_matrix_comes_from_the_driver_not_yaml(self) -> None:
        # AC-4: the consumer matrix is expanded from `ci.run list --json`, not transcribed.
        self.assertIn("ci.run list --json --group consumer", self.text)
        self.assertIn(
            "fromJSON(needs.prepare-consumer-gates.outputs.include)", self.text
        )
        self.assertIn("needs: prepare-consumer-gates", self.text)

    def test_gate_runs_only_through_the_driver(self) -> None:
        self.assertIn("ci.run run ${{ matrix.gate.id }}", self.text)
        for embedded in (
            "unittest discover -s tests -t .",
            "unittest -v tests.test_installed_wheel",
            "python -m tests.installed_wheel --json",
        ):
            self.assertNotIn(embedded, self.text)

    def test_expected_core_sha_is_the_exact_gitlink(self) -> None:
        # AC-1: the tested SHA is the umbrella tree's gitlink, and the actual submodule
        # checkout SHA is printed next to it for evidence.
        self.assertIn("git rev-parse HEAD:feature-pipeline-skill", self.text)
        self.assertIn("git -C feature-pipeline-skill rev-parse HEAD", self.text)
        self.assertIn(
            '--expected-source-sha "${{ steps.gitlink.outputs.sha }}"', self.text
        )

    def test_core_branch_head_is_never_queried(self) -> None:
        # Risks note: reading origin/main or the submodule's remote head would allow a
        # false-green consumer result.
        self.assertNotIn("origin/main", self.text)
        self.assertNotIn("ls-remote", self.text)

    def test_evidence_collection_is_not_unconditional(self) -> None:
        # AC-3: nothing runs after a failed checkout/preflight, so a missing uv/tool/path
        # cannot produce a second, misleading failure.
        self.assertNotIn("always()", self.text)

    def test_preserves_the_full_os_python_consumer_matrix(self) -> None:
        # AC-4: ubuntu/windows x py3.11/3.12/3.13, owned by the manifest.
        gate = contract.load(
            Path(__file__).resolve().parents[1] / "ci" / "gates.toml"
        ).gate("installed-package")
        self.assertEqual(set(gate.os), {"ubuntu-latest", "windows-latest"})
        self.assertEqual(set(gate.python), {"3.11", "3.12", "3.13"})


@unittest.skipUnless(_HAS_YAML, "pyyaml dev dependency (uv sync --extra dev) not installed")
class UmbrellaWorkflowValidatesInAnArbitrarilyNamedNestedCheckout(unittest.TestCase):
    """UGA-06 AC-2: the committed umbrella workflow reports zero topology/drift violations
    when the umbrella and its submodule are checked out under directory names that match no
    repository this project ships under."""

    def test_no_violations_in_a_renamed_nested_layout(self) -> None:
        require_umbrella(".github/workflows/installed-package.yml")
        workflow_text = UMBRELLA_WORKFLOW.read_text(encoding="utf-8")
        with TemporaryDirectory() as raw:
            umbrella = Path(raw) / "arbitrary-umbrella-name"
            (umbrella / ".github" / "workflows").mkdir(parents=True)
            (umbrella / ".github" / "workflows" / "installed-package.yml").write_text(
                workflow_text, encoding="utf-8"
            )
            # The adapter passes an explicit --source-root; the validator only needs the
            # umbrella root itself to carry the layout markers.
            (umbrella / "pyproject.toml").write_text("", encoding="utf-8")
            (umbrella / "tests").mkdir()
            self.assertEqual(workflows.validate_topology(umbrella), [])


if __name__ == "__main__":
    unittest.main()
