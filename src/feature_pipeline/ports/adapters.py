"""The in-process execution-adapter registry (CP-01).

``pipeline_core`` chooses an execution adapter in :mod:`pipeline_core.adapter_resolution` by
probing an ``environment`` mapping, and separately describes each adapter's behaviour in prose
in :mod:`pipeline_core.adapters`. CP-01 lifts *what each adapter can do* into one declared,
in-process table so the plan compiler can resolve the adapter once and reject a task whose
requirements the resolved adapter cannot meet — all before any process is launched.

The registry is pure data: an adapter's availability is a declared field, never discovered by
inspecting the host from inside this module. A caller that knows a tool is missing constructs
a registry that says so.

Recognised names and the ``auto`` preference order mirror
:data:`pipeline_core.adapter_resolution.KNOWN_ADAPTERS`; availability is supplied by the
bootstrap composition root. The default timeout mirrors
:data:`pipeline_core.adapters.DEFAULT_TIMEOUT_S`.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from feature_pipeline.domain.errors import DomainError

#: Adapter names the core recognises, in ``auto`` preference order.
CLAUDE = "claude"
CODEX = "codex"
AUTO = "auto"
KNOWN_ADAPTERS = (CLAUDE, CODEX)

#: Mirrors :data:`pipeline_core.adapters.DEFAULT_TIMEOUT_S`.
DEFAULT_TIMEOUT_S = 3600.0

#: The capability tokens :class:`AdapterCapabilities` reports through :meth:`AdapterCapabilities.has`.
RESUME = "resume"
READ_ONLY = "read_only"
WRITE = "write"


class AdapterError(DomainError):
    """Base class for every fail-closed adapter-registry rejection."""

    code = "adapter-error"


class UnknownAdapter(AdapterError):
    """A name that is not a recognised execution adapter."""

    code = "unknown-adapter"


class AdapterUnavailable(AdapterError):
    """A recognised adapter that cannot run here — nothing is ever substituted for it."""

    code = "adapter-unavailable"


class IncompatibleAdapter(AdapterError):
    """The resolved adapter cannot satisfy a capability the plan requires."""

    code = "incompatible-adapter"


@dataclass(frozen=True)
class AdapterCapabilities:
    """What one execution adapter can do, as declared data."""

    name: str
    available: bool
    supports_resume: bool
    supports_read_only: bool
    supports_write: bool
    default_timeout_s: float = DEFAULT_TIMEOUT_S

    def has(self, capability: str) -> bool:
        """Whether this adapter declares ``capability`` (:data:`RESUME` / :data:`READ_ONLY` /
        :data:`WRITE`). An unrecognised token is simply not held."""
        return {
            RESUME: self.supports_resume,
            READ_ONLY: self.supports_read_only,
            WRITE: self.supports_write,
        }.get(capability, False)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Every capability token this adapter holds, in a stable order."""
        return tuple(token for token in (READ_ONLY, RESUME, WRITE) if self.has(token))


@dataclass(frozen=True)
class AdapterRegistry:
    """An ordered, immutable table of :class:`AdapterCapabilities`.

    ``adapters`` order is the ``auto`` preference order: the first available entry wins.
    """

    adapters: tuple[AdapterCapabilities, ...]

    @classmethod
    def default(cls) -> "AdapterRegistry":
        """The compatibility registry keeps Codex unavailable until a bootstrap supplies it."""
        return cls(
            (
                AdapterCapabilities(
                    CLAUDE,
                    available=True,
                    supports_resume=True,
                    supports_read_only=True,
                    supports_write=True,
                ),
                AdapterCapabilities(
                    CODEX,
                    available=False,
                    supports_resume=False,
                    supports_read_only=True,
                    supports_write=True,
                ),
            )
        )

    def _by_name(self) -> dict[str, AdapterCapabilities]:
        return {adapter.name: adapter for adapter in self.adapters}

    def get(self, name: str) -> AdapterCapabilities:
        """The declared capabilities for ``name``, or :class:`UnknownAdapter`."""
        try:
            return self._by_name()[name]
        except KeyError:
            raise UnknownAdapter(f"unknown execution adapter '{name}'") from None

    def resolve(self, requested: str | None) -> AdapterCapabilities:
        """Resolve ``requested`` (a concrete name, ``auto``, or ``None`` meaning ``auto``).

        ``auto`` returns the first available adapter in registry order. An explicit name that
        is unknown raises :class:`UnknownAdapter`; one that is known but unavailable raises
        :class:`AdapterUnavailable`. Nothing is ever substituted.
        """
        choice = (requested or AUTO).strip().lower()
        if choice == AUTO:
            for adapter in self.adapters:
                if adapter.available:
                    return adapter
            raise AdapterUnavailable(
                "no execution adapter is available in this environment"
            )
        adapter = self.get(choice)
        if not adapter.available:
            raise AdapterUnavailable(
                f"execution adapter '{choice}' is not available in this environment"
            )
        return adapter

    def require(
        self, name: str, capabilities: Iterable[str]
    ) -> AdapterCapabilities:
        """Return ``name``'s capabilities only if it declares every token in ``capabilities``;
        otherwise :class:`IncompatibleAdapter` naming the missing ones."""
        adapter = self.get(name)
        missing = tuple(token for token in capabilities if not adapter.has(token))
        if missing:
            raise IncompatibleAdapter(
                f"adapter '{name}' cannot satisfy required capabilities: "
                f"{', '.join(missing)}"
            )
        return adapter


__all__ = [
    "AUTO",
    "CLAUDE",
    "CODEX",
    "KNOWN_ADAPTERS",
    "DEFAULT_TIMEOUT_S",
    "RESUME",
    "READ_ONLY",
    "WRITE",
    "AdapterError",
    "UnknownAdapter",
    "AdapterUnavailable",
    "IncompatibleAdapter",
    "AdapterCapabilities",
    "AdapterRegistry",
]
