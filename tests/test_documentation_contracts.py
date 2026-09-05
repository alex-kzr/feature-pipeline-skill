"""RMD-02: executable contract tests for DOC-01 documentation.

DOC-01 published the architecture, public-contracts, and migration-v3 pages. Its AC-3
required documentation examples to execute successfully in CI; that never happened before
this suite. These tests exercise, against real code rather than the doc's prose:

* the public-namespace import surface documented in ``docs/contracts/feature-pipeline.md``
  actually matches ``feature_pipeline.__all__``;
* the CLI example in that same doc actually parses and drives a real plan-only ``--dry-run``
  through :func:`pipeline_core.runner_cli.main`, using the doc's own literal anchors and
  relative-path text (only the anchor directories are substituted for a temporary fixture);
* the exit-code table in that doc matches the frozen ``EXIT_*`` constants it describes;
* the critical cross-links between the architecture, contracts, and migration pages resolve
  to real files (and, where a fragment is given, to a real heading in the target file).

If a future edit changes any of the documented commands, exports, exit codes, or links
without updating the implementation (or vice versa), this suite fails.
"""

from __future__ import annotations

import io
import json
import re
import shlex
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import runner_cli
from pipeline_core.execution import EXIT_BLOCKED, EXIT_ERROR, EXIT_GATE_PENDING, EXIT_OK

import feature_pipeline

DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"
ARCHITECTURE_DOC = DOCS_ROOT / "architecture" / "feature-pipeline.md"
CONTRACTS_DOC = DOCS_ROOT / "contracts" / "feature-pipeline.md"
MIGRATION_DOC = DOCS_ROOT / "migration" / "feature-pipeline-v3.md"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fenced_block(text: str, marker: str, lang: str) -> str:
    """Return the contents of the first ```<lang> fence after ``marker``."""
    start = text.index(marker)
    fence_open = f"```{lang}"
    fence_start = text.index(fence_open, start) + len(fence_open)
    fence_end = text.index("```", fence_start)
    return text[fence_start:fence_end].strip()


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _slugify_heading(heading: str) -> str:
    """A minimal GitHub-style heading slug: lowercase, spaces to '-', punctuation dropped."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug


def _headings(text: str) -> set[str]:
    return {
        _slugify_heading(line.lstrip("#").strip())
        for line in text.splitlines()
        if line.startswith("#")
    }


def _relative_links(text: str) -> list[str]:
    """Every markdown link target that is a relative filesystem reference, not a URL."""
    targets = re.findall(r"\]\(([^)]+)\)", text)
    return [
        target for target in targets
        if not target.startswith(("http://", "https://", "#"))
    ]


class PublicApiSurfaceTests(unittest.TestCase):
    """The `## Supported Python API` section of the contracts doc."""

    def test_documented_import_line_names_only_real_public_exports(self) -> None:
        text = _read(CONTRACTS_DOC)
        block = _fenced_block(text, "## Supported Python API", "python")
        import_line = next(
            line for line in block.splitlines() if line.startswith("from feature_pipeline import")
        )
        names = [n.strip() for n in import_line.split("import", 1)[1].split(",")]
        for name in names:
            self.assertIn(name, feature_pipeline.__all__,
                           f"{name!r} imported by the doc is not in feature_pipeline.__all__")
            self.assertTrue(hasattr(feature_pipeline, name),
                             f"{name!r} imported by the doc does not exist on feature_pipeline")

    def test_documented_export_list_matches_the_public_namespace(self) -> None:
        text = _read(CONTRACTS_DOC)
        sentence = re.search(
            r"The public namespace exports (.+?)\.\n", text, flags=re.DOTALL,
        )
        self.assertIsNotNone(sentence, "could not find the documented export-list sentence")
        documented = set(re.findall(r"`(\w+)`", sentence.group(1)))
        # `schemas` is called out separately as a compatibility shim, not part of this list.
        documented.discard("schemas")
        for name in documented:
            self.assertIn(name, feature_pipeline.__all__,
                           f"documented export {name!r} missing from feature_pipeline.__all__")


class CliExampleTests(unittest.TestCase):
    """The `## CLI` fenced example in the contracts doc is a real, runnable invocation."""

    def _cli_example_tokens(self) -> list[str]:
        text = _read(CONTRACTS_DOC)
        block = _fenced_block(
            text, "Every runnable invocation supplies three anchors", "text",
        )
        joined = " ".join(line.rstrip("\\").strip() for line in block.splitlines())
        return shlex.split(joined)

    def test_example_command_names_the_feature_pipeline_program(self) -> None:
        tokens = self._cli_example_tokens()
        self.assertEqual(tokens[0], "feature-pipeline")

    def test_example_command_drives_a_real_plan_only_dry_run(self) -> None:
        tokens = self._cli_example_tokens()
        self.assertIn("--dry-run", tokens)

        with TemporaryDirectory() as directory:
            project_root = Path(directory) / "project"
            agents_root = Path(directory) / "agents"
            core_root = project_root
            profile_rel = "tools/feature-pipeline/config/pipeline.profile.json"
            plan_rel = "docs/plans/example.json"

            profile_path = project_root / profile_rel
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(json.dumps({
                "version": 1,
                "name": "doc-example",
                "logical_paths": {"project": ".", "agents": ".", "core": "."},
                "role_grants": {
                    "executor": ["read", "write"],
                    "task_verifier": ["read"],
                    "test_verifier": ["read", "run_checks"],
                },
                "stages": [
                    {"name": "implement", "subagents": ["executor"], "argv": ["content", "apply"]},
                    {"name": "verification",
                     "subagents": ["task_verifier", "test_verifier"],
                     "argv": ["content", "check"]},
                ],
                "registry": {
                    "task_types": {
                        "docs": {
                            "stack": "text", "subagents": ["executor"], "root": "content",
                            "checks": ["lint"], "storage": "runs",
                        },
                    },
                    "stacks": {"text": {"runtime": "text"}},
                    "subagents": {"executor": {"grant": "executor"}},
                    "roots": {"content": "content"},
                    "checks": {"lint": ["lint", "run"]},
                    "storage": {"runs": ".pipeline/runs"},
                },
            }, indent=2), encoding="utf-8")

            plan_path = project_root / plan_rel
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps({
                "feature": "doc-example",
                "tasks": [{"id": "T-01", "type": "docs"}],
            }, indent=2), encoding="utf-8")

            substitutions = {
                "PROJECT": str(project_root),
                "AGENTS": str(agents_root),
                "CORE": str(core_root),
                profile_rel: profile_rel,
                plan_rel: plan_rel,
            }
            argv = [substitutions.get(tok, tok) for tok in tokens[1:]]

            code, out, err = _run_cli(argv)

        self.assertEqual(code, EXIT_GATE_PENDING, err)
        self.assertIn("C1.", out)
        self.assertIn(f"Exit code: {EXIT_GATE_PENDING}", out)


class ExitCodeTableTests(unittest.TestCase):
    """The `Exit ...` sentence in the contracts doc names the frozen exit-code constants."""

    def test_documented_exit_codes_match_the_frozen_constants(self) -> None:
        text = _read(CONTRACTS_DOC)
        self.assertEqual(EXIT_OK, 0)
        self.assertIn(f"Exit `{EXIT_OK}` is reserved for `--status`", text)
        self.assertIn(f"`{EXIT_GATE_PENDING}` means a delivery gate is pending", text)
        self.assertIn(f"`{EXIT_BLOCKED}` blocked", text)
        self.assertIn(f"`{EXIT_ERROR}` input/profile/routing error", text)


class CrossLinkTests(unittest.TestCase):
    """Critical relative links between the architecture, contracts, and migration pages."""

    def _assert_links_resolve(self, doc_path: Path) -> None:
        text = _read(doc_path)
        for target in _relative_links(text):
            path_part, _, fragment = target.partition("#")
            if not path_part:
                continue  # a same-file fragment only; not a cross-document link
            resolved = (doc_path.parent / path_part).resolve()
            self.assertTrue(
                resolved.is_file(),
                f"{doc_path} links to {target!r}, which does not resolve to a file",
            )
            if fragment:
                headings = _headings(_read(resolved))
                self.assertIn(
                    fragment, headings,
                    f"{doc_path} links to {target!r}, but {resolved} has no such heading",
                )

    def test_architecture_doc_links_resolve(self) -> None:
        self._assert_links_resolve(ARCHITECTURE_DOC)

    def test_contracts_doc_links_resolve(self) -> None:
        self._assert_links_resolve(CONTRACTS_DOC)

    def test_migration_doc_links_resolve(self) -> None:
        self._assert_links_resolve(MIGRATION_DOC)

    def test_architecture_doc_links_to_contracts_and_migration_pages(self) -> None:
        text = _read(ARCHITECTURE_DOC)
        self.assertIn("../contracts/feature-pipeline.md", text)
        self.assertIn("../migration/feature-pipeline-v3.md", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
