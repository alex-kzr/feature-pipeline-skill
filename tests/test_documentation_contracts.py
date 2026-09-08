"""Executable and repository-facing documentation contracts."""

from __future__ import annotations

import io
import json
import re
import shlex
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import runner_cli
from pipeline_core.execution import EXIT_BLOCKED, EXIT_ERROR, EXIT_GATE_PENDING, EXIT_OK

from ci import contract as ci_contract
from ci import promotion as ci_promotion

import feature_pipeline

from tests._umbrella import require_umbrella, umbrella_root


def setUpModule() -> None:
    # Every assertion below reads a file that ships only in the umbrella working tree
    # (docs/**, ci/gates.toml is core-local but the docs it is compared against are not).
    require_umbrella("docs/architecture|contracts|migration|validation/*.md")


ROOT = umbrella_root()
DOCS_ROOT = ROOT / "docs"
ARCHITECTURE_DOC = DOCS_ROOT / "architecture" / "feature-pipeline.md"
CONTRACTS_DOC = DOCS_ROOT / "contracts" / "feature-pipeline.md"
MIGRATION_DOC = DOCS_ROOT / "migration" / "feature-pipeline-v3.md"

CORE_ROOT = ROOT / "feature-pipeline-skill"
GATES_MANIFEST = CORE_ROOT / "ci" / "gates.toml"
TESTS_README = CORE_ROOT / "tests" / "README.md"
UNIVERSAL_CI_DOC = DOCS_ROOT / "validation" / "github-actions-universal-solution.md"
QUALITY_GATES_DOC = DOCS_ROOT / "validation" / "quality-gates.md"
INSTALLED_PACKAGE_DOC = DOCS_ROOT / "validation" / "installed-package.md"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _fenced_block(text: str, marker: str, lang: str) -> str:
    start = text.index(marker)
    fence_start = text.index(f"```{lang}", start) + len(lang) + 3
    return text[fence_start:text.index("```", fence_start)].strip()


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _heading_slugs(text: str) -> set[str]:
    return {
        re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", line.lstrip("#").strip().lower()))
        for line in text.splitlines() if line.startswith("#")
    }


def _section(text: str, heading: str) -> str:
    """Return a level-two Markdown section, excluding its heading."""

    match = re.search(
        rf"^## {re.escape(heading)}\r?$(?:\r?\n)(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing section: {heading}")
    return match.group(1)


def _table_rows(text: str, heading: str) -> list[list[str]]:
    """Parse the first Markdown table in a named level-two section."""

    lines = _section(text, heading).splitlines()
    table = [line for line in lines if line.startswith("|")]
    if len(table) < 2:
        raise AssertionError(f"missing table in section: {heading}")
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in table[2:]
    ]


def _table_after_marker(text: str, marker: str) -> list[list[str]]:
    """Parse the first Markdown table following an unambiguous document marker."""

    lines = text[text.index(marker):].splitlines()
    table_start = next(index for index, line in enumerate(lines) if line.startswith("|"))
    table: list[str] = []
    for line in lines[table_start:]:
        if not line.startswith("|"):
            break
        table.append(line)
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in table[2:]
    ]


class PublicApiSurfaceTests(unittest.TestCase):
    def test_documented_import_line_names_only_real_public_exports(self) -> None:
        block = _fenced_block(_text("docs/contracts/feature-pipeline.md"), "## Supported Python API", "python")
        import_line = next(line for line in block.splitlines() if line.startswith("from feature_pipeline import"))
        for name in (value.strip() for value in import_line.split("import", 1)[1].split(",")):
            with self.subTest(name=name):
                self.assertIn(name, feature_pipeline.__all__)
                self.assertTrue(hasattr(feature_pipeline, name))

    def test_documented_export_list_matches_the_public_namespace(self) -> None:
        text = _text("docs/contracts/feature-pipeline.md")
        match = re.search(r"The public namespace exports (.+?)\.\n", text, flags=re.DOTALL)
        self.assertIsNotNone(match)
        documented = set(re.findall(r"`(\w+)`", match.group(1)))
        documented.discard("schemas")
        for name in documented:
            self.assertIn(name, feature_pipeline.__all__)


