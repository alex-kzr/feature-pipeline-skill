"""Application layer for the portable feature-pipeline core.

Use-case sequencing and the mapping from typed outcomes to stable CLI exit codes live here.
DF-01 introduced :mod:`feature_pipeline.application.results`; DF-02 adds
:mod:`feature_pipeline.application.controls` (boundary control resolution) and
:mod:`feature_pipeline.application.identity` (the first clock-injected slice). Later phases
add the plan compiler, run service, and pipeline engine (``docs/adr/002`` §5.4).
"""

from __future__ import annotations

from .controls import ResolvedExecuteControls, resolve_execute_controls
from .identity import RunIdentity, RunIdentityFactory
from .results import Outcome, PipelineResult, ResultMapper

__all__ = [
    "Outcome",
    "PipelineResult",
    "ResultMapper",
    "ResolvedExecuteControls",
    "resolve_execute_controls",
    "RunIdentity",
    "RunIdentityFactory",
]
