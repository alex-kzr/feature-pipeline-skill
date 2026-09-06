"""UGA-02 — contract for ``ci/gates.toml`` and its fail-closed loader.

``ci/gates.toml`` is the single executable source of truth for every CI gate and
the supported OS/Python matrix for each. The commands were previously duplicated
between workflow YAML and ``tests/README.md``; this manifest describes the
*existing* contract without changing thresholds, suite membership, or package
behaviour.

These tests pin:

* **AC-1** — all eight stable gate IDs, plus every command either workflow runs
  today (``quality-gates.yml`` and ``installed-package.yml``);
* **AC-2** — commands are ordered argv arrays with no shell-parsing dependency;
* **AC-3** — the loader rejects malformed schema, unsafe paths, invalid matrices,
  and duplicate or unknown references with deterministic ``ContractError``s;
* **AC-4** — matrices, suite membership, the coverage floor, and lint/type policy
  are unchanged from ``quality-gates.yml`` / ``installed-package.yml`` /
  ``pyproject.toml``.

Standard library only.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ci import contract

CORE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = CORE_ROOT / "ci" / "gates.toml"

# Copied verbatim from feature-pipeline-skill/.github/workflows/quality-gates.yml
# (and mirrored in tests/README.md): the gate argv this manifest must preserve.
CORE_WORKFLOW_COMMANDS: dict[str, list[list[str]]] = {
    "lint": [["uv", "run", "--with", "ruff", "ruff", "check", "."]],
    "types": [["uv", "run", "--with", "mypy", "mypy"]],
    "coverage": [
        ["uv", "run", "--with", "coverage", "coverage", "run", "-m", "unittest",
         "discover", "-s", "tests", "-t", "."],
        ["uv", "run", "--with", "coverage", "coverage", "report"],
    ],
    "platform": [[
        "uv", "run", "python", "-m", "unittest",
        "tests.test_process_runner", "tests.test_worktree",
        "tests.test_worktree_bounded_attribution",
    ]],
    "fault-injection": [[
        "uv", "run", "python", "-m", "unittest",
        "tests.test_concurrency", "tests.test_scope_gate",
        "tests.test_git_safety_allowlist", "tests.test_launch_controls",
        "tests.test_critical_behavior_characterization",
    ]],
    "performance": [[
        "uv", "run", "python", "-m", "unittest",
        "tests.test_worktree_performance_baseline",
    ]],
}

# Copied from .github/workflows/installed-package.yml, with the
# ``--python ${{ matrix.python }}`` template removed — the interpreter matrix is
# the explicit ``python`` field on the gate, not part of the stored argv.
CONSUMER_WORKFLOW_COMMANDS: dict[str, list[list[str]]] = {
    "installed-package": [
        ["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        ["uv", "run", "python", "-m", "unittest", "-v", "tests.test_installed_wheel"],
        ["uv", "run", "python", "-m", "tests.installed_wheel", "--json"],
    ],
}

# tests/README.md "documentation" suite — no workflow runs it yet, but the gate
# ID is contractual (UGA-02 Requirements).
DOCUMENTATION_COMMANDS: list[list[str]] = [
    ["uv", "run", "python", "-m", "unittest", "tests.test_documentation_contracts"],
]

_SHELL_METACHARACTERS = set("&|;<>`$()")


def _load_text(text: str) -> contract.Contract:
    with TemporaryDirectory() as raw:
        path = Path(raw) / "gates.toml"
        path.write_text(text, encoding="utf-8")
        return contract.load(path)


_GOOD_GATE = """
[[gates]]
id = "lint"
group = "core"
description = "Ruff lint + complexity policy"
commands = [{ argv = ["uv", "run", "ruff", "check", "."] }]
required_paths = ["pyproject.toml"]
"""


def _manifest(gate_block: str, *, schema: str = "schema_version = 1\n") -> str:
    return schema + gate_block


class ManifestContentTests(unittest.TestCase):
    """AC-1, AC-2, AC-4 — the committed manifest describes today's contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = contract.load()

    def test_manifest_file_exists(self) -> None:
        self.assertTrue(MANIFEST.is_file(), MANIFEST)

    def test_raw_manifest_declares_schema_version_1(self) -> None:
        data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)

    def test_all_eight_stable_gate_ids_present(self) -> None:
        self.assertEqual(
            set(self.contract.ids()),
            {
                "lint", "types", "coverage", "platform", "fault-injection",
                "performance", "installed-package", "documentation",
            },
        )
        self.assertEqual(len(self.contract.ids()), 8)

    def test_core_workflow_commands_are_preserved(self) -> None:
        for gate_id, expected in CORE_WORKFLOW_COMMANDS.items():
            with self.subTest(gate=gate_id):
                got = [list(cmd.argv) for cmd in self.contract.gate(gate_id).commands]
                self.assertEqual(got, expected)

    def test_consumer_workflow_commands_are_preserved(self) -> None:
        for gate_id, expected in CONSUMER_WORKFLOW_COMMANDS.items():
            with self.subTest(gate=gate_id):
                got = [list(cmd.argv) for cmd in self.contract.gate(gate_id).commands]
                self.assertEqual(got, expected)

    def test_documentation_gate_command(self) -> None:
        got = [list(cmd.argv) for cmd in self.contract.gate("documentation").commands]
        self.assertEqual(got, DOCUMENTATION_COMMANDS)

    def test_workflow_group_membership(self) -> None:
        core = {g.id for g in self.contract.group("core")}
        consumer = {g.id for g in self.contract.group("consumer")}
        self.assertEqual(
            core,
            {
                "lint", "types", "coverage", "platform", "fault-injection",
                "performance", "documentation",
            },
        )
        self.assertEqual(consumer, {"installed-package"})

    def test_commands_are_ordered_pure_argv_arrays(self) -> None:
        """AC-2: no shell operators, pipes, redirects, or substitutions."""
        for gate in self.contract.gates.values():
            self.assertGreater(len(gate.commands), 0, gate.id)
            for position, command in enumerate(gate.commands):
                with self.subTest(gate=gate.id, command=position):
                    self.assertIsInstance(command.argv, tuple)
                    self.assertGreater(len(command.argv), 0)
                    for token in command.argv:
                        self.assertIsInstance(token, str)
                        self.assertTrue(token)
                        self.assertFalse(
                            _SHELL_METACHARACTERS.intersection(token)
                            and token != ".",
                            token,
                        )

    def test_matrices_match_the_workflows(self) -> None:
        """AC-4: OS/Python matrices are unchanged."""
        for gate_id in ("platform", "fault-injection", "performance"):
            with self.subTest(gate=gate_id):
                self.assertEqual(
                    self.contract.gate(gate_id).os,
                    ("ubuntu-latest", "windows-latest"),
                )
        installed = self.contract.gate("installed-package")
        self.assertEqual(installed.os, ("ubuntu-latest", "windows-latest"))
        self.assertEqual(installed.python, ("3.11", "3.12", "3.13"))


