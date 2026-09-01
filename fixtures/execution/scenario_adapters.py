"""Deterministic fake adapters and the execute-mode integration scenario table.

These are the *fixtures* EMI-01's acceptance evidence is built from: no real ``claude``
process is launched, every launch outcome is scripted, so one ``execute_run`` invocation is
byte-reproducible across machines.

The fakes speak the exact two-call adapter protocol the real
:class:`~pipeline_core.adapters.ClaudeAdapter` speaks and that
:func:`pipeline_core.dispatch.dispatch_executor` /
:func:`pipeline_core.verification.orchestrate_verification` settle:

* an executor launch writes a prose report, then a same-session tool-free continuation
  returns the strict JSON status envelope;
* a verifier launch writes a prose report with a ``- Verdict:`` line, then a same-session
  tool-free continuation returns the strict four-key JSON verdict envelope.

``SCENARIOS`` maps a scenario name to a :class:`Scenario`; ``tests/test_execute_mode.py``
turns each into an ``ExecuteRequest`` and asserts the terminal status and exit code. A few
scenarios that need a second invocation (resume, adapter switch) or an out-of-band lock
(live lease) are driven by bespoke test methods and are described in ``README.md``.

Standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_core.adapters import LaunchResult

_ATTEMPT_RE = re.compile(r'"attempt":\s*(\d+)')


def _write(path: object, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class ScriptedExecutor:
    """A fake executor adapter. ``results[i]`` is the outcome of the *i*-th real launch:
    ``"implemented"``, ``"blocked"``, or ``"launch-fail"`` (a non-zero launch exit)."""

    def __init__(self, results: tuple[str, ...] = ("implemented",)) -> None:
        self.results = list(results)
        self.launches = 0
        self.calls: list[dict] = []
        self._status = "implemented"

    def launch(self, request):  # noqa: ANN001 - test double
        prompt = request.prompt
        if request.no_tools or request.resume_session_id:
            match = _ATTEMPT_RE.search(prompt)
            attempt = int(match.group(1)) if match else 1
            text = json.dumps(
                {"role": "executor", "status": self._status,
                 "task_id": request.task_id, "attempt": attempt})
            _write(request.report_path, text)
            return LaunchResult(exit_code=0, stdout=text, session_id="exec-sess")

        index = self.launches
        self.launches += 1
        status = self.results[index] if index < len(self.results) else self.results[-1]
        self.calls.append(
            {"role": request.role, "task_id": request.task_id,
             "fresh_session": request.fresh_session,
             "is_repair": "Repair of:" in prompt})
        if status == "launch-fail":
            return LaunchResult(exit_code=1, stdout="launch failed", session_id="exec-sess")
        self._status = "implemented" if status == "implemented" else "blocked"
        _write(
            request.report_path,
            f"# Executor report\n\n- Status: {self._status}\n\n## Files changed\n- none\n")
        return LaunchResult(exit_code=0, stdout="", session_id="exec-sess")


class ScriptedVerifier:
    """One verifier role. ``verdicts[g]`` is the token returned at verification gate ``g``.

    With ``malformed_envelope`` the JSON verdict-envelope continuation returns unparseable
    text, which the runner must treat as a failed settlement and block the task.
    """

    def __init__(self, verdicts: tuple[str, ...], *, malformed_envelope: bool = False) -> None:
        self.verdicts = list(verdicts)
        self.malformed_envelope = malformed_envelope
        self.gate = -1
        self.calls: list[dict] = []

    def _verdict(self) -> str:
        return self.verdicts[min(self.gate, len(self.verdicts) - 1)]

    def launch(self, request):  # noqa: ANN001 - test double
        is_env = bool(request.resume_session_id)
        if not is_env:
            self.gate += 1
        verdict = self._verdict()
        self.calls.append(
            {"role": request.role, "is_env": is_env, "read_only": request.read_only,
             "no_tools": request.no_tools})
        if is_env:
            if self.malformed_envelope:
                text = "{ this is not a verdict envelope"
            else:
                match = _ATTEMPT_RE.search(request.prompt)
                attempt = int(match.group(1)) if match else 1
                text = json.dumps(
                    {"role": request.role, "verdict": verdict,
                     "task_id": request.task_id, "attempt": attempt})
        else:
            finding = "clean" if verdict == "PASS" else "defect: acceptance criterion unmet"
            text = f"# {request.role}\n\n- Verdict: {verdict}\n\n- Findings: {finding}\n"
        _write(request.report_path, text)
        return LaunchResult(exit_code=0, stdout=text, session_id="ver-sess")


@dataclass(frozen=True)
class Scenario:
    """One single-invocation execute-mode scenario and its expected terminal shape."""

    summary: str
    task_ids: tuple[str, ...]
    executor_results: tuple[str, ...]
    task_verdicts: tuple[str, ...]
    test_verdicts: tuple[str, ...]
    expected_status: str
    expected_exit: int
    controls: dict = field(default_factory=dict)
    environment: dict = field(default_factory=lambda: {"claude": True})
    malformed_test_envelope: bool = False

    def executor(self) -> ScriptedExecutor:
        return ScriptedExecutor(self.executor_results)

    def task_verifier(self) -> ScriptedVerifier:
        return ScriptedVerifier(self.task_verdicts)

    def test_verifier(self) -> ScriptedVerifier:
        return ScriptedVerifier(
            self.test_verdicts, malformed_envelope=self.malformed_test_envelope)


#: ``EXIT_OK`` / ``EXIT_GATE_PENDING`` / ``EXIT_BLOCKED`` / ``EXIT_ERROR`` from the baseline
#: compatibility exit-code table.
EXIT_OK, EXIT_GATE_PENDING, EXIT_BLOCKED, EXIT_ERROR = 0, 10, 20, 30


SCENARIOS: dict[str, Scenario] = {
    "direct-success": Scenario(
        "AC-1: one ready task -> executor launch, runner checks, two PASS -> verified",
        task_ids=("EX-01",),
        executor_results=("implemented",),
        task_verdicts=("PASS",),
        test_verdicts=("PASS",),
        expected_status="ok",
        expected_exit=EXIT_OK,
    ),
    "successful-repair": Scenario(
        "a first FAIL spends one bounded repair, then the second gate verifies",
        task_ids=("EX-01",),
        executor_results=("implemented", "implemented"),
        task_verdicts=("FAIL", "PASS"),
        test_verdicts=("PASS", "PASS"),
        expected_status="ok",
        expected_exit=EXIT_OK,
    ),
    "repair-exhaustion": Scenario(
        "AC-2: FAIL with no repair budget ends blocked with a non-zero exit",
        task_ids=("EX-01",),
        executor_results=("implemented",),
        task_verdicts=("FAIL",),
        test_verdicts=("PASS",),
        expected_status="blocked",
        expected_exit=EXIT_BLOCKED,
        controls={"max_repair_attempts": 0},
    ),
    "external-blocker": Scenario(
        "AC-4: an external BLOCKED verdict ends blocked and spends no repair attempt",
        task_ids=("EX-01",),
        executor_results=("implemented",),
        task_verdicts=("BLOCKED",),
        test_verdicts=("PASS",),
        expected_status="blocked",
        expected_exit=EXIT_BLOCKED,
    ),
    "executor-cannot-implement": Scenario(
        "AC-4: an executor that reports blocked ends blocked before any verification gate",
        task_ids=("EX-01",),
        executor_results=("blocked",),
        task_verdicts=("PASS",),
        test_verdicts=("PASS",),
        expected_status="blocked",
        expected_exit=EXIT_BLOCKED,
    ),
    "malformed-verifier-output": Scenario(
        "AC-4: a malformed verdict envelope fails closed with a diagnostic, never verified",
        task_ids=("EX-01",),
        executor_results=("implemented",),
        task_verdicts=("PASS",),
        test_verdicts=("PASS",),
        expected_status="blocked",
        expected_exit=EXIT_BLOCKED,
        malformed_test_envelope=True,
    ),
    "dependency-ordering": Scenario(
        "AC-1: EX-02 becomes ready only after EX-01 verifies; both end verified in plan order",
        task_ids=("EX-01", "EX-02"),
        executor_results=("implemented", "implemented"),
        task_verdicts=("PASS", "PASS"),
        test_verdicts=("PASS", "PASS"),
        expected_status="ok",
        expected_exit=EXIT_OK,
    ),
    "dependency-suppression": Scenario(
        "AC-2/AC-4: EX-01 blocks at the repair limit; EX-02 and EX-03 are suppressed, not run",
        task_ids=("EX-01", "EX-02", "EX-03"),
        executor_results=("implemented",),
        task_verdicts=("FAIL",),
        test_verdicts=("PASS",),
        expected_status="blocked",
        expected_exit=EXIT_BLOCKED,
        controls={"max_repair_attempts": 0},
    ),
    "adapter-unavailable": Scenario(
        "AC-4: no adapter available in the environment -> runner error, nothing substituted",
        task_ids=("EX-01",),
        executor_results=("implemented",),
        task_verdicts=("PASS",),
        test_verdicts=("PASS",),
        expected_status="error",
        expected_exit=EXIT_ERROR,
        environment={},
    ),
    "gate-pending": Scenario(
        "AC-5: no --approve-plan and no --unattended -> gate pending, no executor dispatched",
        task_ids=("EX-01",),
        executor_results=("implemented",),
        task_verdicts=("PASS",),
        test_verdicts=("PASS",),
        expected_status="gate-pending",
        expected_exit=EXIT_GATE_PENDING,
        controls={"_no_gate": True},
    ),
}
