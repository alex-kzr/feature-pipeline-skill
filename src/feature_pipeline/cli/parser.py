"""The argparse surface for the portable core runner, and nothing else.

``build_parser`` only declares flags, their help text, and their argparse-level validation
(choices, ``action="store_true"``, ``type=int``). It makes no selection, routing, control,
lifecycle, or exit-policy decision — those live in :mod:`feature_pipeline.cli.use_cases`.

The exit-code constants and the post-task mode token are declared here because they are part
of the same frozen compatibility surface as the flags themselves (``scripts/README.md`` and
the MI-01 exit-code table); every other module imports them from here rather than redeclaring
them.

Standard library only.
"""

from __future__ import annotations

import argparse
import re

# Process exit codes preserved from the baseline compatibility promise.
EXIT_OK = 0
EXIT_GATE_PENDING = 10
EXIT_BLOCKED = 20
EXIT_ERROR = 30
# The `--push` refusal keeps the exact legacy string and its dedicated exit code.
EXIT_PUSH_DENIED = 1
PUSH_DENIED_MESSAGE = "push is denied at this stage and the runner has no push code path."

DELIVERY_GATES = ("plan", "final-diff", "commit", "verification-verdict")

#: The ``--mode`` value that extends the plan-only dry run with the stage 10-16 post-task
#: lifecycle (documentation, its audit, the code-graph refresh + verification, final
#: verification, the human final-diff gate, and the release dry run). It always writes
#: nothing and always stops at the release dry-run boundary.
POST_TASK_MODE = "release-dry-run"

# One-line meaning of each exit code, for the C5 line of the dry-run plan.
EXIT_MEANINGS = {
    EXIT_OK: "ok",
    EXIT_GATE_PENDING: "a delivery gate is pending",
    EXIT_BLOCKED: "blocked",
    EXIT_ERROR: "error",
}

