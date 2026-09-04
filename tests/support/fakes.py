"""Shared executor/verifier test doubles (QG-01).

``ScriptedExecutor`` and ``StubVerifier`` were previously defined twice, near-verbatim, in
``tests/test_repair.py`` and ``tests/test_execution.py``. Both suites drive the same
``TaskEngine``/``run_task`` two-call launch shape (a fresh prose-report launch, then a
same-session verdict/status-envelope continuation), so one shared implementation replaces the
two copies; both modules now import from here (see ``tests/README.md``, AC-2/AC-3).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline_core.adapters import LaunchResult
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.verification import VerifierAnchors

#: Shared anchors used by every repair/execution builder in this package.
ENVELOPE_ANCHORS = EnvelopeAnchors(project_root=".", agents_root=".agents")
VERIFIER_ANCHORS = VerifierAnchors(project_root="/repo", agents_root="/repo/.agents")


def write_text(path: object, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8, creating parent directories as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class ScriptedExecutor:
    """A two-call executor adapter (prose report, then same-session status envelope).

    ``results`` gives the status the *n*-th executor launch settles to — ``"implemented"``,
    ``"blocked"``, or ``"launch-fail"`` for a non-zero launch. The status envelope echoes the
    ``attempt`` embedded in its own prompt, so a repair round (attempt 2, 3, …) settles too.
    """

    def __init__(self, results: tuple[str, ...] = ("implemented",)) -> None:
        self.results = list(results)
        self.launches = 0
        self.calls: list[dict] = []
        self._status = "implemented"

    def launch(self, request):  # noqa: ANN001 - test double
        prompt = request.prompt
        if request.no_tools or request.resume_session_id:
            match = re.search(r'"attempt":\s*(\d+)', prompt)
            attempt = int(match.group(1)) if match else 1
            text = json.dumps(
                {"role": "executor", "status": self._status,
                 "task_id": request.task_id, "attempt": attempt})
            write_text(request.report_path, text)
            return LaunchResult(exit_code=0, stdout=text, session_id="exec-sess")

        index = self.launches
        self.launches += 1
        status = self.results[index] if index < len(self.results) else self.results[-1]
        self.calls.append(
            {
                "role": request.role,
                "fresh_session": request.fresh_session,
                "resume": request.resume_session_id,
                "prompt": prompt,
                "is_repair": "Repair of:" in prompt,
            }
        )
        if status == "launch-fail":
            return LaunchResult(exit_code=1, stdout="launch failed", session_id="exec-sess")
        self._status = "implemented" if status == "implemented" else "blocked"
        write_text(
            request.report_path,
            f"# Executor report\n\n- Status: {self._status}\n\n## Files changed\n- none\n",
        )
        return LaunchResult(exit_code=0, stdout="", session_id="exec-sess")


class StubVerifier:
    """One verifier role. ``verdicts`` is the token this role returns per verification gate."""

    def __init__(self, verdicts: tuple[str, ...]) -> None:
        self.verdicts = list(verdicts)
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
            {
                "role": request.role,
                "is_env": is_env,
                "fresh_session": request.fresh_session,
                "read_only": request.read_only,
                "no_tools": request.no_tools,
                "prompt": request.prompt,
            }
        )
        if is_env:
            match = re.search(r'"attempt":\s*(\d+)', request.prompt)
            attempt = int(match.group(1)) if match else 1
            text = json.dumps(
                {"role": request.role, "verdict": verdict,
                 "task_id": request.task_id, "attempt": attempt})
        else:
            finding = "clean" if verdict == "PASS" else "defect: guard clause still missing"
            text = f"# {request.role}\n\n- Verdict: {verdict}\n\n- Findings: {finding}\n"
        write_text(request.report_path, text)
        return LaunchResult(exit_code=0, stdout=text, session_id="ver-sess")
