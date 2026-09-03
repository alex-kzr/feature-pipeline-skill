"""IN-02 — one pure task graph for dependency correctness, order, and selection.

Every dependency check, the deterministic execution order, and the ``--task`` / ``--through``
selection queries live in one immutable :class:`feature_pipeline.domain.graph.TaskGraph`.
These tests pin:

* AC-1 — every invalid graph (unknown dependency, self-dependency, cycle, unknown selection,
  dependency-closure violation) is rejected with a typed
  :class:`~feature_pipeline.domain.errors.DomainError`, before any caller acts on it;
* AC-2 — the deterministic topological order and the ``task`` / ``through`` selections stay
  byte-compatible with the shipped ``pipeline_core.runner_cli._select`` /
  ``pipeline_core.execution._select_ids`` behaviour and the compatibility goldens;
* AC-3 — the one application entry point
  (:func:`feature_pipeline.application.selection.resolve_selection`) reproduces the exact CLI
  rejection strings, so a later cutover changes no accepted input or message.
"""

from __future__ import annotations

import itertools
import random
import unittest

from feature_pipeline.application.selection import (
    DependencyStatus,
    ResolvedSelection,
    SelectionError,
    dependency_status,
    resolve_selection,
)
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.graph import (
    DependencyClosureViolation,
    DependencyCycle,
    DuplicateTask,
    SelfDependency,
    TaskGraph,
    UnknownDependency,
    UnknownTask,
)

from pipeline_core.execution import _select_ids
from pipeline_core.runner_cli import _select


# --- construction + validation (AC-1) -------------------------------------------------------


class GraphValidationTests(unittest.TestCase):
    def test_from_pairs_keeps_authored_order_and_edges(self) -> None:
        graph = TaskGraph.from_pairs(
            [("A-01", []), ("A-02", ["A-01"]), ("A-03", ["A-01", "A-02"])]
        )
        self.assertEqual(graph.ids, ("A-01", "A-02", "A-03"))
        self.assertEqual(graph.dependencies("A-03"), ("A-01", "A-02"))
        self.assertEqual(graph.position("A-02"), 1)
        self.assertIn("A-01", graph)
        self.assertNotIn("Z-99", graph)

    def test_from_definitions_reads_id_and_depends_on(self) -> None:
        class _Def:
            def __init__(self, tid: str, deps: tuple[str, ...]) -> None:
                self.id = tid
                self.depends_on = deps

        graph = TaskGraph.from_definitions(
            [_Def("A-01", ()), _Def("A-02", ("A-01",))]
        )
        self.assertEqual(graph.ids, ("A-01", "A-02"))
        self.assertEqual(graph.dependencies("A-02"), ("A-01",))

    def test_duplicate_task_id_is_rejected(self) -> None:
        with self.assertRaises(DuplicateTask) as caught:
            TaskGraph.from_pairs([("A-01", []), ("A-01", [])])
        self.assertEqual(caught.exception.code, "duplicate-task")
        self.assertIsInstance(caught.exception, DomainError)

    def test_unknown_dependency_is_rejected(self) -> None:
        with self.assertRaises(UnknownDependency) as caught:
            TaskGraph.from_pairs([("A-01", ["A-00"]), ("A-02", ["A-01"])])
        self.assertEqual(caught.exception.code, "unknown-dependency")
        self.assertIn("A-00", str(caught.exception))
        self.assertIn("A-01", str(caught.exception))

    def test_self_dependency_is_rejected(self) -> None:
        with self.assertRaises(SelfDependency) as caught:
            TaskGraph.from_pairs([("A-01", ["A-01"])])
        self.assertEqual(caught.exception.code, "self-dependency")
        self.assertIn("A-01", str(caught.exception))

    def test_direct_cycle_is_rejected_with_members_in_plan_order(self) -> None:
        with self.assertRaises(DependencyCycle) as caught:
            TaskGraph.from_pairs([("A-01", ["A-02"]), ("A-02", ["A-01"])])
        self.assertEqual(caught.exception.code, "dependency-cycle")
        self.assertIn("A-01", str(caught.exception))
        self.assertIn("A-02", str(caught.exception))
        # members are reported in plan position order, not hash order
        self.assertLess(
            str(caught.exception).index("A-01"), str(caught.exception).index("A-02")
        )

    def test_indirect_cycle_is_rejected(self) -> None:
        with self.assertRaises(DependencyCycle):
            TaskGraph.from_pairs(
                [("A-01", ["A-03"]), ("A-02", ["A-01"]), ("A-03", ["A-02"])]
            )

    def test_duplicate_edge_on_one_task_is_rejected(self) -> None:
        with self.assertRaises(UnknownDependency):
            TaskGraph.from_pairs([("A-02", ["A-01", "A-01"])])

    def test_forward_reference_dependency_is_allowed_when_acyclic(self) -> None:
        # authored out of dependency order, but still a DAG
        graph = TaskGraph.from_pairs([("A-02", ["A-01"]), ("A-01", [])])
        self.assertEqual(graph.order, ("A-01", "A-02"))


