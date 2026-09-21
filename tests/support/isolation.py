"""Shared R03/TC-11 durable strict-isolation-probe test fixture."""

from __future__ import annotations

from feature_pipeline.ports.adapters import (
    AdapterCapabilities,
    IsolationCapabilityProof,
    STRICT_ISOLATION_CAPABILITIES,
)


def proven_isolation_capabilities(
    name: str, *, runtime: str | None = None, observed_version: str = "test-version",
) -> AdapterCapabilities:
    """Construct an explicit test-only stand-in for a separately verified proof.

    Production diagnostic records intentionally cannot create these capabilities.
    """
    selected_runtime = runtime or name
    cli_surface = f"{name} {'-p' if name == 'claude' else 'exec'}"
    evidence = f"test-only verified proof for {name} at {selected_runtime}"
    return AdapterCapabilities(
        name=name, available=True, supports_resume=name == "claude",
        supports_read_only=True, supports_write=True,
        supports_bundle_validated=True, supports_discovery_isolated=True,
        supports_skill_reads_enforced=True, supports_subprocess_isolated=True,
        supports_nested_delegation_isolated=True,
        isolation_proofs=tuple(
            IsolationCapabilityProof(
                token, name, selected_runtime, cli_surface, observed_version, evidence,
            ) for token in STRICT_ISOLATION_CAPABILITIES
        ),
        runtime=selected_runtime, cli_surface=cli_surface, observed_version=observed_version,
    )
