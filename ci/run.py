"""UGA-03 — the portable, explicit-root CI gate driver.

Interfaces (each resolves every path below an explicit ``--source-root`` and
never from the ambient current directory):

    uv run python -m ci.run list --json [--group GROUP] [--source-root PATH]
    uv run python -m ci.run validate --source-root PATH
    uv run python -m ci.run run GATE_ID --source-root PATH \
        [--expected-source-sha SHA] [--workflow-repo R] [--workflow-sha S] \
        [--source-repo R]

``list --json`` emits deterministic JSON so a GitHub Actions workflow can build
its matrix without duplicating command definitions. ``run`` prints the workflow
repository, workflow SHA (when supplied), source repository, ``git rev-parse
HEAD`` source SHA, resolved root, gate ID, and required paths before executing a
gate; it returns the first command's nonzero exit code and runs nothing after a
failure. This module is CI tooling; the installable runtime never imports it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ci import contract
from ci import runner as _runner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci/run.py", description="Explicit-root CI gate driver."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="Emit the gate contract as deterministic JSON.")
    listing.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        required=True,
        help="Emit JSON (the only supported format).",
    )
    listing.add_argument("--group", choices=contract.WORKFLOW_GROUPS, default=None)
    listing.add_argument("--source-root", dest="source_root", default=None)

    validate = sub.add_parser(
        "validate", help="Validate an explicit source root and its gate contract."
    )
    validate.add_argument("--source-root", dest="source_root", required=True)

    execute = sub.add_parser("run", help="Run one gate below an explicit source root.")
    execute.add_argument("gate_id", metavar="GATE_ID")
    execute.add_argument("--source-root", dest="source_root", required=True)
    execute.add_argument("--expected-source-sha", dest="expected_source_sha", default=None)
    execute.add_argument("--workflow-repo", dest="workflow_repo", default=None)
    execute.add_argument("--workflow-sha", dest="workflow_sha", default=None)
    execute.add_argument("--source-repo", dest="source_repo", default=None)
    return parser


def _list(args: argparse.Namespace, out) -> int:
    if args.source_root is not None:
        loaded = _runner.load_contract(_runner.resolve_source_root(args.source_root))
    else:
        loaded = contract.load()
    print(_runner.list_json(loaded, args.group), end="", file=out)
    return 0


def _validate(args: argparse.Namespace, out) -> int:
    root = _runner.resolve_source_root(args.source_root)
    loaded = _runner.load_contract(root)
    print(f"resolved source root: {root}", file=out)
    print(
        f"gate contract: {len(loaded.ids())} gates ({', '.join(loaded.ids())})",
        file=out,
    )
    return 0


def _run(args: argparse.Namespace, out, runner) -> int:
    return _runner.run_gate(
        gate_id=args.gate_id,
        source_root=args.source_root,
        runner=runner,
        out=out,
        expected_source_sha=args.expected_source_sha,
        workflow_repo=args.workflow_repo,
        workflow_sha=args.workflow_sha,
        source_repo=args.source_repo,
    )


def main(argv: Sequence[str] | None = None, *, runner=None, stdout=None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    out = stdout if stdout is not None else sys.stdout
    command_runner = runner if runner is not None else _runner.SubprocessRunner()

    try:
        if args.command == "list":
            return _list(args, out)
        if args.command == "validate":
            return _validate(args, out)
        if args.command == "run":
            return _run(args, out, command_runner)
    except (_runner.DriverError, contract.ContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command!r}")  # unreachable
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
