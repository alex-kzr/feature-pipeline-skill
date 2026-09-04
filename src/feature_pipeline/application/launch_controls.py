"""Resolve every accepted launch control once, then apply it to each launch.

BL-02 finding F-07: output byte budgets and the serialized-program mutex were accepted as
CLI flags and persisted as controls, but never threaded into the command / adapter path that
actually runs a task's verification — ``run_command`` fell back to its hard-coded module
budgets and execute passed no serialized-program set or timeout policy. ADR 007 §4 requires
every accepted control to be validated, resolved, persisted, and applied — or explicitly
rejected at parse time with a named reason. No operational flag may be a silent no-op.

This module is that boundary. :func:`resolve_launch_controls` validates the raw control
mapping *before* any run state, lease, or process exists (ADR 007 §5): an unknown key is
rejected by name, a malformed value is rejected with its reason. :func:`plan_launch` then
turns the resolved controls plus one command into a :class:`ResolvedLaunch` that names the
concrete timeout, the concrete output budget for that launch kind, and the mutex program (if
any) — the single record a launcher applies. :func:`launch_scope` runs a launch under its
resolved mutex so the lock is released and the diagnostic record stays complete whether the
body returns or raises.

Wiring these into ``pipeline_core.execution`` / ``pipeline_core.commands`` is a later phase;
here the boundary is proven by tests, as with every typed layer before it.

Standard library only.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import ContextManager, Iterator, Literal, Mapping

from pipeline_core.commands import DIAGNOSTIC_OUTPUT_BUDGET, ROUTINE_OUTPUT_BUDGET

LaunchKind = Literal["routine", "diagnostic"]

#: The controls this boundary understands. A key outside this set is rejected by name rather
#: than silently ignored (ADR 007 §4).
SUPPORTED_CONTROLS: frozenset[str] = frozenset(
    {
        "command_timeout_s",
        "routine_output_byte_budget",
        "diagnostic_output_byte_budget",
        "serialized_programs",
    }
)


class UnsupportedControl(ValueError):
    """A control key outside :data:`SUPPORTED_CONTROLS`. ``key`` names the rejected control."""

    code = "unsupported-control"

    def __init__(self, key: str) -> None:
        super().__init__(
            f"launch control '{key}' is not supported; "
            f"supported controls are {sorted(SUPPORTED_CONTROLS)}"
        )
        self.key = key


class InvalidControl(ValueError):
    """A supported control whose value is malformed. ``key`` names it; the message says why."""

    code = "invalid-control"

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"launch control '{key}' is invalid: {reason}")
        self.key = key
        self.reason = reason


@dataclass(frozen=True)
class LaunchControls:
    """Every accepted launch control, validated and resolved to a concrete value.

    ``command_timeout_s`` is ``None`` only when the caller explicitly declined a timeout.
    ``sources`` records, per control, whether the value came from the caller (``"explicit"``)
    or a default (``"default"``) so a persisted run can be reasoned about on resume.
    """

    command_timeout_s: float | None = None
    routine_output_byte_budget: int = ROUTINE_OUTPUT_BUDGET
    diagnostic_output_byte_budget: int = DIAGNOSTIC_OUTPUT_BUDGET
    serialized_programs: tuple[str, ...] = ()
    sources: Mapping[str, str] = field(default_factory=dict)

    def budget_for(self, kind: LaunchKind) -> int:
        """The output byte budget that applies to a launch of ``kind``."""
        return (
            self.routine_output_byte_budget
            if kind == "routine"
            else self.diagnostic_output_byte_budget
        )

    def mutex_program(self, program_basename: str) -> str | None:
        """The serialized-program key ``program_basename`` matches, compared by lowercased
        stem (the same rule as ``pipeline_core.concurrency.is_serialized_program``), else
        ``None``."""
        stem = Path(program_basename).stem.lower()
        for declared in self.serialized_programs:
            if Path(declared).stem.lower() == stem:
                return declared
        return None

    def as_record(self) -> dict[str, object]:
        """A deterministic, JSON-ready record of every resolved control and its source."""
        return {
            "command_timeout_s": self.command_timeout_s,
            "routine_output_byte_budget": self.routine_output_byte_budget,
            "diagnostic_output_byte_budget": self.diagnostic_output_byte_budget,
            "serialized_programs": list(self.serialized_programs),
            "sources": dict(self.sources),
        }


def _as_positive_number(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidControl(key, f"expected a number, got {value!r}")
    number = float(value)
    if number <= 0:
        raise InvalidControl(key, f"must be greater than zero, got {number:g}")
    return number


def _as_non_negative_int(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidControl(key, f"expected an integer, got {value!r}")
    if value < 0:
        raise InvalidControl(key, f"must not be negative, got {value}")
    return value


def _as_program_tuple(key: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, frozenset, set)):
        raise InvalidControl(key, "expected a list of program basenames")
    programs: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise InvalidControl(key, f"program basename {entry!r} is not a non-empty string")
        if "/" in entry or "\\" in entry:
            raise InvalidControl(key, f"program basename {entry!r} must not contain a path")
        programs.append(entry)
    return tuple(programs)


def resolve_launch_controls(raw: Mapping[str, object]) -> LaunchControls:
    """Validate ``raw`` and resolve it to :class:`LaunchControls`.

    Raises :class:`UnsupportedControl` for a key outside :data:`SUPPORTED_CONTROLS` and
    :class:`InvalidControl` for a malformed value — both *before* any run state, lease, or
    process is created.
    """
    for key in raw:
        if key not in SUPPORTED_CONTROLS:
            raise UnsupportedControl(key)

    sources: dict[str, str] = {name: "default" for name in SUPPORTED_CONTROLS}
    resolved: dict[str, object] = {}

    if "command_timeout_s" in raw and raw["command_timeout_s"] is not None:
        resolved["command_timeout_s"] = _as_positive_number(
            "command_timeout_s", raw["command_timeout_s"]
        )
        sources["command_timeout_s"] = "explicit"
    elif "command_timeout_s" in raw:  # explicit ``None`` — the caller declined a timeout
        resolved["command_timeout_s"] = None
        sources["command_timeout_s"] = "explicit"

    for key in ("routine_output_byte_budget", "diagnostic_output_byte_budget"):
        if key in raw and raw[key] is not None:
            resolved[key] = _as_non_negative_int(key, raw[key])
            sources[key] = "explicit"

    if "serialized_programs" in raw and raw["serialized_programs"] is not None:
        resolved["serialized_programs"] = _as_program_tuple(
            "serialized_programs", raw["serialized_programs"]
        )
        sources["serialized_programs"] = "explicit"

    return LaunchControls(sources=sources, **resolved)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ResolvedLaunch:
    """One command bound to its resolved controls — the record a launcher applies verbatim."""

    argv: tuple[str, ...]
    cwd: str
    kind: LaunchKind
    timeout_s: float | None
    output_byte_budget: int
    mutex_program: str | None

    @property
    def is_serialized(self) -> bool:
        return self.mutex_program is not None


def plan_launch(
    controls: LaunchControls,
    *,
    argv: tuple[str, ...],
    cwd: str,
    program_basename: str,
    kind: LaunchKind,
) -> ResolvedLaunch:
    """Bind one command to ``controls``: the concrete timeout, the ``kind`` output budget, and
    the mutex program if ``program_basename`` is serialized."""
    if not argv:
        raise InvalidControl("argv", "a launch needs a non-empty argv")
    return ResolvedLaunch(
        argv=argv,
        cwd=cwd,
        kind=kind,
        timeout_s=controls.command_timeout_s,
        output_byte_budget=controls.budget_for(kind),
        mutex_program=controls.mutex_program(program_basename),
    )


@contextmanager
def launch_scope(
    launch: ResolvedLaunch,
    *,
    mutex: ContextManager[object] | None = None,
) -> Iterator[ResolvedLaunch]:
    """Run a launch under its resolved mutex.

    ``mutex`` is the cross-process write lock for ``launch.mutex_program`` when the launch is
    serialized (the caller supplies it — this module owns no lock file); a non-serialized
    launch needs none. The lock is released whether the body returns or raises, so resource
    cleanup and the diagnostic record stay complete on a policy denial or a launch failure
    (AC-3).
    """
    guard = mutex if (launch.is_serialized and mutex is not None) else nullcontext()
    with guard:
        yield launch
