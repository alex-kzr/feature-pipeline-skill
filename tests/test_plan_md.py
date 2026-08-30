"""Tests for the core CLI's Markdown-plan reader (`pipeline_core.plan_md`) and its wiring
into `runner_cli` (CR-05).

Two layers:
* unit tests on `load_markdown_plan` — the task-graph it extracts and every fail-closed case;
* an equivalence test through `runner_cli.main` — a Markdown plan and a JSON plan over the
  same graph produce the same C1–C8 dry-run and the same exit code.
"""

from __future__ import annotations

import io
import json
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import runner_cli
from pipeline_core.plan_md import MarkdownPlanError, load_markdown_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

MINIMAL_REGISTRY = {
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

MAIN_PLAN_MD = """\
# Feature: Sample

## Tasks

| ID | Title | Type | Depends on |
|---|---|---|---|
| SF-01 | Write the base note | docs | (none) |
| SF-02 | Extend the base note | docs | SF-01 |

## Phase 1

### SF-01 Write the base note
→ [tasks/SF-01_base-note.md](tasks/SF-01_base-note.md)
"""


def _write(tmp: Path, name: str, body: str) -> Path:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


class LoadMarkdownPlanTests(unittest.TestCase):
    def test_reads_the_task_graph_and_the_one_dependency_edge(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "2026-08-29-shared-project.md", MAIN_PLAN_MD)
            feature, tasks = load_markdown_plan(path)
        self.assertEqual(feature, "shared-project")  # date prefix stripped from the stem
        self.assertEqual(
            tasks,
            [
                {"id": "SF-01", "type": "docs", "depends_on": []},
                {"id": "SF-02", "type": "docs", "depends_on": ["SF-01"]},
            ],
        )

    def test_column_order_is_free_and_headers_are_case_insensitive(self) -> None:
        body = (
            "| Depends On | TYPE | notes | id |\n"
            "|---|---|---|---|\n"
            "| - | docs | x | A-01 |\n"
            "| A-01, B-02 | research | y | C-03 |\n"
            "| (none) | docs | z | B-02 |\n"
        )
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "plan.md", body)
            _feature, tasks = load_markdown_plan(path)
        by_id = {t["id"]: t for t in tasks}
        self.assertEqual(by_id["A-01"]["depends_on"], [])
        self.assertEqual(by_id["C-03"]["depends_on"], ["A-01", "B-02"])
        self.assertEqual(by_id["C-03"]["type"], "research")

    def test_id_cell_tolerates_backticks_and_a_markdown_link(self) -> None:
        body = (
            "| ID | Type | Depends on |\n|---|---|---|\n"
            "| `T-01` | docs | (none) |\n"
            "| [T-02](tasks/T-02.md) | docs | `T-01` |\n"
        )
        with TemporaryDirectory() as raw:
            _feature, tasks = load_markdown_plan(_write(Path(raw), "plan.md", body))
        self.assertEqual([t["id"] for t in tasks], ["T-01", "T-02"])
        self.assertEqual(tasks[1]["depends_on"], ["T-01"])

    def test_feature_falls_back_to_a_slug_of_the_feature_heading(self) -> None:
        body = "# Feature: My Cool Thing\n\n| ID | Type | Depends on |\n|---|---|---|\n| A-01 | docs | - |\n"
        with TemporaryDirectory() as raw:
            # a stem with no date prefix and nothing to strip still wins if non-empty;
            # use a stem that is only a date so the heading slug is exercised
            feature, _tasks = load_markdown_plan(_write(Path(raw), "2026-08-29-.md", body))
        self.assertEqual(feature, "my-cool-thing")

    def test_no_task_table_and_no_headings_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "plan.md", "# Feature: X\n\nno table here\n")
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(path)


