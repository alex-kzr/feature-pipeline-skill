"""Infrastructure adapters for the feature-pipeline core.

The infrastructure layer owns the mechanism the pure domain deliberately does not: durable
persistence, the filesystem, clocks bound to real time. Every module here depends *inward*
on :mod:`feature_pipeline.domain`, never the reverse.

Introduced by **DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``) with the
strict schema-v3 run-state boundary in :mod:`feature_pipeline.infrastructure.state`.
"""

from __future__ import annotations
