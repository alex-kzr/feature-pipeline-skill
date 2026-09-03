"""Application layer for the portable feature-pipeline core.

Use-case sequencing and the mapping from typed outcomes to stable CLI exit codes live here.
DF-01 introduces only :mod:`feature_pipeline.application.results`; later phases add the plan
compiler, run service, and pipeline engine (``docs/adr/002`` §5.4).
"""

from __future__ import annotations

from .results import Outcome, PipelineResult, ResultMapper

__all__ = ["Outcome", "PipelineResult", "ResultMapper"]