class CliExampleTests(unittest.TestCase):
    def _tokens(self) -> list[str]:
        block = _fenced_block(_text("docs/contracts/feature-pipeline.md"), "Every runnable invocation supplies three anchors", "text")
        return shlex.split(" ".join(line.rstrip("\\").strip() for line in block.splitlines()))

    def test_example_command_names_the_feature_pipeline_program(self) -> None:
        self.assertEqual(self._tokens()[0], "feature-pipeline")

    def test_example_command_drives_a_real_plan_only_dry_run(self) -> None:
        tokens = self._tokens()
        self.assertIn("--dry-run", tokens)
        with TemporaryDirectory() as directory:
            project_root = Path(directory) / "project"
            agents_root = Path(directory) / "agents"
            profile_rel = "tools/feature-pipeline/config/pipeline.profile.json"
            plan_rel = "docs/plans/example.json"
            profile = project_root / profile_rel
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_text(json.dumps({"version": 1, "name": "doc-example", "logical_paths": {"project": ".", "agents": ".", "core": "."}, "role_grants": {"executor": ["read", "write"], "task_verifier": ["read"], "test_verifier": ["read", "run_checks"]}, "stages": [{"name": "implement", "subagents": ["executor"], "argv": ["content", "apply"]}, {"name": "verification", "subagents": ["task_verifier", "test_verifier"], "argv": ["content", "check"]}], "registry": {"task_types": {"docs": {"stack": "text", "subagents": ["executor"], "root": "content", "checks": ["lint"], "storage": "runs"}}, "stacks": {"text": {"runtime": "text"}}, "subagents": {"executor": {"grant": "executor"}}, "roots": {"content": "content"}, "checks": {"lint": ["lint", "run"]}, "storage": {"runs": ".pipeline/runs"}}}), encoding="utf-8")
            plan = project_root / plan_rel
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(json.dumps({"feature": "doc-example", "tasks": [{"id": "T-01", "type": "docs"}]}), encoding="utf-8")
            substitutions = {"PROJECT": str(project_root), "AGENTS": str(agents_root), "CORE": str(project_root), profile_rel: profile_rel, plan_rel: plan_rel}
            code, out, err = _run_cli([substitutions.get(token, token) for token in tokens[1:]])
        self.assertEqual(code, EXIT_GATE_PENDING, err)
        self.assertIn("C1.", out)
        self.assertIn(f"Exit code: {EXIT_GATE_PENDING}", out)


class ExitCodeTableTests(unittest.TestCase):
    def test_documented_exit_codes_match_the_frozen_constants(self) -> None:
        text = _text("docs/contracts/feature-pipeline.md")
        self.assertEqual(EXIT_OK, 0)
        self.assertIn(f"Exit `{EXIT_OK}` is reserved for `--status`", text)
        self.assertIn(f"`{EXIT_GATE_PENDING}` means a delivery gate is pending", text)
        self.assertIn(f"`{EXIT_BLOCKED}` blocked", text)
        self.assertIn(f"`{EXIT_ERROR}` input/profile/routing error", text)


class CrossLinkTests(unittest.TestCase):
    def _assert_links_resolve(self, doc: Path) -> None:
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#")):
                continue
            path_part, _, fragment = target.partition("#")
            resolved = (doc.parent / path_part).resolve()
            self.assertTrue(resolved.is_file(), f"{doc} links to {target!r}, which does not resolve")
            if fragment:
                self.assertIn(fragment, _heading_slugs(resolved.read_text(encoding="utf-8")))

    def test_critical_links_resolve(self) -> None:
        for doc in (ARCHITECTURE_DOC, CONTRACTS_DOC, MIGRATION_DOC):
            with self.subTest(doc=doc):
                self._assert_links_resolve(doc)

    def test_architecture_links_to_contracts_and_migration(self) -> None:
        text = ARCHITECTURE_DOC.read_text(encoding="utf-8")
        self.assertIn("../contracts/feature-pipeline.md", text)
        self.assertIn("../migration/feature-pipeline-v3.md", text)


