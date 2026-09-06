"""UGA-03 — the explicit-root CI driver (``ci/run.py`` + ``ci/runner.py``).

The driver makes the source checkout an explicit input and removes every
ambient current-directory assumption from gate execution. These tests pin the
six vertical slices from the task's Implementation Notes:

* required-root rejection — a missing, nonexistent, non-directory, or
  contract-less ``--source-root`` fails deterministically;
* valid root / preflight — the resolved root, source SHA, gate ID, and
  required paths are printed before the first gate command;
* deterministic listing — ``list --json`` is byte-stable for one manifest and
  group (AC-5);
* single-command execution — one gate command runs via argv with an explicit
  ``cwd`` and no shell (AC-1, AC-2);
* multi-command short-circuit — the first nonzero exit code is returned and no
  later command or evidence producer runs (AC-4);
* SHA mismatch — ``--expected-source-sha`` that differs from the checked-out
  source SHA fails before any gate subprocess starts (AC-3).

Standard library only. Gate subprocesses are replaced by an injected fake
runner; the real quality suite is never executed here.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ci import contract, run as run_cli, runner

CORE_ROOT = Path(__file__).resolve().parents[1]
REAL_MANIFEST = CORE_ROOT / "ci" / "gates.toml"

# Every ``required_paths`` entry named by any gate in the committed manifest.
REQUIRED_FIXTURE_PATHS = (
    "pyproject.toml",
    "tests/README.md",
    "tests/test_documentation_contracts.py",
    "tests/installed_wheel.py",
)

_FIXED_SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRunner:
    """Records every argv/cwd and returns scripted results for gate commands.

    ``git rev-parse HEAD`` is answered from ``sha``; all other commands consume
    ``results`` in order (defaulting to exit 0).
    """

    def __init__(self, *, sha: str = _FIXED_SHA, results: list[runner.CommandResult] | None = None):
        self.sha = sha
        self.results = list(results or [])
        self.calls: list[tuple[list[str], Path, bool]] = []

    def __call__(self, argv, *, cwd, capture: bool = False) -> runner.CommandResult:
        argv = list(argv)
        self.calls.append((argv, Path(cwd), capture))
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return runner.CommandResult(returncode=0, stdout=self.sha + "\n")
        if self.results:
            return self.results.pop(0)
        return runner.CommandResult(returncode=0, stdout="")

    @property
    def gate_calls(self) -> list[tuple[list[str], Path, bool]]:
        return [call for call in self.calls if call[0][:3] != ["git", "rev-parse", "HEAD"]]


def _make_root(parent: Path, name: str = "core") -> Path:
    """A synthetic source checkout: real manifest + every gate's required paths."""

    root = parent / name
    (root / "ci").mkdir(parents=True)
    shutil.copyfile(REAL_MANIFEST, root / "ci" / "gates.toml")
    for rel in REQUIRED_FIXTURE_PATHS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return root


