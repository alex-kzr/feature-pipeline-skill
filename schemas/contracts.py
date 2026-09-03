"""Deprecated shim — the canonical module is :mod:`feature_pipeline.contracts`.

Re-exports every public name (classes, validators, and the module-level frozensets such as
``TASK_TYPES`` / ``DIFF_POLICIES`` / ``SCHEMA_VERSION``) as the *same* objects, so class
identity is preserved across both import spellings for the one deprecation window granted by
``docs/adr/007-compatibility-and-versioning-policy.md``. Removal is gated on DOC-02.
"""

from __future__ import annotations

from feature_pipeline.contracts import *  # noqa: F401,F403
from feature_pipeline.contracts import (  # noqa: F401  -- names not covered by `import *`
    DIFF_POLICIES,
    RUN_STATUSES,
    SCHEMA_VERSION,
    SHELL_TOKENS,
    SUBAGENTS,
    TASK_ID_RE,
    TASK_TYPES,
    VERDICTS,
    VERIFICATION_TIERS,
    WRITE_CAPABILITIES,
)
