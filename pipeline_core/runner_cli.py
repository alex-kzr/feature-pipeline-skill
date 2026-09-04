"""Portable core runner CLI.

This is the project-independent entry point the compatibility launcher and every
acceptance gate wire to. It accepts explicit filesystem anchors and logical, anchor-relative
paths only; it never infers a project layout, and it carries no project identity.

This module is now only the argparse-facing shim: :func:`main` parses argv into a typed
:class:`~feature_pipeline.cli.commands.RunCommand`, dispatches it to
:func:`feature_pipeline.cli.use_cases.dispatch`, and prints the typed
:class:`~feature_pipeline.application.results.PipelineResult` it gets back. Every decision —
profile/plan resolution, task selection with dependency and lease gates, per-task routing
through the profile's closed registry, the fixed delivery-gate order (``plan``,
``final-diff``, ``commit``, verification verdict), the dry-run C1-C8 plan, and the stage
5-9 execute path — lives in :mod:`feature_pipeline.cli.use_cases` and the modules it calls;
none of it is decided here.

What the CLI surface does, end to end (see :mod:`feature_pipeline.cli` for the module split):

* resolves the profile through :mod:`pipeline_core.profiles` explicit-anchor resolution,
  rejecting absolute paths, ``~``, ``..`` and ``\\`` separators in the logical arguments;
* reads the plan as JSON, or - for a ``.md`` path - as a Markdown task table via
  :mod:`pipeline_core.plan_md`, so the core can be pointed at a real project's native plan;
* builds a run from profile data — task selection with dependency and lease gates, per-task
  routing through the profile's closed registry, generic non-executing stage construction, and
  the fixed delivery-gate order (``plan``, ``final-diff``, ``commit``, verification verdict);
* with ``--dry-run`` prints a deterministic, redacted plan (sections ``C1``–``C8`` of the
  parallel-acceptance comparison matrix) and writes nothing outside a temporary run directory;
  a non-blocked ``--dry-run`` exits ``10`` (a delivery gate is pending), matching the legacy
  runner and the MI-01 exit-code table;
* refuses ``--push`` before any run artifact exists, with the recorded baseline message and
  exit code, and never lets ``--commit`` / ``--commit-approved-manifest`` bypass the plan gate;
* accepts the legacy ``run`` operational surface the compatibility launcher forwards -
  implementing ``--approve-final-diff`` / ``--commit-approved-manifest`` / ``--status`` /
  ``--unattended`` and accepting the rest as documented no-ops (see ``scripts/README.md``);
* fails closed on ``design`` and unknown task types.

Standard library only.
"""

from __future__ import annotations

import sys
from typing import Sequence

from feature_pipeline.cli.commands import RunCommand
from feature_pipeline.cli.errors import CliError
from feature_pipeline.cli.parser import (
    DELIVERY_GATES,
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_GATE_PENDING,
    EXIT_MEANINGS,
    EXIT_OK,
    EXIT_PUSH_DENIED,
    POST_TASK_MODE,
    PUSH_DENIED_MESSAGE,
    build_parser,
)
from feature_pipeline.cli.use_cases import dispatch, make_execute_adapters
from feature_pipeline.bootstrap import redact_cli_error

# Re-exported for compatibility: every existing caller and test that reaches these through
# ``pipeline_core.runner_cli`` (the historical home of the whole CLI) keeps working unchanged.
__all__ = [
    "main",
    "build_parser",
    "make_execute_adapters",
    "CliError",
    "EXIT_OK",
    "EXIT_GATE_PENDING",
    "EXIT_BLOCKED",
    "EXIT_ERROR",
    "EXIT_PUSH_DENIED",
    "PUSH_DENIED_MESSAGE",
    "EXIT_MEANINGS",
    "DELIVERY_GATES",
    "POST_TASK_MODE",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = RunCommand.from_args(args)

    if command.push:
        # Refused before anything is resolved, loaded, or written.
        print(PUSH_DENIED_MESSAGE, file=sys.stderr)
        return EXIT_PUSH_DENIED

    try:
        result = dispatch(command)
    except CliError as exc:
        print(redact_cli_error(exc.message), file=sys.stderr)
        return exc.code

    print(result.message, end="")
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