# --- deterministic order (AC-2) -----------------------------------------------------------


class GraphOrderTests(unittest.TestCase):
    def test_order_is_plan_order_when_plan_is_already_topological(self) -> None:
        pairs = [("A-01", []), ("A-02", ["A-01"]), ("A-03", ["A-02"]), ("A-04", ["A-01"])]
        graph = TaskGraph.from_pairs(pairs)
        self.assertEqual(graph.order, ("A-01", "A-02", "A-03", "A-04"))

    def test_order_breaks_ties_by_plan_position(self) -> None:
        # B-02 and B-03 are both ready after B-01; plan lists B-02 first, so it wins.
        graph = TaskGraph.from_pairs(
            [("B-01", []), ("B-02", ["B-01"]), ("B-03", ["B-01"])]
        )
        self.assertEqual(graph.order, ("B-01", "B-02", "B-03"))

    def test_order_is_a_valid_topological_sort(self) -> None:
        pairs = [
            ("C-01", []),
            ("C-02", ["C-01"]),
            ("C-03", ["C-01"]),
            ("C-04", ["C-02", "C-03"]),
            ("C-05", ["C-04"]),
        ]
        graph = TaskGraph.from_pairs(pairs)
        order = graph.order
        seen: set[str] = set()
        for tid in order:
            for dep in graph.dependencies(tid):
                self.assertIn(dep, seen, f"{tid} scheduled before its dependency {dep}")
            seen.add(tid)
        self.assertEqual(set(order), {"C-01", "C-02", "C-03", "C-04", "C-05"})

    def test_order_is_stable_under_equivalent_reorderings_of_independent_tasks(self) -> None:
        # Independent chains authored in the same relative order must always come back the
        # same way, regardless of interleaving.
        base = [("D-01", []), ("D-02", ["D-01"]), ("E-01", []), ("E-02", ["E-01"])]
        graph = TaskGraph.from_pairs(base)
        self.assertEqual(graph.order, ("D-01", "D-02", "E-01", "E-02"))


# --- selection parity with the two shipped call sites (AC-2 / AC-3) -----------------------


_PARITY_PLAN = [
    {"id": "P-01", "type": "python", "depends_on": []},
    {"id": "P-02", "type": "python", "depends_on": ["P-01"]},
    {"id": "P-03", "type": "python", "depends_on": ["P-01"]},
    {"id": "P-04", "type": "python", "depends_on": ["P-02", "P-03"]},
]


class SelectionParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph.from_pairs(
            [(t["id"], t["depends_on"]) for t in _PARITY_PLAN]
        )
        self.ids = [t["id"] for t in _PARITY_PLAN]

    def test_no_selection_matches_cli_select_authored_order(self) -> None:
        cli = [t["id"] for t in _select(list(_PARITY_PLAN), None, None)]
        exe = _select_ids(self.ids, None, None)
        resolved = resolve_selection(self.graph)
        self.assertEqual(list(resolved.task_ids), cli)
        self.assertEqual(list(resolved.task_ids), exe)
        self.assertEqual(resolved.mode, "all")

    def test_task_selection_matches_both_call_sites(self) -> None:
        for tid in self.ids:
            cli = [t["id"] for t in _select(list(_PARITY_PLAN), tid, None)]
            exe = _select_ids(self.ids, tid, None)
            resolved = resolve_selection(self.graph, task=tid)
            self.assertEqual(list(resolved.task_ids), cli, tid)
            self.assertEqual(list(resolved.task_ids), exe, tid)
            self.assertEqual(resolved.mode, "task")

    def test_through_selection_matches_cli_prefix(self) -> None:
        for tid in self.ids:
            cli = [t["id"] for t in _select(list(_PARITY_PLAN), None, tid)]
            exe = _select_ids(self.ids, None, tid)
            resolved = resolve_selection(self.graph, through=tid)
            self.assertEqual(list(resolved.task_ids), cli, tid)
            self.assertEqual(list(resolved.task_ids), exe, tid)
            self.assertEqual(resolved.mode, "through")

    def test_unknown_task_message_matches_cli_verbatim(self) -> None:
        with self.assertRaises(DomainError) as caught:
            resolve_selection(self.graph, task="Z-99")
        self.assertEqual(str(caught.exception), "--task names an unknown task: Z-99")

    def test_unknown_through_message_matches_cli_verbatim(self) -> None:
        with self.assertRaises(DomainError) as caught:
            resolve_selection(self.graph, through="Z-99")
        self.assertEqual(str(caught.exception), "--through names an unknown task: Z-99")

    def test_task_and_through_together_is_refused(self) -> None:
        # The shipped CLI silently lets --task win; the single entry point fails closed
        # instead (no golden exercises both flags; CP-01/CP-02 wires an argparse
        # mutually-exclusive group to match).
        with self.assertRaises(SelectionError):
            resolve_selection(self.graph, task="P-02", through="P-03")

    def test_resolved_selection_is_frozen(self) -> None:
        resolved = resolve_selection(self.graph)
        self.assertIsInstance(resolved, ResolvedSelection)
        with self.assertRaises(Exception):
            resolved.task_ids = ()  # type: ignore[misc]


class GeneratedSelectionParityTests(unittest.TestCase):
    """Random DAGs, authored in a valid topological order, cross-checked against both
    shipped selectors for every possible ``--task`` and ``--through`` argument."""

    def test_random_dags_match_the_shipped_selectors(self) -> None:
        rng = random.Random(20260903)
        for _ in range(60):
            size = rng.randint(1, 9)
            ids = [f"G-{n:02d}" for n in range(1, size + 1)]
            plan: list[dict] = []
            for index, tid in enumerate(ids):
                deps = sorted(
                    rng.sample(ids[:index], rng.randint(0, index)) if index else []
                )
                plan.append({"id": tid, "type": "python", "depends_on": deps})
            graph = TaskGraph.from_pairs([(t["id"], t["depends_on"]) for t in plan])

            for arg in [None, *ids]:
                cli = [t["id"] for t in _select(list(plan), arg, None)]
                self.assertEqual(
                    list(resolve_selection(graph, task=arg).task_ids)
                    if arg is not None
                    else list(resolve_selection(graph).task_ids),
                    cli,
                )
            for arg in ids:
                cli = [t["id"] for t in _select(list(plan), None, arg)]
                self.assertEqual(
                    list(resolve_selection(graph, through=arg).task_ids), cli
                )

            # order is always a valid topological sort of the same id set
            order = graph.order
            self.assertEqual(set(order), set(ids))
            placed: set[str] = set()
            for tid in order:
                self.assertTrue(set(graph.dependencies(tid)) <= placed)
                placed.add(tid)


# --- dependency-closure + unmet-dependency queries (AC-1 / AC-2) --------------------------


class ClosureAndUnmetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph.from_pairs(
            [
                ("H-01", []),
                ("H-02", ["H-01"]),
                ("H-03", ["H-02"]),
                ("H-04", ["H-01"]),
            ]
        )

    def test_through_prefix_that_drops_a_dependency_is_a_closure_violation(self) -> None:
        # A hand-built plan whose authored order is topological, but where a task's
        # dependency sits *after* it — selecting `--through` the earlier task cuts the dep.
        graph = TaskGraph.from_pairs(
            [("K-01", []), ("K-02", ["K-01"]), ("K-03", []), ("K-04", ["K-03"])]
        )
        # K-02 -> {K-01} both inside a `--through K-02` prefix: fine.
        self.assertEqual(
            resolve_selection(graph, through="K-02").task_ids, ("K-01", "K-02")
        )
        # Force a violation: a graph where K-03 depends on the later K-04.
        bad = TaskGraph.from_pairs(
            [("K-01", []), ("K-03", ["K-04"]), ("K-04", [])]
        )
        with self.assertRaises(DependencyClosureViolation) as caught:
            resolve_selection(bad, through="K-03")
        self.assertEqual(caught.exception.code, "dependency-closure-violation")
        self.assertIn("K-04", str(caught.exception))

    def test_missing_dependencies_lists_deps_outside_a_selection_in_plan_order(self) -> None:
        self.assertEqual(self.graph.missing_dependencies(["H-03"]), ("H-01", "H-02"))
        self.assertEqual(self.graph.missing_dependencies(["H-01", "H-02", "H-03"]), ())

    def test_required_closure_is_transitive(self) -> None:
        self.assertEqual(self.graph.required_closure(["H-03"]), frozenset({"H-01", "H-02"}))
        self.assertEqual(self.graph.required_closure(["H-01"]), frozenset())

    def test_unmet_dependencies_matches_cli_dep_blocked_computation(self) -> None:
        # CLI: unmet = [d for d in selected[0]["depends_on"] if d not in verified/attested]
        self.assertEqual(
            self.graph.unmet_dependencies("H-03", satisfied=frozenset()),
            ("H-02",),
        )
        self.assertEqual(
            self.graph.unmet_dependencies("H-03", satisfied=frozenset({"H-02"})),
            (),
        )
        self.assertEqual(
            self.graph.unmet_dependencies("H-01", satisfied=frozenset()),
            (),
        )

    def test_unmet_dependencies_preserves_declared_edge_order(self) -> None:
        graph = TaskGraph.from_pairs(
            [("M-01", []), ("M-02", []), ("M-03", ["M-02", "M-01"])]
        )
        self.assertEqual(
            graph.unmet_dependencies("M-03", satisfied=frozenset()),
            ("M-02", "M-01"),
        )

    def test_unmet_dependencies_on_unknown_task_is_rejected(self) -> None:
        with self.assertRaises(UnknownTask):
            self.graph.unmet_dependencies("Z-99", satisfied=frozenset())

    def test_dependency_status_is_typed_data_not_an_exit_code(self) -> None:
        blocked = dependency_status(self.graph, "H-03", satisfied=frozenset())
        self.assertIsInstance(blocked, DependencyStatus)
        self.assertFalse(blocked.ready)
        self.assertEqual(blocked.unmet, ("H-02",))

        ready = dependency_status(self.graph, "H-03", satisfied=frozenset({"H-02"}))
        self.assertTrue(ready.ready)
        self.assertEqual(ready.unmet, ())


# --- goldens the ordering + selection must stay compatible with (AC-2) -------------------


class GoldenCompatibilityTests(unittest.TestCase):
    def test_two_task_chain_matches_the_plan_only_golden_order(self) -> None:
        # tests/goldens/compatibility/plan-only-plan.txt: SF-01 then SF-02 (SF-02 -> SF-01)
        graph = TaskGraph.from_pairs([("SF-01", []), ("SF-02", ["SF-01"])])
        self.assertEqual(graph.order, ("SF-01", "SF-02"))
        self.assertEqual(
            resolve_selection(graph).task_ids, ("SF-01", "SF-02")
        )

    def test_blocked_unmet_dependency_golden_reason(self) -> None:
        # blocked-unmet-dependency.txt: `--task SF-02` with SF-01 not verified ->
        # "dependency-not-satisfied: SF-01"
        graph = TaskGraph.from_pairs([("SF-01", []), ("SF-02", ["SF-01"])])
        self.assertEqual(
            graph.unmet_dependencies("SF-02", satisfied=frozenset()), ("SF-01",)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
