"""Deterministic execution-adapter selection and fail-closed pinning.

:func:`resolve_adapter` chooses between ``claude``, ``codex``, and ``auto`` from an explicit
environment description — never by probing the developer machine from inside this module. The
choice is then pinned: :func:`pin_adapter` records ``controls.adapter_resolved`` on the run and
stamps every task, and :func:`ensure_pinned_adapter` rejects a resume that would silently
switch tools.

An unavailable explicit adapter fails closed with ``adapter-unavailable`` rather than falling
back to ``claude`` — no adapter is ever substituted for another.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import Run

#: Adapter names the core recognizes, in ``auto`` preference order.
KNOWN_ADAPTERS = ("claude", "codex")
AUTO = "auto"


class AdapterResolutionError(RuntimeError):
    """Adapter selection failed. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdapterResolution:
    """The outcome of one selection: what was asked, what it resolved to, and how."""

    requested: str  # 'claude' | 'codex' | 'auto', exactly as asked
    resolved: str   # a concrete adapter name
    sourced: str    # 'explicit' when the caller named it, 'default' when 'auto' picked it


def adapter_available(environment: Mapping[str, object], name: str) -> bool:
    """Whether ``name`` can run here according to the injected environment facts."""
    if name not in KNOWN_ADAPTERS:
        return False
    return bool(environment.get(name))


def resolve_adapter(
    requested: str | None, environment: Mapping[str, object]
) -> AdapterResolution:
    """Resolve ``requested`` (``claude`` / ``codex`` / ``auto`` / unset) against ``environment``.

    ``environment`` maps an adapter name to a truthy value when that adapter can run. ``auto``
    picks the first available adapter in :data:`KNOWN_ADAPTERS` order. An explicit adapter that
    is unknown or unavailable raises ``adapter-unavailable``; nothing else is substituted.
    """
    choice = (requested or AUTO).strip().lower()

    if choice == AUTO:
        for name in KNOWN_ADAPTERS:
            if adapter_available(environment, name):
                return AdapterResolution(AUTO, name, "default")
        raise AdapterResolutionError(
            "no execution adapter is available in this environment", "adapter-unavailable"
        )

    if choice not in KNOWN_ADAPTERS:
        raise AdapterResolutionError(
            f"unknown execution adapter '{choice}'", "adapter-unavailable"
        )
    if not adapter_available(environment, choice):
        raise AdapterResolutionError(
            f"execution adapter '{choice}' is not available in this environment",
            "adapter-unavailable",
        )
    return AdapterResolution(choice, choice, "explicit")


def pinned_adapter(run: "Run") -> str | None:
    """The adapter already pinned on ``run``, or ``None`` if this is a fresh run."""
    control = run.controls.get("adapter_resolved")
    if isinstance(control, Mapping):
        return control.get("value")  # type: ignore[return-value]
    return control


def pin_adapter(run: "Run", resolution: AdapterResolution) -> None:
    """Persist the resolved adapter globally and on every task for resume consistency (AC-4)."""
    run.set_control("adapter_requested", resolution.requested, sourced="explicit")
    run.set_control("adapter_resolved", resolution.resolved, sourced=resolution.sourced)
    for task in run.tasks.values():
        task.adapter = resolution.resolved


def ensure_pinned_adapter(run: "Run", resolution: AdapterResolution) -> str:
    """Fail a resume that would change the pinned adapter (AC-3); never substitute one.

    Returns the pinned adapter name. A fresh run (nothing pinned yet) passes through.
    """
    pinned = pinned_adapter(run)
    if pinned is not None and pinned != resolution.resolved:
        raise AdapterResolutionError(
            f"run is pinned to adapter '{pinned}' but resume resolved '{resolution.resolved}'; "
            f"the adapter is fixed for the life of a run",
            "adapter-switch",
        )
    return resolution.resolved