class LoaderFailClosedTests(unittest.TestCase):
    """AC-3 — every validation rule rejects with a deterministic ContractError."""

    def test_default_manifest_loads(self) -> None:
        loaded = contract.load()
        self.assertEqual(loaded.schema_version, 1)

    def test_rejects_unknown_schema_version(self) -> None:
        with self.assertRaises(contract.ContractError) as caught:
            _load_text("schema_version = 2\n")
        self.assertIn("schema_version", str(caught.exception))

    def test_rejects_missing_schema_version(self) -> None:
        with self.assertRaises(contract.ContractError):
            _load_text(_GOOD_GATE)

    def test_rejects_duplicate_gate_id(self) -> None:
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(_GOOD_GATE + _GOOD_GATE))
        self.assertIn("duplicate", str(caught.exception))

    def test_rejects_unknown_gate_id(self) -> None:
        block = _GOOD_GATE.replace('id = "lint"', 'id = "bogus"')
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("unknown gate id", str(caught.exception))

    def test_rejects_missing_required_gate(self) -> None:
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(_GOOD_GATE))
        self.assertIn("missing required gate", str(caught.exception))

    def test_rejects_empty_argv(self) -> None:
        block = _GOOD_GATE.replace(
            'commands = [{ argv = ["uv", "run", "ruff", "check", "."] }]',
            "commands = [{ argv = [] }]",
        )
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("argv", str(caught.exception))

    def test_rejects_shell_operator_in_argv(self) -> None:
        block = _GOOD_GATE.replace(
            'commands = [{ argv = ["uv", "run", "ruff", "check", "."] }]',
            'commands = [{ argv = ["uv", "run", "ruff", "&&", "mypy"] }]',
        )
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("shell operator", str(caught.exception))

    def test_rejects_unsafe_required_path(self) -> None:
        block = _GOOD_GATE.replace(
            'required_paths = ["pyproject.toml"]',
            'required_paths = ["../outside/secrets"]',
        )
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("repository-relative", str(caught.exception))

    def test_rejects_absolute_required_path(self) -> None:
        block = _GOOD_GATE.replace(
            'required_paths = ["pyproject.toml"]',
            'required_paths = ["/etc/passwd"]',
        )
        with self.assertRaises(contract.ContractError):
            _load_text(_manifest(block))

    def test_rejects_invalid_os_matrix(self) -> None:
        block = _GOOD_GATE + 'os = ["plan9-latest"]\n'
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("OS", str(caught.exception))

    def test_rejects_invalid_python_matrix(self) -> None:
        block = _GOOD_GATE + 'python = ["2.7"]\n'
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("Python", str(caught.exception))

    def test_rejects_unknown_gate_key(self) -> None:
        block = _GOOD_GATE + 'surprise = true\n'
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("unknown key", str(caught.exception))

    def test_rejects_invalid_workflow_group(self) -> None:
        block = _GOOD_GATE.replace('group = "core"', 'group = "middleware"')
        with self.assertRaises(contract.ContractError) as caught:
            _load_text(_manifest(block))
        self.assertIn("group", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
