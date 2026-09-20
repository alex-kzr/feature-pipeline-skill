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

#: Strict worker-isolation tokens (R03 / TC-11). Each names one enforceable pre-launch
#: guarantee, bound to the selected runtime, its CLI surface and an observed version — never
#: assumed from ambient provider settings or prose. A token an adapter cannot structurally
#: enforce through its own launch surface stays declared ``False``; ``AdapterRegistry.require``
#: then rejects a strict-role launch before any process starts rather than silently launching
#: an unrestricted worker. Missing live proof of an isolation guarantee remains UNKNOWN — it is
#: never a derived/assumed capability.
BUNDLE_VALIDATED = "bundle_validated"
DISCOVERY_ISOLATED = "discovery_isolated"
SKILL_READS_ENFORCED = "skill_reads_enforced"
SUBPROCESS_ISOLATED = "subprocess_isolated"
NESTED_DELEGATION_ISOLATED = "nested_delegation_isolated"

#: Every strict-isolation token a role launch requires (R03). A strict role needs all of them;
#: an adapter missing even one must reject the launch rather than degrade to a partial or
#: read-only substitute.
STRICT_ISOLATION_CAPABILITIES: tuple[str, ...] = (
    BUNDLE_VALIDATED,
    DISCOVERY_ISOLATED,
    SKILL_READS_ENFORCED,
    SUBPROCESS_ISOLATED,
    NESTED_DELEGATION_ISOLATED,
)


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
class IsolationCapabilityProof:
    """One observed, adapter-bound proof for a strict isolation capability.

    A declared boolean describes a candidate launch surface.  It becomes usable only when
    this record ties the individual token to the observed runtime, CLI surface, version and
    retained probe evidence.  Empty or cross-adapter records are deliberately not proof.
    """

    capability: str
    adapter: str
    runtime: str
    cli_surface: str
    observed_version: str
    evidence: str

    def proves(
        self,
        capability: str,
        adapter: str,
        *,
        runtime: str,
        cli_surface: str,
        observed_version: str,
    ) -> bool:
        """Whether this proof binds ``capability`` to one observed launch surface."""
        return (
            self.capability == capability
            and self.adapter == adapter
            and self.runtime == runtime
            and self.cli_surface == cli_surface
            and self.observed_version == observed_version
            and all(value.strip() for value in (runtime, cli_surface, observed_version, self.evidence))
        )


