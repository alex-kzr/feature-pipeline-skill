"""WorkItem launch attribution is required at every launch boundary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feature_pipeline.application.work_items import (
    WorkItemError,
    activate_work_item,
    register_work_items,
    require_active_work_item,
)
from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.work_items import ProducerDescriptor, WorkItem
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import Run


def _spec() -> TaskSpec:
    return TaskSpec.build(
        id="WI-01",
        task_type="python",
        executor="python-executor",
        allowed_scope=("src/**",),
    )


class WorkItemTests(unittest.TestCase):
    def test_work_item_from_spec_carries_the_revisioned_operation_contract(self) -> None:
        spec = TaskSpec.build(
            id="WI-01",
            task_type="python",
            executor="python-executor",
            title="Revisioned item",
            path="docs/plans/tasks/WI-01.md",
            allowed_scope=("src/**",),
            out_of_scope=(".pipeline/**",),
            required_skills=("skills/python/SKILL.md",),
            verification_commands=({"cwd": ".", "argv": ("uv", "run", "test")},),
            max_repair_attempts=3,
            blocking_conditions="external service unavailable",
        )

        item = WorkItem.from_spec("run-7", spec, generation=2, snapshot="sha256:snapshot")

        self.assertEqual(item.run_id, "run-7")
        self.assertEqual(item.task_id, "WI-01")
        self.assertEqual(item.parent_id, "WI-01")
        self.assertEqual(item.operation_id, "run-7:WI-01:executor:2")
        self.assertEqual(item.schema_revision, 1)
        self.assertEqual(item.kind, "python")
        self.assertEqual(item.stack, "python")
        self.assertEqual(item.role, "python-executor")
        self.assertEqual(item.goal, "Revisioned item")
        self.assertEqual(item.scope, ("src/**",))
        self.assertEqual(item.skills, ("skills/python/SKILL.md",))
        self.assertEqual(item.budgets["max_repair_attempts"], 3)
        self.assertEqual(item.freshness, "declared")
        self.assertEqual(item.snapshot, "sha256:snapshot")
        self.assertEqual(item.generation, 2)

    def test_versioned_producer_descriptor_rejects_an_incompatible_item(self) -> None:
        descriptor = ProducerDescriptor(
            name="executor",
            emitted_kind="python",
            schema_revision=1,
            active=True,
        )
        item = WorkItem(
            run_id="run-7", task_id="WI-01", parent_id="WI-01",
            operation_id="run-7:WI-01:executor:1", kind="docs", stack="docs",
            role="python-executor", generation=1,
        )

        with self.assertRaisesRegex(ValueError, "incompatible"):
            descriptor.require_compatible(item)

    def test_registered_active_item_has_a_stable_producer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            register_work_items(run, (_spec(),))
            with activate_work_item(run, "WI-01") as item:
                self.assertEqual(item.producer_id, f"{run.run_id}:WI-01")

    def test_unknown_or_inactive_item_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            with self.assertRaisesRegex(WorkItemError, "unregistered"):
                with activate_work_item(run, "WI-01"):
                    pass

    def test_registration_does_not_activate_a_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            register_work_items(run, (_spec(),))

            with self.assertRaisesRegex(WorkItemError, "unregistered"):
                require_active_work_item(run, "WI-01")

    def test_registration_rejects_an_inactive_producer_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])

            with self.assertRaisesRegex(WorkItemError, "inactive"):
                register_work_items(
                    run,
                    (_spec(),),
                    descriptors={"WI-01": ProducerDescriptor(
                        name="executor", emitted_kind="python", schema_revision=1, active=False,
                    )},
                )

    def test_registration_rebuilds_a_snapshot_without_changing_persisted_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            run.set_task_contract("WI-01", "tasks/WI-01.md", "sha256:contract", version="v1")
            record = run.task("WI-01")
            record.execution_evidence["executor_report"] = "reports/WI-01.md"
            run.save()

            resumed = Run.load(run.run_dir, root)
            item = register_work_items(resumed, (_spec(),))[0]

            self.assertEqual(item.snapshot, "sha256:contract")
            self.assertEqual(
                resumed.task("WI-01").execution_evidence["executor_report"],
                "reports/WI-01.md",
            )

    def test_registration_accepts_an_approved_new_contract_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            register_work_items(run, (_spec(),))
            amended = TaskSpec.build(
                id="WI-01", task_type="python", executor="python-executor",
                allowed_scope=("src/**", "tests/**"),
            )

            item = register_work_items(run, (amended,))[0]

            self.assertEqual(item.scope, ("src/**", "tests/**"))


if __name__ == "__main__":
    unittest.main()
