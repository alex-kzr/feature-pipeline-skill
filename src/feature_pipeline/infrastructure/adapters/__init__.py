"""Process-level plumbing for the concrete tool adapters (RS-01).

:class:`~feature_pipeline.infrastructure.adapters.claude_launcher.ClaudeLauncher` and
:class:`~feature_pipeline.infrastructure.adapters.codex_launcher.CodexLauncher` run
already-built tool argv through the shared
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner`. It carries **no**
CLI translation: argv construction and result parsing stay in ``pipeline_core.adapters``.

Standard library only.
"""

from __future__ import annotations

from .claude_launcher import ClaudeLauncher
from .codex_launcher import CodexLauncher

__all__ = ["ClaudeLauncher", "CodexLauncher"]