class HeadingsAndTaskFilesTests(unittest.TestCase):
    """Convention 2: '### <ID>' headings + tasks/<ID>_*.md metadata (the Oxidium plan shape)."""

    PLAN = """\
# Feature: Doc Thing

## Phase 7: Notes (DA-01 to DA-02)
**Execution:** separate

---

## Phase 7 — Notes

### DA-01 Provide the base note
→ [DA-01_base.md](tasks/DA-01_base.md)

### DA-02 Extend it
→ [DA-02_extend.md](tasks/DA-02_extend.md)
"""

    def _task_file(self, body_type: str, depends: str) -> str:
        return (
            "# DA-0X - x\n\n## Status\n- [ ] To Do\n\n## Execution Metadata\n"
            f"- Type: {body_type}\n- Executor: docs-maintainer\n- Depends on: {depends}\n"
            "- Allowed scope: `x`\n- Out of scope: none\n- Required skills: none\n"
            "- Documentation impact: none\n\n## Purpose\nx\n"
        )

    def test_reads_ids_from_headings_and_type_deps_from_task_files(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tasks").mkdir()
            (root / "2026-08-23-doc-thing.md").write_text(self.PLAN, encoding="utf-8")
            (root / "tasks" / "DA-01_base.md").write_text(
                self._task_file("docs", "none"), encoding="utf-8")
            (root / "tasks" / "DA-02_extend.md").write_text(
                self._task_file("research", "DA-01"), encoding="utf-8")
            feature, tasks = load_markdown_plan(root / "2026-08-23-doc-thing.md")
        self.assertEqual(feature, "doc-thing")
        self.assertEqual(tasks, [
            {"id": "DA-01", "type": "docs", "depends_on": []},
            {"id": "DA-02", "type": "research", "depends_on": ["DA-01"]},
        ])

    def test_missing_task_file_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tasks").mkdir()
            (root / "plan.md").write_text(self.PLAN, encoding="utf-8")
            (root / "tasks" / "DA-01_base.md").write_text(
                self._task_file("docs", "none"), encoding="utf-8")
            # DA-02 file is absent
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(root / "plan.md")

    def test_task_file_without_a_type_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tasks").mkdir()
            (root / "plan.md").write_text(self.PLAN, encoding="utf-8")
            for name in ("DA-01_base.md", "DA-02_extend.md"):
                (root / "tasks" / name).write_text(
                    "## Execution Metadata\n- Executor: docs-maintainer\n- Depends on: none\n",
                    encoding="utf-8")
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(root / "plan.md")

    def test_a_table_wins_when_the_plan_has_both_a_table_and_headings(self) -> None:
        # the shared-fixture main plan shape: a Tasks table AND ### headings + task files.
        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tasks").mkdir()
            (root / "plan.md").write_text(MAIN_PLAN_MD, encoding="utf-8")
            # no tasks/ files written — proving the table path is taken, not the file path
            _feature, tasks = load_markdown_plan(root / "plan.md")
        self.assertEqual([t["id"] for t in tasks], ["SF-01", "SF-02"])

    def test_missing_type_column_fails_closed(self) -> None:
        body = "| ID | Title | Depends on |\n|---|---|---|\n| A-01 | t | - |\n"
        with TemporaryDirectory() as raw:
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(_write(Path(raw), "plan.md", body))

    def test_row_missing_an_id_or_type_fails_closed(self) -> None:
        body = "| ID | Type | Depends on |\n|---|---|---|\n| A-01 |  | - |\n"
        with TemporaryDirectory() as raw:
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(_write(Path(raw), "plan.md", body))

    def test_duplicate_task_id_fails_closed(self) -> None:
        body = "| ID | Type | Depends on |\n|---|---|---|\n| A-01 | docs | - |\n| A-01 | docs | - |\n"
        with TemporaryDirectory() as raw:
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(_write(Path(raw), "plan.md", body))

    def test_unknown_dependency_fails_closed(self) -> None:
        body = "| ID | Type | Depends on |\n|---|---|---|\n| A-01 | docs | Z-99 |\n"
        with TemporaryDirectory() as raw:
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(_write(Path(raw), "plan.md", body))

    def test_self_dependency_fails_closed(self) -> None:
        body = "| ID | Type | Depends on |\n|---|---|---|\n| A-01 | docs | A-01 |\n"
        with TemporaryDirectory() as raw:
            with self.assertRaises(MarkdownPlanError):
                load_markdown_plan(_write(Path(raw), "plan.md", body))


def _seed(dest: Path) -> dict:
    shutil.copytree(FIXTURES / "library-guide", dest, dirs_exist_ok=True)
    profile_path = dest / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["registry"] = json.loads(json.dumps(MINIMAL_REGISTRY))
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    agents_root = dest / "_agents"
    (agents_root / "skills").mkdir(parents=True, exist_ok=True)
    return {
        "anchors": [
            "--project-root", str(dest),
            "--agents-root", str(agents_root),
            "--core-root", str(dest),
        ],
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _strip_anchor_lines(text: str) -> str:
    # the profile-relative --plan path is echoed under "anchors:"; drop it so a .md and a
    # .json plan over the same graph compare equal on everything the run actually plans.
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("profile: <project_root>/")
    )


class MarkdownJsonEquivalenceTests(unittest.TestCase):
    def test_markdown_and_json_plans_over_one_graph_give_the_same_dry_run(self) -> None:
        with TemporaryDirectory() as raw:
            dest = Path(raw) / "project"
            seed = _seed(dest)
            (dest / "plan.md").write_text(MAIN_PLAN_MD, encoding="utf-8")
            (dest / "plan.json").write_text(
                json.dumps({
                    "feature": "shared-project",
                    "tasks": [
                        {"id": "SF-01", "type": "docs"},
                        {"id": "SF-02", "type": "docs", "depends_on": ["SF-01"]},
                    ],
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            common = seed["anchors"] + ["--profile", "profile.json",
                                        "--dry-run", "--mode", "plan-only"]
            md_code, md_out, _ = _run(common + ["--plan", "plan.md"])
            js_code, js_out, _ = _run(common + ["--plan", "plan.json"])

        self.assertEqual(md_code, 10)
        self.assertEqual(js_code, 10)
        self.assertEqual(_strip_anchor_lines(md_out), _strip_anchor_lines(js_out))
        for marker in ("C1.", "C2.", "C3.", "C4.", "C5.", "C6.", "C7.", "C8."):
            self.assertIn(marker, md_out)

    def test_markdown_plan_unmet_dependency_fails_closed_like_json(self) -> None:
        with TemporaryDirectory() as raw:
            dest = Path(raw) / "project"
            seed = _seed(dest)
            (dest / "plan.md").write_text(MAIN_PLAN_MD, encoding="utf-8")
            code, out, _ = _run(seed["anchors"] + [
                "--profile", "profile.json", "--dry-run", "--mode", "plan-only",
                "--plan", "plan.md", "--task", "SF-02",
            ])
        self.assertEqual(code, 20)
        self.assertIn("dependency-not-satisfied", out)

    def test_malformed_markdown_plan_reaches_the_cli_as_exit_30(self) -> None:
        with TemporaryDirectory() as raw:
            dest = Path(raw) / "project"
            seed = _seed(dest)
            (dest / "plan.md").write_text("# Feature: X\n\nno table\n", encoding="utf-8")
            code, _out, err = _run(seed["anchors"] + [
                "--profile", "profile.json", "--dry-run", "--mode", "plan-only",
                "--plan", "plan.md",
            ])
        self.assertEqual(code, 30)
        self.assertIn("no tasks found", err)


if __name__ == "__main__":
    unittest.main()
