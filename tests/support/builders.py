"""Task-spec, run, and execution builders shared by the repair/execution suites (QG-01).

``tests/test_repair.py`` and ``tests/test_execution.py`` each hand-rolled a ``Run``/
``RunLifecycle`` bootstrap (``_run``) and a ``TaskExecution`` factory (``_execution``) that were
either identical or differed only in the ``TaskSpec`` passed to ``TaskSpec.build``. The
``Run``/``TaskExecution`` builders are consolidated here; each caller keeps its own
task-spec builder (``bounded_repair_spec`` / ``narrow_blocker_spec``) because the two suites
intentionally exercise slightly different spec shapes (scope, acceptance criteria) and
collapsing them into one parameterized function would obscure that difference rather than
remove duplication.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.execution import TaskExecution
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import Run
from pipeline_core.verification import VerifierLaunchers
from schemas.contracts import TaskSpec

from tests.support.fakes import ENVELOPE_ANCHORS, VERIFIER_ANCHORS


def initialize_run(root: Path, *, tasks=(("VR-03", ()),)) -> RunLifecycle:
    """Create a fresh ``prompt.md`` + ``Run`` under ``root`` and initialize its lifecycle."""
    prompt = root / "prompt.md"
    prompt.write_text("feature prompt", encoding="utf-8")
    run = Run.create("exec", prompt, None, root / "runs" / "exec", root)
    return RunLifecycle.initialize(run, tasks=list(tasks))


def bounded_repair_spec(
    task_id: str = "VR-03", *, max_repair_attempts: int = 2, **overrides: object
) -> TaskSpec:
    """The ``TaskSpec`` shape used by the bounded-repair-loop scenarios."""
    base: dict[str, object] = dict(
        id=task_id,
        title="Implement the bounded repair loop",
        path=f"docs/plans/tasks/{task_id}_bounded-repair-loop.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("feature-pipeline-skill/pipeline_core/execution.py",),
        out_of_scope=("feature-pipeline-skill/pipeline_core/runner_cli.py",),
        acceptance_criteria=(
            "Each FAIL consumes at most one repair attempt.",
            "Every repair returns through implemented and reruns the gate.",
        ),
        verification_commands=(),
        max_repair_attempts=max_repair_attempts,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def narrow_blocker_spec(
    task_id: str = "VR-03", *, path: str | None = None, **overrides: object
) -> TaskSpec:
    """The ``TaskSpec`` shape used by the ``run_task`` orchestration/blocker-edit scenarios."""
    base: dict[str, object] = dict(
        id=task_id,
        title="Implement the bounded repair loop",
        path=path or f"docs/plans/tasks/{task_id}_bounded-repair-loop.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("feature-pipeline-skill/pipeline_core/execution.py",),
        acceptance_criteria=("The loop is bounded.",),
        verification_commands=(),
        max_repair_attempts=2,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def build_execution(
    spec: TaskSpec, executor: object, task: object, test: object, *, plan_path: str | None = None
) -> TaskExecution:
    """Build the ``TaskExecution`` request for ``run_task`` from a spec and three doubles."""
    kwargs: dict[str, object] = dict(
        spec=spec,
        adapter=executor,
        launchers=VerifierLaunchers(task=task, test=test),
        verifier_anchors=VERIFIER_ANCHORS,
        envelope_anchors=ENVELOPE_ANCHORS,
    )
    if plan_path is not None:
        kwargs["plan_path"] = plan_path
    return TaskExecution(**kwargs)
