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
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import Run


def _spec() -> TaskSpec:
    return TaskSpec.build(
        id="WI-01", task_type="python", executor="python-executor",
        allowed_scope=("src/**",),
    )


class WorkItemTests(unittest.TestCase):
    def test_registered_active_item_has_a_stable_producer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"; prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            life = RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            register_work_items(run, (_spec(),))
            with activate_work_item(run, "WI-01") as item:
                self.assertEqual(item.producer_id, f"{run.run_id}:WI-01")

    def test_unknown_or_inactive_item_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"; prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            with self.assertRaisesRegex(WorkItemError, "unregistered"):
                with activate_work_item(run, "WI-01"):
                    pass

    def test_registration_does_not_activate_a_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"; prompt.write_text("x", encoding="utf-8")
            run = Run.create("feature", prompt, None, root / "runs" / "feature", root)
            RunLifecycle.initialize(run, tasks=[("WI-01", ())])
            register_work_items(run, (_spec(),))

            with self.assertRaisesRegex(WorkItemError, "unregistered"):
                require_active_work_item(run, "WI-01")


if __name__ == "__main__":
    unittest.main()