@dataclass(frozen=True)
class AdapterCapabilities:
    """What one execution adapter can do, as declared data."""

    name: str
    available: bool
    supports_resume: bool
    supports_read_only: bool
    supports_write: bool
    default_timeout_s: float = DEFAULT_TIMEOUT_S
    #: Strict worker-isolation tokens (R03). Each defaults closed: an adapter must declare it
    #: explicitly, bound to its actual launch surface, before a strict-role launch may rely on
    #: it. See :data:`STRICT_ISOLATION_CAPABILITIES`.
    supports_bundle_validated: bool = False
    supports_discovery_isolated: bool = False
    supports_skill_reads_enforced: bool = False
    supports_subprocess_isolated: bool = False
    supports_nested_delegation_isolated: bool = False
    #: Observed proof records for strict tokens.  A strict boolean without its matching
    #: record remains unproved and therefore unusable.
    isolation_proofs: tuple[IsolationCapabilityProof, ...] = ()
    #: The observed runtime identity, concrete CLI surface, and version to which the proof is
    #: bound.  Empty defaults keep strict isolation unavailable.
    runtime: str = ""
    cli_surface: str = ""
    observed_version: str = ""

    def _has_isolation_proof(self, capability: str) -> bool:
        return any(
            proof.proves(
                capability,
                self.name,
                runtime=self.runtime,
                cli_surface=self.cli_surface,
                observed_version=self.observed_version,
            )
            for proof in self.isolation_proofs
        )

    def has(self, capability: str) -> bool:
        """Whether this adapter declares ``capability`` (:data:`RESUME` / :data:`READ_ONLY` /
        :data:`WRITE` / a :data:`STRICT_ISOLATION_CAPABILITIES` token). An unrecognised token
        is simply not held."""
        declared = {
            RESUME: self.supports_resume,
            READ_ONLY: self.supports_read_only,
            WRITE: self.supports_write,
            BUNDLE_VALIDATED: self.supports_bundle_validated,
            DISCOVERY_ISOLATED: self.supports_discovery_isolated,
            SKILL_READS_ENFORCED: self.supports_skill_reads_enforced,
            SUBPROCESS_ISOLATED: self.supports_subprocess_isolated,
            NESTED_DELEGATION_ISOLATED: self.supports_nested_delegation_isolated,
        }.get(capability, False)
        if capability in STRICT_ISOLATION_CAPABILITIES:
            return declared and self._has_isolation_proof(capability)
        return declared

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Every capability token this adapter holds, in a stable order."""
        return tuple(
            token for token in (READ_ONLY, RESUME, WRITE, *STRICT_ISOLATION_CAPABILITIES)
            if self.has(token)
        )


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
                    # Claude's `-p` argv structurally enforces these three: an inline
                    # `--agents`/`--agent` definition bound to one role (bundle_validated),
                    # `--strict-mcp-config` with `--setting-sources user,project` (no ambient
                    # plugin/user/project discovery beyond the pinned bundle:
                    # discovery_isolated), and embedded skill content plus scoped `--add-dir`
                    # reads instead of ambient file access (skill_reads_enforced).
                    #
                    # `subprocess_isolated` and `nested_delegation_isolated` stay declared
                    # `False`: TC-02-capabilities.md 1e marks nested-delegate/subprocess
                    # isolation UNKNOWN and live-unrun for both runtimes ("No field or code
                    # path constrains tools available to a nested delegate or a subprocess the
                    # session spawns") and requires an opt-in budgeted live probe (R03 / MO-14
                    # live matrix) before either token may be declared held. An
                    # `--allowed-tools`/`--disallowed-tools` Bash allowance list and the
                    # absence of an on-disk multi-agent definition are argv-construction
                    # reasoning only — not the required live proof — so neither token is
                    # asserted true from that reasoning alone (AC-4).
                    supports_bundle_validated=True,
                    supports_discovery_isolated=True,
                    supports_skill_reads_enforced=True,
                    supports_subprocess_isolated=False,
                    supports_nested_delegation_isolated=False,
                ),
                AdapterCapabilities(
                    CODEX,
                    available=False,
                    supports_resume=False,
                    supports_read_only=True,
                    supports_write=True,
                    # `codex exec` exposes no agent/role/bundle selection flag, no dedicated
                    # no-tools switch and no discovery-scoping flag (TC-02-capabilities.md
                    # 1e); it therefore cannot structurally back any strict-isolation token.
                    # A strict-role Codex launch fails closed with
                    # `stack-isolation-unsupported` rather than substituting the read-only
                    # sandbox for an unproven guarantee.
                    supports_bundle_validated=False,
                    supports_discovery_isolated=False,
                    supports_skill_reads_enforced=False,
                    supports_subprocess_isolated=False,
                    supports_nested_delegation_isolated=False,
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
        adapter = self.select(requested)
        if not adapter.available:
            choice = (requested or AUTO).strip().lower()
            if choice == AUTO:
                raise AdapterUnavailable(
                    "no execution adapter is available in this environment"
                )
            raise AdapterUnavailable(
                f"execution adapter '{choice}' is not available in this environment"
            )
        return adapter

    def select(self, requested: str | None) -> AdapterCapabilities:
        """Choose a declared adapter without requiring its executable to be available.

        Plan construction and argv rendering need a stable adapter identity, but do not launch
        it. Actual execution must use :meth:`resolve`, which performs the availability check.
        """
        choice = (requested or AUTO).strip().lower()
        if choice != AUTO:
            return self.get(choice)
        for adapter in self.adapters:
            if adapter.available:
                return adapter
        if self.adapters:
            return self.adapters[0]
        raise AdapterUnavailable("no execution adapter is registered")

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
    "BUNDLE_VALIDATED",
    "DISCOVERY_ISOLATED",
    "SKILL_READS_ENFORCED",
    "SUBPROCESS_ISOLATED",
    "NESTED_DELEGATION_ISOLATED",
    "STRICT_ISOLATION_CAPABILITIES",
    "AdapterError",
    "UnknownAdapter",
    "AdapterUnavailable",
    "IncompatibleAdapter",
    "AdapterCapabilities",
    "AdapterRegistry",
]
