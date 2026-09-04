"""The concrete local operating-system process runner (RS-01).

:class:`~feature_pipeline.infrastructure.process.runner.LocalProcessRunner` is the single
implementation of :class:`feature_pipeline.ports.process.ProcessRunner`; both
``pipeline_core.adapters.run_subprocess`` and ``pipeline_core.commands.run_command`` spawn
through it. :mod:`feature_pipeline.infrastructure.process.capture` holds the byte-aware
diagnostic tail those call sites share.

Standard library only.
"""

from __future__ import annotations

from .capture import TRUNCATION_MARKER, tail_truncate
from .runner import KILL_GRACE_S, LocalProcessRunner, terminate_process_tree

__all__ = [
    "TRUNCATION_MARKER",
    "tail_truncate",
    "KILL_GRACE_S",
    "LocalProcessRunner",
    "terminate_process_tree",
]
