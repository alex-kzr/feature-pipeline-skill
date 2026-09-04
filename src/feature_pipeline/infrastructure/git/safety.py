"""Parsed read-only Git allowlist — safety by recognising a known operation, never by
listing dangerous spellings.

``pipeline_core.git_port`` historically denied a Git call only when ``argv[0]`` was an exact
denied subcommand or a denied flag appeared anywhere in argv (BL-02 finding F-06). A global
option before the subcommand (``git -C /elsewhere push``), an attached global option
(``git --git-dir=../other/.git push``), or an alias indirection (``git -c alias.x=push x``)
sailed straight through. ADR 007 §7 replaces that denylist with this parsed allowlist:

* the subcommand must be ``argv[0]`` itself — any leading ``-``/``--`` token is refused, so
  there is no position for a global option or a ``-c`` alias override to hide in;
* the subcommand must be one of a small fixed set of read-only operations;
* every remaining token must match that operation's argument schema — an unknown flag, a
  universally dangerous option (``-c``, ``--exec-path``, ``--output``, …), or a NUL byte is
  refused.

"The spelling was not recognised" is therefore never a reason a mutating call succeeds: an
unknown operation and an unknown flag both fail closed.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

#: Stable, machine-readable rejection reasons.
CODE_EMPTY_ARGV = "empty-argv"
CODE_LEADING_OPTION = "leading-option"
CODE_UNKNOWN_OPERATION = "unknown-operation"
CODE_DISALLOWED_ARGUMENT = "disallowed-argument"
CODE_MALFORMED_ARGUMENT = "malformed-argument"


class GitPolicyError(ValueError):
    """A Git argv rejected by the read-only allowlist. ``code`` is one of the ``CODE_*``
    constants; ``token`` is the offending argument when one applies."""

    def __init__(self, message: str, code: str, *, token: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.token = token


# --- argument schema ------------------------------------------------------------------------

#: Options that are dangerous under *every* operation — they redirect where Git reads its
#: config, which binary it execs, or where it writes output. Matched as an exact token or as
#: the ``--flag`` half of a ``--flag=value`` token.
_DANGEROUS_OPTIONS = frozenset(
    {
        "-c",
        "-C",
        "-o",
        "--output",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--upload-pack",
        "--receive-pack",
        "--open-files-in-pager",
        "--ext-diff",
    }
)


@dataclass(frozen=True)
class OperationSchema:
    """The argument schema for one read-only Git operation.

    ``flags`` are exact permitted tokens; ``valued`` are permitted ``--flag`` heads of a
    ``--flag=value`` token; ``allows_operand`` says whether bare tokens (a revision, a
    pathspec, or a ``--`` separator with the paths that follow it) are permitted.
    """

    flags: frozenset[str]
    valued: frozenset[str]
    allows_operand: bool


def _schema(
    *flags: str, valued: Sequence[str] = (), operands: bool = True
) -> OperationSchema:
    return OperationSchema(frozenset(flags), frozenset(valued), operands)


#: The complete set of Git operations the portable core may run. Anything not listed here
#: fails closed — there is no "unknown but probably harmless" path.
READ_ONLY_OPERATIONS: Mapping[str, OperationSchema] = {
    "rev-parse": _schema(
        "--is-inside-work-tree",
        "--is-bare-repository",
        "--is-inside-git-dir",
        "--show-toplevel",
        "--show-prefix",
        "--show-cdup",
        "--absolute-git-dir",
        "--abbrev-ref",
        "--symbolic-full-name",
        "--verify",
        "--quiet",
        "-q",
        "--short",
        "--all",
    ),
    "ls-files": _schema(
        "-z",
        "-t",
        "-s",
        "--stage",
        "--cached",
        "--others",
        "--deleted",
        "--modified",
        "--exclude-standard",
        "--error-unmatch",
        "--full-name",
        "--directory",
        "--no-empty-directory",
    ),
    "status": _schema(
        "--porcelain",
        "--short",
        "-s",
        "--branch",
        "-b",
        "-z",
        "--untracked-files",
        "-uno",
        "-uall",
        "--no-renames",
        "--ignored",
        "--long",
        valued=("--porcelain", "--untracked-files", "--ignored"),
    ),
    "diff": _schema(
        "--stat",
        "--numstat",
        "--shortstat",
        "--summary",
        "--name-only",
        "--name-status",
        "--raw",
        "--cached",
        "--staged",
        "-p",
        "-R",
        "--no-color",
        "--no-ext-diff",
        "--find-renames",
        "-M",
        "--find-copies",
        "--",
        valued=("--stat", "--find-renames", "--unified", "-U", "--diff-filter"),
    ),
    "show": _schema(
        "--stat",
        "--name-only",
        "--name-status",
        "--no-color",
        "-s",
        "--no-patch",
        "--",
        valued=("--format", "--pretty", "--stat"),
    ),
    "log": _schema(
        "--oneline",
        "--stat",
        "--name-only",
        "--name-status",
        "--no-color",
        "--graph",
        "--decorate",
        "--reverse",
        "--no-merges",
        "--first-parent",
        "-p",
        "--",
        valued=(
            "--max-count",
            "-n",
            "--format",
            "--pretty",
            "--date",
            "--since",
            "--until",
        ),
    ),
    "rev-list": _schema(
        "--count",
        "--all",
        "--branches",
        "--tags",
        "--no-merges",
        "--first-parent",
        valued=("--max-count", "-n"),
    ),
    "for-each-ref": _schema(
        valued=("--format", "--count", "--sort", "--points-at"), operands=False
    ),
    "merge-base": _schema("--is-ancestor", "--fork-point", "--all", "--independent"),
    "describe": _schema(
        "--tags", "--always", "--long", "--dirty", "--all", valued=("--abbrev", "--match")
    ),
}


@dataclass(frozen=True)
class ParsedGitCommand:
    """A Git argv proven to be a known read-only operation. ``argv`` is the input token
    sequence unchanged — the parser recognises, it never rewrites or re-quotes."""

    operation: str
    argv: tuple[str, ...]


def _reject(message: str, code: str, *, token: str | None = None) -> GitPolicyError:
    return GitPolicyError(message, code, token=token)


def _option_head(token: str) -> str:
    """The ``--flag`` part of ``--flag=value``; the token itself otherwise."""
    return token.split("=", 1)[0] if token.startswith("--") and "=" in token else token


def parse_read_only_git(argv: Sequence[str]) -> ParsedGitCommand:
    """Parse ``argv`` (the tokens *after* ``git``) into a :class:`ParsedGitCommand`, or raise
    :class:`GitPolicyError` with a stable ``code``.

    Fails closed on: an empty argv, any leading option token (no global-option or ``-c`` alias
    position), an operation outside :data:`READ_ONLY_OPERATIONS`, a universally dangerous
    option, an unknown flag for the operation, or a NUL byte in any token.
    """
    tokens = [str(token) for token in argv]
    if not tokens:
        raise _reject("git argv must not be empty", CODE_EMPTY_ARGV)

    for token in tokens:
        if "\x00" in token:
            raise _reject("git argv token contains a NUL byte", CODE_MALFORMED_ARGUMENT,
                          token=token)

    operation = tokens[0]
    if operation.startswith("-"):
        raise _reject(
            f"git subcommand must be the first token; '{operation}' is an option — no "
            "global-option or alias position is permitted",
            CODE_LEADING_OPTION,
            token=operation,
        )
    schema = READ_ONLY_OPERATIONS.get(operation)
    if schema is None:
        raise _reject(
            f"git operation '{operation}' is not in the read-only allowlist",
            CODE_UNKNOWN_OPERATION,
            token=operation,
        )

    operands_open = False
    for token in tokens[1:]:
        if operands_open:
            continue  # everything after ``--`` is a pathspec; already NUL-checked
        if token == "--":
            if not schema.allows_operand:
                raise _reject(
                    f"git {operation} does not take path operands",
                    CODE_DISALLOWED_ARGUMENT,
                    token=token,
                )
            operands_open = True
            continue
        if not token.startswith("-"):
            if not schema.allows_operand:
                raise _reject(
                    f"git {operation} does not take the operand '{token}'",
                    CODE_DISALLOWED_ARGUMENT,
                    token=token,
                )
            continue
        head = _option_head(token)
        if head in _DANGEROUS_OPTIONS:
            raise _reject(
                f"git option '{head}' is denied under every operation",
                CODE_DISALLOWED_ARGUMENT,
                token=token,
            )
        if token in schema.flags:
            continue
        if "=" in token and head in schema.valued:
            continue
        raise _reject(
            f"git {operation} does not accept the option '{token}'",
            CODE_DISALLOWED_ARGUMENT,
            token=token,
        )

    return ParsedGitCommand(operation=operation, argv=tuple(tokens))
