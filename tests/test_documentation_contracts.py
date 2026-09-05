"""Executable and repository-facing documentation contracts."""

from __future__ import annotations

import io
import json
import re
import shlex
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import runner_cli
from pipeline_core.execution import EXIT_BLOCKED, EXIT_ERROR, EXIT_GATE_PENDING, EXIT_OK

import feature_pipeline


ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = ROOT / "docs"
ARCHITECTURE_DOC = DOCS_ROOT / "architecture" / "feature-pipeline.md"
CONTRACTS_DOC = DOCS_ROOT / "contracts" / "feature-pipeline.md"
MIGRATION_DOC = DOCS_ROOT / "migration" / "feature-pipeline-v3.md"


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


if __name__ == "__main__":
    unittest.main()