FEATURE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: A safe ``--attest-dependency`` ``SOURCE_FEATURE`` token: a bare directory name under the
#: profile's run-storage root — no path separator, ``.``, ``..``, or absolute/drive path.
#: ``execution._resolve_source_run_dir`` re-checks containment independently of this check.
SOURCE_FEATURE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Portable feature-pipeline core runner. Every path is an explicit anchor "
                    "or a logical path resolved below one; nothing is inferred.",
    )
    anchors = parser.add_argument_group("explicit anchors (filesystem roots)")
    anchors.add_argument("--project-root", metavar="DIR",
                         help="filesystem root the project's logical paths resolve below")
    anchors.add_argument("--agents-root", metavar="DIR",
                         help="filesystem root the shared agents/skills tree resolves below")
    anchors.add_argument("--core-root", metavar="DIR",
                         help="filesystem root this portable core resolves below")

    logical = parser.add_argument_group("logical paths (relative to an anchor)")
    logical.add_argument("--profile", metavar="REL",
                         help="profile file, relative to --project-root")
    logical.add_argument("--project-skill", metavar="REL",
                         help="project skill directory, relative to --agents-root")

    run = parser.add_argument_group("run selection and scope")
    run.add_argument("--plan", metavar="REL",
                     help="plan file, relative to the resolved project directory: JSON, or "
                          "Markdown (a '| ID | Type | Depends on |' task table) for a '.md' path")
    run.add_argument("--task", metavar="ID", help="run only this task, if its dependencies pass")
    run.add_argument("--through", metavar="ID",
                     help="select tasks from the first up to and including this one")
    run.add_argument("--attest-dependency", metavar="DEP_ID=SOURCE_FEATURE", action="append",
                     dest="attest_dependency",
                     help="--task only: treat DEP_ID (one of the task's own dependencies) as "
                          "satisfied because the already-closed run SOURCE_FEATURE verified it, "
                          "without dispatching it in this run. Repeatable; real control for "
                          "--mode execute and for --dry-run (both resolve and validate the "
                          "source run read-only), accepted as a no-op otherwise")
    run.add_argument("--resume", action="store_true",
                     help="resume a previously recorded run instead of starting one")
    run.add_argument("--mode", default="plan-only",
                     choices=["plan-only", "unattended", "execute", POST_TASK_MODE],
                     help="run mode (default: plan-only). 'execute' runs stages 5-9 — executor "
                          "dispatch, independent verification, and bounded repair — and stops "
                          "before documentation/delivery. 'release-dry-run' extends the "
                          "plan-only dry run with the post-task lifecycle (stages 10–16) and "
                          "its gates in the C4/C6 plan, then stops at the final-diff / release "
                          "dry-run boundary; it still writes nothing")
    run.add_argument("--feature", metavar="NAME", help="override the plan's feature name")
    run.add_argument("--prompt", metavar="REL",
                     help="prompt file, relative to the resolved project directory "
                          "(defaults to the plan file)")

    gates = parser.add_argument_group("delivery gates (all closed by default)")
    gates.add_argument("--approve-plan", action="store_true", help="satisfy the plan gate")
    gates.add_argument("--approve-final-diff", action="store_true",
                       help="satisfy the final-diff gate; never bypasses an earlier gate")
    gates.add_argument("--commit", action="store_true",
                       help="request the commit gate; it never bypasses the plan gate and this "
                            "runner has no commit code path")
    gates.add_argument("--commit-approved-manifest", action="store_true",
                       help="request the commit gate for an approved manifest; never bypasses "
                            "an earlier gate and this runner has no commit code path")
    gates.add_argument("--dry-run", action="store_true",
                       help="print the deterministic, redacted plan and write nothing outside a "
                            "temporary run directory")
    gates.add_argument("--push", action="store_true",
                       help="denied before any run artifact is created")

    # MI-01 core-classified operational flags the compatibility launcher forwards verbatim.
    # Each is either given a faithful minimal behaviour here or accepted as a documented
    # no-op ("not wired in the portable core; the project launcher owns this"). The bar:
    # a forwarded legacy invocation never dies with an argparse error inside the core.
    # scripts/README.md carries the per-flag decision table.
    compat = parser.add_argument_group(
        "compatibility surface (forwarded from the legacy CLI; see scripts/README.md)")
    compat.add_argument("--status", action="store_true",
                        help="print the resolved run state and exit")
    compat.add_argument("--unattended", action="store_true",
                        help="alias of --mode unattended")
    compat.add_argument("--adapter", metavar="NAME", choices=["claude", "codex", "auto"],
                        help="execution adapter for --mode execute (real control there); "
                             "accepted as a no-op in plan-only/unattended")
    compat.add_argument("--max-repair-attempts", metavar="N", type=int,
                        help="repair-loop bound for --mode execute (overrides each task's "
                             "declared bound); accepted as a no-op in plan-only/unattended")
    compat.add_argument("--routine-output-byte-budget", metavar="N", type=int,
                        help="recorded control for --mode execute; accepted as a no-op in "
                             "plan-only/unattended")
    compat.add_argument("--diagnostic-output-byte-budget", metavar="N", type=int,
                        help="recorded control for --mode execute; accepted as a no-op in "
                             "plan-only/unattended")
    compat.add_argument("--verbose", action="store_true",
                        help="accepted; reporter verbosity is owned by the project launcher")
    compat.add_argument("--quiet", action="store_true",
                        help="accepted; reporter verbosity is owned by the project launcher")
    return parser


__all__ = [
    "build_parser",
    "EXIT_OK",
    "EXIT_GATE_PENDING",
    "EXIT_BLOCKED",
    "EXIT_ERROR",
    "EXIT_PUSH_DENIED",
    "PUSH_DENIED_MESSAGE",
    "DELIVERY_GATES",
    "POST_TASK_MODE",
    "EXIT_MEANINGS",
    "FEATURE_RE",
    "SOURCE_FEATURE_RE",
]
