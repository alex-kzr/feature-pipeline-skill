"""The migrated slice: a run's identity, built from injected time.

``pipeline_core.state.Run.create`` composes ``run_id`` as
``f"{_now().replace(':', '-')}-{feature}"`` by calling the module-level ``_now`` helper
directly — untestable without monkeypatching. :class:`RunIdentityFactory` is the DF-02
migration of that one slice onto the :class:`~feature_pipeline.ports.clock.Clock` port: the
feature slug is validated, time is injected, and the result is byte-identical to
``Run.create`` (AC-3).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from feature_pipeline.domain.identifiers import FeatureId
from feature_pipeline.ports.clock import Clock, format_timestamp


@dataclass(frozen=True)
class RunIdentity:
    """A run's identifier and its creation timestamp, both derived from one instant."""

    run_id: str
    created_at: str


class RunIdentityFactory:
    """Builds a :class:`RunIdentity` from an injected clock — no wall-clock call below it."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def create(self, feature: str | FeatureId) -> RunIdentity:
        """Mint the identity for ``feature``.

        ``feature`` is validated through :class:`FeatureId` (the same slug rule
        ``pipeline_core.runner_cli`` enforces). The timestamp is read once, so ``run_id`` and
        ``created_at`` always describe the same instant.
        """
        slug = feature if isinstance(feature, FeatureId) else FeatureId(feature)
        stamp = format_timestamp(self._clock.now())
        return RunIdentity(
            run_id=f"{stamp.replace(':', '-')}-{slug}",
            created_at=stamp,
        )


__all__ = ["RunIdentity", "RunIdentityFactory"]
