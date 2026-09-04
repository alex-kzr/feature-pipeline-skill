"""The thin CLI boundary: parsing, typed dispatch, and rendering.

This package holds every argparse-facing concern for the portable core runner, split by
responsibility so no single module mixes flag definitions with routing, gate, or lifecycle
decisions:

* :mod:`feature_pipeline.cli.parser` — the argparse surface and its frozen constants
  (exit codes, meanings, the delivery-gate names, the post-task mode token). Parsing only:
  it builds a parser and nothing else.
* :mod:`feature_pipeline.cli.errors` — :class:`~feature_pipeline.cli.errors.CliError`, the
  one fail-closed exception carrying a stable exit code and a leak-free message.
* :mod:`feature_pipeline.cli.commands` — :class:`~feature_pipeline.cli.commands.RunCommand`,
  the typed, ``argparse``-free view of a parsed invocation every use case consumes.
* :mod:`feature_pipeline.cli.use_cases` — status, dry-run, and execute, dispatched from one
  typed :func:`~feature_pipeline.cli.use_cases.dispatch`, each independent of rendering and
  returning a typed :class:`~feature_pipeline.application.results.PipelineResult`. No use
  case mutates domain state or launches a process itself; execution is delegated to
  :mod:`pipeline_core.execution`.
* :mod:`feature_pipeline.cli.renderers` — pure text rendering for a recorded run's status,
  the post-task lifecycle preview, and the non-dry-run gate summary.

:mod:`pipeline_core.runner_cli` remains the public entry point
(:func:`pipeline_core.runner_cli.main`); it now only parses argv, builds the typed command,
dispatches to a use case, and prints the rendered result.

Standard library only.
"""

from __future__ import annotations