class DocumentationContractTests(unittest.TestCase):
    def test_runtime_contract_mentions_reuse_scope_protocol_and_recovery(self) -> None:
        text = _text("docs/contracts/feature-pipeline.md") + _text("docs/architecture/feature-pipeline.md")
        for token in (
            "--verify-dependency-chain", "--attest-dependency", "execution_scope",
            "reused_verification", "evidence-legacy-ambiguous", "source run bytes",
            "result-protocol-invalid", "launch-failure-<N>.json", "turn.completed",
            "durable-first", "canonical task-local state", "real filesystem failure",
            "KLC-03", "KLC-02", "klc-current-plan",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_metadata_contract_limits_globs_to_documentation_impact(self) -> None:
        text = _text("docs/agents/task-metadata-contract.md")
        for token in (
            "Documentation impact", "docs/agents/**", "docs/*.md", "docs/guide?.md",
            "/docs/x", "C:/docs/x", "~/docs/x", "docs/../x", "docs\\\\x",
            "Required skills", "command `cwd`", "execution-boundary validator",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_docs_preserve_verifier_approval_commit_and_no_push_boundaries(self) -> None:
        text = _text("docs/agents/README.md") + _text("docs/agents/task-metadata-contract.md")
        for token in ("cannot verify its own work", "plan approval", "final-diff approval", "commit controls", "no-push"):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_cli_guides_describe_the_rendered_chain_controls(self) -> None:
        text = _text("feature-pipeline-skill/scripts/README.md") + _text("docs/agents/README.md")
        for token in (
            "dependency verification chain", "execution scope", "reused sources",
            "planned dispatch set", "--verify-dependency-chain", "--attest-dependency",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_explicit_attestation_uses_cross_run_evidence_eligibility(self) -> None:
        text = _text("feature-pipeline-skill/scripts/README.md")
        for token in (
            "same verified-reuse eligibility policy", "task path and task-contract digest",
            "both verifier verdicts", "ineligible-evidence diagnostic",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)
        self.assertNotIn("attestation-source-identity-mismatch", text)
        self.assertNotIn("prompt_path`/`plan_path` match this run", text)


class UniversalCiContractDocumentationTests(unittest.TestCase):
    """UGA-08 - the CI operating contract is documentation checked against executable data.

    Every documented gate ID, command, matrix cell, and required-check identity is compared,
    mechanically, to the loaded ``ci/gates.toml`` and to ``ci.promotion.required_core_checks``,
    so copying a value into prose without updating the manifest is a red test, not silent drift
    (UGA-08 AC-1, AC-3). The recovery guidance keeps single-cause diagnostics and the permanent
    no-push rule (AC-4).
    """

    def setUp(self) -> None:
        self.contract = ci_contract.load(GATES_MANIFEST)
        self.readme = TESTS_README.read_text(encoding="utf-8")
        self.solution = UNIVERSAL_CI_DOC.read_text(encoding="utf-8")
        self.quality_gates = QUALITY_GATES_DOC.read_text(encoding="utf-8")
        self.installed_package = INSTALLED_PACKAGE_DOC.read_text(encoding="utf-8")

    def _expected_gate_rows(self, gates: tuple[ci_contract.Gate, ...]) -> set[tuple[str, ...]]:
        return {
            (
                gate.id,
                gate.group,
                ", ".join(gate.os) if gate.os else "—",
                ", ".join(gate.python) if gate.python else "—",
                "; ".join(" ".join(command.argv) for command in gate.commands),
            )
            for gate in gates
        }

    def _documented_gate_rows(self, text: str, heading: str) -> set[tuple[str, ...]]:
        rows = _table_rows(text, heading)
        return {
            (
                row[0].strip("`"),
                row[1],
                row[2],
                row[3],
                "; ".join(re.findall(r"`([^`]+)`", row[4])),
            )
            for row in rows
        }

    def test_ci_gate_tables_are_exact_manifest_transcriptions(self) -> None:
        cases = (
            (self.readme, "CI gate contract (`ci/gates.toml`)", tuple(self.contract.gates.values())),
            (self.quality_gates, "Current producer gate contract", self.contract.group("core")),
            (self.installed_package, "Current consumer gate contract", self.contract.group("consumer")),
        )
        for text, heading, gates in cases:
            with self.subTest(heading=heading):
                self.assertEqual(
                    self._documented_gate_rows(text, heading),
                    self._expected_gate_rows(gates),
                )

    def test_installed_package_supporting_tables_match_the_consumer_gate(self) -> None:
        """Keep the consumer proof's explanatory commands and matrix in lockstep."""

        gate = self.contract.gate("installed-package")
        tested_rows = _table_rows(self.installed_package, "What is tested")
        self.assertEqual(
            [re.findall(r"`([^`]+)`", row[1]) for row in tested_rows],
            [
                [" ".join(gate.commands[0].argv)],
                [" ".join(gate.commands[1].argv)],
            ],
        )

        supported_rows = _table_rows(self.installed_package, "Supported version matrix")
        self.assertEqual(
            {(row[0].strip("`"), row[1]) for row in supported_rows},
            {(image, ", ".join(gate.python)) for image in gate.os},
        )

    def test_docs_name_the_manifest_as_the_only_command_authority(self) -> None:
        for text in (self.readme, self.solution):
            self.assertIn("ci/gates.toml", text)
            self.assertIn("the only command/matrix authority", text)

    def test_solution_doc_exactly_transcribes_required_check_identities(self) -> None:
        required = ci_promotion.required_check_contract(self.contract)
        producer = {
            row[0].strip("`")
            for row in _table_after_marker(self.solution, "### Producer")
        }
        self.assertEqual(producer, set(required.producer))
        consumer = {
            row[0].strip("`")
            for row in _table_after_marker(self.solution, "### Consumer and promotion")
        }
        self.assertEqual(consumer, set((*required.consumer, required.promotion)))
        self.assertIn(ci_promotion.CONSUMER_CHECK_IDENTITY, self.solution)
        self.assertIn(required.promotion, self.solution)

    def test_promotion_identity_drift_is_detected(self) -> None:
        required = ci_promotion.required_check_contract(self.contract)
        documented = {
            row[0].strip("`")
            for row in _table_after_marker(self.solution, "### Consumer and promotion")
        }
        drifted = replace(required, promotion="renamed promotion check")
        self.assertNotEqual(documented, set((*drifted.consumer, drifted.promotion)))

    def test_solution_doc_covers_the_operating_contract_and_no_push_recovery(self) -> None:
        for token in (
            "--source-root",
            "--expected-source-sha",
            "resolved source root",
            "producer",
            "consumer",
            "promotion",
            "single cause",
            "single-cause diagnostics",
            "Stuck-run recovery",
            "no-push rule is permanent",
            "human",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.solution)

    def test_solution_doc_links_and_fragments_resolve(self) -> None:
        doc = UNIVERSAL_CI_DOC
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#")):
                continue
            path_part, _, fragment = target.partition("#")
            resolved = (doc.parent / path_part).resolve()
            with self.subTest(target=target):
                self.assertTrue(resolved.is_file(), f"{target} does not resolve")
                if fragment:
                    self.assertIn(
                        fragment, _heading_slugs(resolved.read_text(encoding="utf-8"))
                    )


if __name__ == "__main__":
    unittest.main()