class RequiredRootRejectionTests(unittest.TestCase):
    """Slice 1 — an explicit, valid root is mandatory; no parent/child search."""

    def test_missing_source_root_is_rejected(self) -> None:
        with self.assertRaises(runner.DriverError):
            runner.resolve_source_root(None)

    def test_empty_source_root_is_rejected(self) -> None:
        with self.assertRaises(runner.DriverError):
            runner.resolve_source_root("")

    def test_nonexistent_source_root_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            with self.assertRaises(runner.DriverError):
                runner.resolve_source_root(str(Path(raw) / "nope"))

    def test_non_directory_source_root_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            afile = Path(raw) / "a-file"
            afile.write_text("", encoding="utf-8")
            with self.assertRaises(runner.DriverError):
                runner.resolve_source_root(str(afile))

    def test_contract_less_source_root_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            plain = Path(raw) / "plain"
            plain.mkdir()
            with self.assertRaises(runner.DriverError):
                runner.resolve_source_root(str(plain))

    def test_valid_root_resolves_absolute(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            resolved = runner.resolve_source_root(str(root))
            self.assertTrue(resolved.is_absolute())
            self.assertEqual(resolved, root.resolve())


class PreflightTests(unittest.TestCase):
    """Slice 2 — diagnostics are printed before the first gate command."""

    def test_preflight_prints_root_sha_gate_and_required_paths(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner(sha=_FIXED_SHA)
            out = io.StringIO()
            code = runner.run_gate(
                gate_id="coverage",
                source_root=str(root),
                runner=fake,
                out=out,
                source_repo="alex-kzr/feature-pipeline-skill",
                workflow_repo="alex-kzr/feature-pipeline-skill",
            )
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn(str(root.resolve()), text)
            self.assertIn(_FIXED_SHA, text)
            self.assertIn("coverage", text)
            self.assertIn("tests/README.md", text)
            self.assertIn("alex-kzr/feature-pipeline-skill", text)

    def test_workflow_sha_line_only_when_supplied(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            without = io.StringIO()
            runner.run_gate(
                gate_id="lint", source_root=str(root), runner=FakeRunner(), out=without
            )
            self.assertNotIn("workflow SHA", without.getvalue())

            with_sha = io.StringIO()
            runner.run_gate(
                gate_id="lint",
                source_root=str(root),
                runner=FakeRunner(),
                out=with_sha,
                workflow_sha="deadbeef",
            )
            self.assertIn("deadbeef", with_sha.getvalue())

    def test_preflight_prints_before_any_gate_command(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            out = io.StringIO()
            runner.run_gate(
                gate_id="lint", source_root=str(root), runner=FakeRunner(), out=out
            )
            text = out.getvalue()
            self.assertLess(text.index("source SHA"), text.index("ruff"))


class DeterministicListingTests(unittest.TestCase):
    """Slice 3 / AC-5 — ``list --json`` is byte-stable for one manifest + group."""

    def test_list_json_is_byte_stable(self) -> None:
        loaded = contract.load(REAL_MANIFEST)
        first = runner.list_json(loaded, "core")
        second = runner.list_json(loaded, "core")
        self.assertEqual(first, second)

    def test_list_json_is_stable_across_independent_loads(self) -> None:
        a = runner.list_json(contract.load(REAL_MANIFEST), "consumer")
        b = runner.list_json(contract.load(REAL_MANIFEST), "consumer")
        self.assertEqual(a, b)

    def test_list_json_is_valid_json_for_the_named_group(self) -> None:
        payload = json.loads(runner.list_json(contract.load(REAL_MANIFEST), "core"))
        self.assertIn("core", payload)
        ids = [gate["id"] for gate in payload["core"]]
        self.assertEqual(
            set(ids),
            {"lint", "types", "coverage", "platform", "fault-injection",
             "performance", "documentation"},
        )

    def test_cli_list_json_matches_helper(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            out = io.StringIO()
            code = run_cli.main(
                ["list", "--json", "--group", "core", "--source-root", str(root)],
                runner=FakeRunner(),
                stdout=out,
            )
            self.assertEqual(code, 0)
            expected = runner.list_json(contract.load(root / "ci" / "gates.toml"), "core")
            self.assertEqual(out.getvalue(), expected)


class SingleCommandExecutionTests(unittest.TestCase):
    """Slice 4 / AC-1, AC-2 — argv + explicit cwd, no shell, cwd-independent."""

    def test_single_command_runs_with_explicit_cwd_and_argv(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner()
            code = runner.run_gate(
                gate_id="lint", source_root=str(root), runner=fake, out=io.StringIO()
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(fake.gate_calls), 1)
            argv, cwd, capture = fake.gate_calls[0]
            self.assertEqual(argv, ["uv", "run", "--with", "ruff", "ruff", "check", "."])
            self.assertEqual(cwd, root.resolve())
            self.assertFalse(capture)

    def test_same_gate_invocation_from_standalone_and_nested_roots(self) -> None:
        """AC-1: arbitrary directory names, identical executed argv."""

        with TemporaryDirectory() as raw:
            standalone = _make_root(Path(raw), name="checkout-7f3a")
            nested_parent = Path(raw) / "umbrella" / "vendor dir"
            nested_parent.mkdir(parents=True)
            nested = _make_root(nested_parent, name="feature-pipeline-skill")

            runs = []
            for root in (standalone, nested):
                fake = FakeRunner()
                runner.run_gate(
                    gate_id="performance",
                    source_root=str(root),
                    runner=fake,
                    out=io.StringIO(),
                )
                runs.append(fake.gate_calls)

            self.assertEqual([argv for argv, _, _ in runs[0]], [argv for argv, _, _ in runs[1]])
            self.assertEqual(runs[0][0][1], standalone.resolve())
            self.assertEqual(runs[1][0][1], nested.resolve())

    def test_process_cwd_does_not_change_resolution_or_argv(self) -> None:
        """AC-2."""

        original = os.getcwd()
        self.addCleanup(os.chdir, original)
        with TemporaryDirectory() as raw, TemporaryDirectory() as elsewhere:
            root = _make_root(Path(raw))

            os.chdir(raw)
            here = FakeRunner()
            runner.run_gate(
                gate_id="lint", source_root=str(root), runner=here, out=io.StringIO()
            )

            os.chdir(elsewhere)
            there = FakeRunner()
            runner.run_gate(
                gate_id="lint", source_root=str(root), runner=there, out=io.StringIO()
            )

            # Leave both temp dirs before their context managers try to remove them
            # (Windows cannot delete the process's current working directory).
            os.chdir(original)

            self.assertEqual(here.gate_calls[0][0], there.gate_calls[0][0])
            self.assertEqual(here.gate_calls[0][1], root.resolve())
            self.assertEqual(there.gate_calls[0][1], root.resolve())

    def test_relative_source_root_is_anchored_to_driver_root(self) -> None:
        """AC-2: a relative root is independent of the process current directory."""

        original = os.getcwd()
        self.addCleanup(os.chdir, original)
        with TemporaryDirectory() as raw, TemporaryDirectory() as elsewhere:
            driver_root = Path(raw)
            root = _make_root(driver_root, name="checkout")

            with patch.object(runner, "DRIVER_ROOT", driver_root):
                os.chdir(driver_root)
                here = FakeRunner()
                run_cli.main(
                    ["run", "lint", "--source-root", "checkout"],
                    runner=here,
                    stdout=io.StringIO(),
                )

                os.chdir(elsewhere)
                there = FakeRunner()
                run_cli.main(
                    ["run", "lint", "--source-root", "checkout"],
                    runner=there,
                    stdout=io.StringIO(),
                )

            os.chdir(original)

            self.assertEqual(here.gate_calls[0][0], there.gate_calls[0][0])
            self.assertEqual(here.gate_calls[0][1], root.resolve())
            self.assertEqual(there.gate_calls[0][1], root.resolve())

    def test_cli_run_propagates_gate_exit_code(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner(results=[runner.CommandResult(returncode=3)])
            code = run_cli.main(
                ["run", "lint", "--source-root", str(root)],
                runner=fake,
                stdout=io.StringIO(),
            )
            self.assertEqual(code, 3)


class MultiCommandShortCircuitTests(unittest.TestCase):
    """Slice 5 / AC-4 — stop at the first failure; no misleading second failure."""

    def test_first_failure_returns_its_code_and_skips_later_commands(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner(
                results=[
                    runner.CommandResult(returncode=7),
                    runner.CommandResult(returncode=0),
                ]
            )
            code = runner.run_gate(
                gate_id="coverage", source_root=str(root), runner=fake, out=io.StringIO()
            )
            self.assertEqual(code, 7)
            self.assertEqual(len(fake.gate_calls), 1)
            self.assertEqual(fake.gate_calls[0][0][:6],
                             ["uv", "run", "--with", "coverage", "coverage", "run"])

    def test_all_commands_run_when_each_succeeds(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner()
            code = runner.run_gate(
                gate_id="coverage", source_root=str(root), runner=fake, out=io.StringIO()
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(fake.gate_calls), 2)


class SourceShaBindingTests(unittest.TestCase):
    """Slice 6 / AC-3 — SHA mismatch or missing path fails before any gate subprocess."""

    def test_expected_sha_match_allows_execution(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner(sha=_FIXED_SHA)
            code = runner.run_gate(
                gate_id="lint",
                source_root=str(root),
                runner=fake,
                out=io.StringIO(),
                expected_source_sha=_FIXED_SHA,
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(fake.gate_calls), 1)

    def test_expected_sha_mismatch_fails_before_any_gate_subprocess(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            fake = FakeRunner(sha=_FIXED_SHA)
            with self.assertRaises(runner.DriverError):
                runner.run_gate(
                    gate_id="lint",
                    source_root=str(root),
                    runner=fake,
                    out=io.StringIO(),
                    expected_source_sha="f" * 40,
                )
            self.assertEqual(fake.gate_calls, [])

    def test_missing_required_path_fails_before_any_gate_subprocess(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            (root / "pyproject.toml").unlink()
            fake = FakeRunner()
            with self.assertRaises(runner.DriverError):
                runner.run_gate(
                    gate_id="lint", source_root=str(root), runner=fake, out=io.StringIO()
                )
            self.assertEqual(fake.gate_calls, [])

    def test_cli_run_reports_sha_mismatch_with_nonzero_exit(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            code = run_cli.main(
                [
                    "run", "lint",
                    "--source-root", str(root),
                    "--expected-source-sha", "f" * 40,
                ],
                runner=FakeRunner(sha=_FIXED_SHA),
                stdout=io.StringIO(),
            )
            self.assertNotEqual(code, 0)


class ValidateCommandTests(unittest.TestCase):
    """``validate --source-root`` accepts a valid root and rejects a bad one."""

    def test_validate_accepts_a_valid_root(self) -> None:
        with TemporaryDirectory() as raw:
            root = _make_root(Path(raw))
            out = io.StringIO()
            code = run_cli.main(
                ["validate", "--source-root", str(root)],
                runner=FakeRunner(),
                stdout=out,
            )
            self.assertEqual(code, 0)
            self.assertIn(str(root.resolve()), out.getvalue())

    def test_validate_rejects_a_contract_less_root(self) -> None:
        with TemporaryDirectory() as raw:
            plain = Path(raw) / "plain"
            plain.mkdir()
            code = run_cli.main(
                ["validate", "--source-root", str(plain)],
                runner=FakeRunner(),
                stdout=io.StringIO(),
            )
            self.assertNotEqual(code, 0)

    def test_run_requires_source_root(self) -> None:
        with self.assertRaises(SystemExit):
            run_cli.main(["run", "lint"], runner=FakeRunner(), stdout=io.StringIO())


if __name__ == "__main__":
    unittest.main()
