"""Process-level plumbing for the concrete tool adapters (RS-01).

:class:`~feature_pipeline.infrastructure.adapters.claude_launcher.ClaudeLauncher` runs one
already-built ``claude`` argv through the shared
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner`. It carries **no**
CLI translation: argv construction (``build_claude_argv``) and result parsing
(``parse_result_text`` / ``parse_session_id``) stay in ``pipeline_core.adapters`` unchanged.

Standard library only.
"""

from __future__ import annotations

from .claude_launcher import ClaudeLauncher

__all__ = ["ClaudeLauncher"]
