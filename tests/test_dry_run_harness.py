"""Acceptance tests for the core-side dry-run harness (`scripts/dry_run_harness.py`).

The harness produces the deterministic, redacted core-side S1-S7 dry-run captures that
CA-02 / CX-01 compare against the legacy baseline. These tests assert the properties that
matter for that comparison — determinism (byte-identical from independent clean fixture
copies), redaction (no host path, user, credential, token, or machine name), the safety
constraint that the harness never drives a delivery gate, and (CR-04) that the harness runs
over the shared-project fixture whose core inputs stay in sync with the canonical copy in
the parent repo.
"""

from __future__ import annotations

import getpass
import json
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dry_run_harness as harness  # noqa: E402


ALL_SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")

#: The vendored core inputs the harness seeds from.
VENDORED_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "shared-project"
#: The canonical fixture in the parent repo (present in the umbrella checkout, absent in a
#: standalone child checkout).
CANONICAL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "docs" / "acceptance" / "fixtures" / "shared-project"
)
CORE_INPUT_FILES = ("profile.json", "plan.json", "plan-s5.json")


class ScenarioTableTests(unittest.TestCase):
    def test_the_table_defines_exactly_s1_through_s7(self) -> None:
        self.assertEqual(tuple(harness.SCENARIOS), ALL_SCENARIOS)

    def test_no_scenario_requests_commit_and_only_the_push_denial_scenario_passes_push(self) -> None:
        for name in ALL_SCENARIOS:
            argv = harness.SCENARIOS[name].cli_args
            self.assertNotIn("--commit", argv, f"{name} must never request the commit gate")
            if name == "S4":
                self.assertIn("--push", argv, "S4 is the push-denial scenario")
            else:
                self.assertNotIn("--push", argv, f"{name} must not pass --push")


class SharedFixtureTests(unittest.TestCase):
    """CR-04: the harness seeds from `fixtures/shared-project/` and its core inputs mirror
    the canonical fixture the legacy runner consumes."""

    def test_harness_targets_only_the_shared_project_fixture(self) -> None:
        self.assertEqual(harness.SHARED_FIXTURE, "shared-project")
        self.assertEqual(list(harness._FIXTURE_LAYOUT), ["shared-project"])
        for name in ALL_SCENARIOS:
            for run in harness.SCENARIOS[name].runs:
                self.assertIn(run.fixture, (None, "shared-project"))

    def test_no_retired_portable_fixture_identifier_remains_in_the_harness(self) -> None:
        text = Path(harness.__file__).read_text(encoding="utf-8")
        # Needles assembled at runtime so this test file is not a false self-match.
        retired = ["-".join(("library", "guide")), "-".join(("incident", "notes")),
                   "sculpture", "-".join(("T", "01")), "-".join(("N", "01"))]
        for needle in retired:
            self.assertNotIn(needle, text, f"retired identifier {needle!r} still in the harness")

    def test_vendored_core_inputs_parse_and_carry_the_expected_task_graph(self) -> None:
        for fname in CORE_INPUT_FILES:
            path = VENDORED_FIXTURE / fname
            self.assertTrue(path.is_file(), f"missing vendored {fname}")
            json.loads(path.read_text(encoding="utf-8"))

        def graph(name: str) -> list[tuple[str, str, tuple[str, ...]]]:
            data = json.loads((VENDORED_FIXTURE / name).read_text(encoding="utf-8"))
            return [(t["id"], t["type"], tuple(t.get("depends_on", []))) for t in data["tasks"]]

        self.assertEqual(graph("plan.json"),
                         [("SF-01", "docs", ()), ("SF-02", "docs", ("SF-01",))])
        self.assertEqual(graph("plan-s5.json"),
                         [("SF-03", "docs", ()), ("SF-04", "mystery", ())])

    def test_vendored_core_inputs_are_byte_identical_to_the_canonical_fixture(self) -> None:
        if not CANONICAL_FIXTURE.is_dir():
            self.skipTest("canonical fixture not present (standalone child checkout)")
        for fname in CORE_INPUT_FILES:
            self.assertEqual(
                (VENDORED_FIXTURE / fname).read_bytes(),
                (CANONICAL_FIXTURE / fname).read_bytes(),
                f"vendored {fname} has drifted from docs/acceptance/fixtures/shared-project/",
            )


class DeterminismTests(unittest.TestCase):
    def test_every_scenario_capture_is_byte_identical_from_two_clean_fixture_copies(self) -> None:
        for name in ALL_SCENARIOS:
            first = harness.capture_scenario(name)
            second = harness.capture_scenario(name)
            self.assertEqual(first, second, f"{name} capture is not deterministic")

    def test_a_capture_carries_the_full_c1_to_c8_plan_shape_for_a_plan_dry_run(self) -> None:
        capture = harness.capture_scenario("S1")
        for marker in ("C1.", "C2.", "C3.", "C4.", "C5.", "C6.", "C7.", "C8."):
            self.assertIn(marker, capture)
        # DR-5: a non-blocked --dry-run reports a pending delivery gate, exit 10.
        self.assertIn("exit: 10", capture)
        self.assertIn("C5. Exit code: 10", capture)


class RedactionTests(unittest.TestCase):
    def _host_needles(self) -> list[str]:
        needles = [
            str(Path.home()),
            str(Path.home().resolve()),
            harness.WORKDIR_ROOT and str(harness.WORKDIR_ROOT) or "",
            socket.gethostname(),
        ]
        user = getpass.getuser()
        if len(user) >= 4:
            needles.append(user)
        return [n for n in needles if n]

    def test_no_capture_leaks_a_host_path_user_or_machine_name(self) -> None:
        needles = self._host_needles()
        for name in ALL_SCENARIOS:
            capture = harness.capture_scenario(name)
            for needle in needles:
                self.assertNotIn(needle, capture, f"{name} leaked {needle!r}")

    def test_captures_reference_only_logical_anchor_tokens_for_the_project_tree(self) -> None:
        capture = harness.capture_scenario("S1")
        self.assertIn("<project_root>", capture)
        for raw in ("AppData", "/tmp/", "\\Temp\\"):
            self.assertNotIn(raw, capture)


class SafetyScenarioTests(unittest.TestCase):
    def test_push_denial_scenario_refuses_before_any_artifact(self) -> None:
        capture = harness.capture_scenario("S4")
        self.assertIn("exit: 1", capture)
        self.assertIn("push is denied at this stage and the runner has no push code path.", capture)
        self.assertNotIn("run.json", capture)

    def test_dependency_scenario_fails_closed(self) -> None:
        capture = harness.capture_scenario("S2")
        self.assertIn("exit: 20", capture)
        self.assertIn("dependency-not-satisfied", capture)

    def test_absolute_path_and_unknown_type_scenario_is_rejected(self) -> None:
        capture = harness.capture_scenario("S5")
        self.assertIn("exit: 30", capture)
        self.assertIn("unknown-task-type", capture)

    def test_harness_writes_nothing_under_docs_acceptance(self) -> None:
        acceptance = Path(__file__).resolve().parents[2] / "docs" / "acceptance"
        before = sorted(p for p in acceptance.rglob("*")) if acceptance.exists() else []
        for name in ALL_SCENARIOS:
            harness.capture_scenario(name)
        after = sorted(p for p in acceptance.rglob("*")) if acceptance.exists() else []
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
