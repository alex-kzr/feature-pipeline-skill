"""Tool-neutral launch contracts and the concrete Claude adapter.

The adapter turns one :class:`LaunchRequest` into one shell-free ``claude -p`` argv, runs it
non-interactively, and returns the captured :class:`LaunchResult` — exit code, output, and the
session id the CLI reported. It returns *facts only*: it never writes run state, never writes a
report file, and never interprets an executor or verifier report. Choosing *which* adapter to
run is :mod:`pipeline_core.adapter_resolution`; composing a role's capability grant is
:mod:`pipeline_core.roles`.

Characterized against the installed Claude Code CLI (win32). Every flag used here was verified
present in that build's own ``--help``; nothing is used on the strength of a memory of the
documentation:

===========================================  =========================================
Used                                         Evidence
===========================================  =========================================
``-p/--print`` with the prompt on stdin      non-interactive mode; keeps a long envelope
                                             off a command line Windows would truncate
``--output-format json``                     one result object carrying ``session_id``
``--permission-mode {manual,acceptEdits}``   the permission posture; ``manual`` is the
                                             strictest mode that still lets a role finish
``--tools`` / ``--allowed-tools`` /          the tool policy — an empty ``--tools`` grants
``--disallowed-tools``                        nothing regardless of the role
``--setting-sources user,project``           a machine-local settings file cannot loosen
                                             the grant
``--strict-mcp-config`` (no ``--mcp-config``) no MCP server is reachable at all
``--add-dir <dir>``                          read/reach for a role launched into a subtree
``-r/--resume <id>``                         continue a healthy session
===========================================  =========================================

``run_subprocess()`` pins ``encoding="utf-8"`` (with ``errors="strict"``) on the
``subprocess.Popen`` call explicitly — the CLI is a Node.js process that always speaks UTF-8 on
its stdio pipes, but Python's own ``text=True`` mode falls back to
``locale.getpreferredencoding()`` for both directions when no ``encoding=`` is given, which is
not UTF-8 on every host locale (RDS-11). Leaving it implicit silently corrupts every non-ASCII
character crossing the pipe in either direction.

There is no per-directory *write* sandbox on this CLI, so a read-only launch is enforced
structurally: an effective grant with no write capability, a ``--tools`` set with no editing
tool, those same editing tools in ``--disallowed-tools``, ``git push`` always denied, and
``--permission-mode manual`` (a ``-p`` session has nobody to approve an out-of-policy tool
call). Executable discovery is injected so tests never touch a developer machine; the
installed-CLI smoke test is opt-in.

Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

#: Capabilities that let a role change the working tree. A read-only launch drops every one.
WRITE_CAPABILITIES = frozenset({"write", "create", "delete", "modify", "filesystem_write"})

#: Built-in tools that can modify the working tree. A read-only launch may name none of them.
WRITING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

#: Roles that are read-only by definition — a request for one that is not marked read-only is a
#: routing bug, not something to satisfy silently.
VERIFIER_ROLES = frozenset({"task_verifier", "test_verifier"})

PERMISSION_MODE_READ_ONLY = "manual"
PERMISSION_MODE_WRITE = "acceptEdits"

#: The push denial that must reach the CLI on every launch, not only the role prose.
PUSH_DENY_TOOL = "Bash(git push:*)"

#: Conventional timeout exit status. Non-zero, so a caller treats a timed-out launch as failed
#: and records a diagnostic before deciding any transition.
EXIT_TIMEOUT = 124

DEFAULT_TIMEOUT_S = 3600.0

_SHELL_METACHARACTERS = ("|", "&", ";", "<", ">", "`", "$(", "\n", "\r")


class AdapterError(RuntimeError):
    """A launch that cannot be built or started. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LaunchRequest:
    """One tool-independent request to run a pipeline role.

    The first five fields are the historical contract and keep their positions. The rest carry
    the execution context an adapter needs without naming any project: where to run, the role's
    composed capability grant, the task's allowed scope, whether the session is fresh or
    resumed, the tool policy, a timeout, and the envelope/report paths.
    """

    role: str
    task_id: str
    prompt: str
    report_path: Path
    read_only: bool = False
    working_root: str = "."
    role_grant: tuple[str, ...] = ()
    allowed_scope: tuple[str, ...] = ()
    resume_session_id: str | None = None
    fresh_session: bool = False
    tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    no_tools: bool = False
    timeout: float | None = None
    envelope_path: Path | None = None


@dataclass(frozen=True)
class LaunchResult:
    """The evidence returned by one adapter launch. Facts only — no interpretation.

    ``stdout`` is the assistant's actual text: for a ``--output-format json`` launch (every
    launch this adapter makes) that is the wrapper's own ``result`` field, extracted by
    :func:`parse_result_text` — never the raw CLI wrapper object those 20+ telemetry keys live
    in. ``raw_stdout`` keeps that untouched wrapper text for a human debugging a failed launch;
    nothing that parses a report/envelope/verdict should ever read it.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    session_id: str | None = None
    raw_stdout: str = ""


class Adapter(Protocol):
    """A CLI-specific implementation of a role launch."""

    def launch(self, request: LaunchRequest) -> LaunchResult:
        """Launch ``request`` and return its captured result."""


# --- role / request policy -------------------------------------------------------------------


def normalize_role(role: str) -> str:
    """``task-verifier`` and ``task_verifier`` name the same role; compare on this form."""
    return role.strip().lower().replace("-", "_")


def is_verifier_role(role: str) -> bool:
    return normalize_role(role) in VERIFIER_ROLES


def request_is_read_only(request: LaunchRequest) -> bool:
    """A request is read-only when it says so, or when its role is read-only by definition."""
    return bool(request.read_only) or is_verifier_role(request.role)


def effective_grant(request: LaunchRequest) -> tuple[str, ...]:
    """The capability grant actually in force: the most restrictive of the role and the request.

    A verifier request that is not marked read-only is rejected outright. A read-only request
    can never retain a write capability, even if the role grant or the adapter input asked for
    one (AC-2).
    """
    if is_verifier_role(request.role) and not request.read_only:
        raise AdapterError(
            f"verifier role '{request.role}' must be launched with a read-only request",
            "verifier-not-read-only",
        )
    grant = set(request.role_grant)
    if request_is_read_only(request):
        grant -= WRITE_CAPABILITIES
    return tuple(sorted(grant))


#: The concrete Claude CLI built-in tools each abstract read/run capability authorizes. ``write``
#: is not listed here — it maps to :data:`WRITING_TOOLS` verbatim rather than a second copy of
#: the editor names.
_READ_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
_RUN_CHECKS_TOOLS: tuple[str, ...] = ("Bash",)


def grant_tool_names(grant: Sequence[str]) -> tuple[str, ...]:
    """Turn an abstract capability grant into the concrete ``--tools`` names it needs.

    ``read`` -> ``Read, Glob, Grep``; ``run_checks`` -> ``Bash``; any write capability
    (:data:`WRITE_CAPABILITIES`) -> :data:`WRITING_TOOLS`. Order is stable and duplicates are
    dropped. A grant that names none of these yields ``()`` — the caller decides whether that
    is a deliberate tool-free launch or a wiring bug; it must never be turned into
    ``--tools ""`` for a role whose prompt promises tools (RDS-13).
    """
    caps = set(grant)
    names: list[str] = []
    if "read" in caps:
        names.extend(_READ_TOOLS)
    if "run_checks" in caps:
        names.extend(_RUN_CHECKS_TOOLS)
    if caps & WRITE_CAPABILITIES:
        names.extend(sorted(WRITING_TOOLS))
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


# --- argv ----------------------------------------------------------------------------------------


def _executable_prefix(executable: str | Sequence[str]) -> list[str]:
    if isinstance(executable, str):
        if not executable:
            raise AdapterError("empty adapter executable", "adapter-unavailable")
        return [executable]
    prefix = [str(part) for part in executable]
    if not prefix:
        raise AdapterError("empty adapter executable", "adapter-unavailable")
    return prefix


def _is_push_token(token: str) -> bool:
    return "push" in token.lower()


def _assert_shell_free(argv: Sequence[str]) -> None:
    for token in argv:
        for bad in _SHELL_METACHARACTERS:
            if bad in token:
                raise AdapterError(
                    f"argv token {token!r} contains the shell metacharacter {bad!r}; the adapter "
                    f"builds a shell-free argv only",
                    "shell-metacharacter",
                )


def build_claude_argv(
    request: LaunchRequest,
    *,
    executable: str | Sequence[str] = "claude",
    settings_path: str | os.PathLike[str] | None = None,
    add_dirs: Sequence[str | os.PathLike[str]] = (),
) -> list[str]:
    """One ``claude -p`` argv for one role and one task. The prompt travels on stdin.

    Builds the most restrictive combination of the role grant and the request, and refuses to
    widen a role: a read-only request that names a writing tool, or any request that
    pre-approves ``git push``, is an error rather than something to quietly drop.
    """
    read_only = request_is_read_only(request)

    if request.resume_session_id and request.fresh_session:
        raise AdapterError(
            f"role '{request.role}' requires a fresh session and may never be resumed",
            "resume-forbidden",
        )

    named_writers = sorted(WRITING_TOOLS.intersection(request.tools))
    if read_only and named_writers:
        raise AdapterError(
            f"read-only request for '{request.role}' names writing tool(s) "
            f"{', '.join(named_writers)}",
            "read-only-write-denied",
        )
    if any(_is_push_token(token) for token in request.allowed_tools):
        raise AdapterError(
            f"request for '{request.role}' pre-approves 'git push'; no role may push",
            "push-denied",
        )

    grant = effective_grant(request)  # validates the verifier read-only rule

    argv = _executable_prefix(executable)
    argv += ["-p", "--output-format", "json"]
    argv += ["--permission-mode", PERMISSION_MODE_READ_ONLY if read_only else PERMISSION_MODE_WRITE]
    argv += ["--setting-sources", "user,project"]
    if settings_path is not None:
        argv += ["--settings", str(settings_path)]
    argv += ["--strict-mcp-config"]

    if request.no_tools:
        # Zero tools, not zero-plus-exceptions: the role's own allow/deny lists are skipped.
        argv += ["--tools", ""]
    else:
        tools = tuple(
            tool for tool in request.tools if not (read_only and tool in WRITING_TOOLS)
        )
        argv += ["--tools", ",".join(tools)]
        if request.allowed_tools:
            argv += ["--allowed-tools", ",".join(request.allowed_tools)]
        disallowed = list(request.disallowed_tools)
        if read_only:
            for writer in sorted(WRITING_TOOLS):
                if writer not in disallowed:
                    disallowed.append(writer)
        if PUSH_DENY_TOOL not in disallowed:
            disallowed.append(PUSH_DENY_TOOL)
        argv += ["--disallowed-tools", ",".join(disallowed)]

    argv += ["--agent", request.role]

    for directory in add_dirs:
        argv += ["--add-dir", str(directory)]
    if request.resume_session_id:
        argv += ["--resume", str(request.resume_session_id)]

    _assert_shell_free(argv)
    _ = grant  # composed above so the verifier rule is enforced even for a no_tools launch
    return argv


# --- result parsing ----------------------------------------------------------------------------


def _result_objects(stdout: str) -> list[dict]:
    """Every JSON object in the output — one for ``--output-format json``, tolerant of a build
    that decorates stdout with a warning line or leaves a partial write after a kill."""
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        objects: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return objects
    return parsed if isinstance(parsed, list) else [parsed]


def parse_session_id(stdout: str) -> str | None:
    """The session id the CLI reported, so a healthy task can be resumed. Never invented."""
    for event in _result_objects(stdout):
        if isinstance(event, dict) and event.get("session_id"):
            return str(event["session_id"])
    return None


def parse_result_text(stdout: str) -> str | None:
    """The assistant's actual text — the wrapper's own ``result`` field — never invented.

    ``--output-format json`` wraps the real report/envelope text the agent wrote inside a
    ``result`` key alongside 20+ unrelated telemetry keys (``session_id``, ``usage``,
    ``modelUsage``, ``total_cost_usd``, …). Every downstream consumer that treats stdout as "the
    agent's report/envelope text" needs this inner string, not the wrapper. Mirrors
    :func:`parse_session_id`'s parse of the same objects; when more than one object carries a
    ``result`` string (a resumed continuation can emit more than one), the last one wins, since
    that is the most recent turn. Returns ``None`` — never an invented empty string — when no
    object carries a ``result`` string, e.g. stdout that is empty, plain prose, or a
    truncated/killed partial write; callers fail closed on that the same way
    :func:`_result_objects` already does for a non-JSON stream.
    """
    text_result: str | None = None
    for event in _result_objects(stdout):
        if isinstance(event, dict) and isinstance(event.get("result"), str):
            text_result = event["result"]
    return text_result


# --- process lifetime ------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletedProcess:
    """What a runner returns: the three facts a :class:`LaunchResult` is built from."""

    exit_code: int
    stdout: str
    stderr: str


def _terminate_tree(process: "subprocess.Popen") -> None:
    """Kill the whole process tree so no child or grandchild survives on Windows or POSIX."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_subprocess(
    argv: Sequence[str],
    *,
    prompt: str = "",
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> CompletedProcess:
    """Run ``argv`` with no shell, feeding ``prompt`` on stdin and capturing both streams.

    On timeout the whole process tree is terminated, any partial output is preserved, and the
    conventional :data:`EXIT_TIMEOUT` status is returned.
    """
    popen_kwargs: dict[str, object] = dict(
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=env,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(list(argv), **popen_kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        raise AdapterError(
            f"could not start '{argv[0]}': {exc}", "adapter-unavailable"
        ) from None

    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
        return CompletedProcess(process.returncode, stdout or "", stderr or "")
    except subprocess.TimeoutExpired:
        _terminate_tree(process)
        stdout, stderr = process.communicate()
        note = f"\n[adapter] timed out after {timeout}s; process tree terminated\n"
        return CompletedProcess(EXIT_TIMEOUT, stdout or "", (stderr or "") + note)


# --- the adapter -----------------------------------------------------------------------------


ProcessRunner = Callable[..., CompletedProcess]


class ClaudeAdapter:
    """:class:`Adapter` over the installed ``claude`` CLI.

    Everything host-specific is injected: ``executable`` (or a ``resolver`` callable that finds
    it), the process ``runner``, the settings path, extra reachable directories, and the
    environment. The adapter holds no reference to run state and writes nothing.
    """

    name = "claude"

    def __init__(
        self,
        *,
        executable: str | Sequence[str] | None = None,
        resolver: Callable[[], str | Sequence[str] | None] | None = None,
        runner: ProcessRunner | None = None,
        settings_path: str | os.PathLike[str] | None = None,
        add_dirs: Sequence[str | os.PathLike[str]] = (),
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        working_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._executable = executable
        self._resolver = resolver or (lambda: shutil.which("claude"))
        self._runner: ProcessRunner = runner or run_subprocess
        self._settings_path = settings_path
        self._add_dirs = tuple(add_dirs)
        self._env = env
        self._timeout = timeout
        self._working_root = Path(working_root) if working_root is not None else None

    def resolved_executable(self) -> str | Sequence[str] | None:
        if self._executable is not None:
            return self._executable
        return self._resolver()

    def available(self) -> bool:
        return self.resolved_executable() is not None

    def plan(self, request: LaunchRequest) -> list[str]:
        """The argv this request would run — used by a dry run and by the tests, so what is
        reviewed is exactly what would execute."""
        executable = self.resolved_executable() or "claude"
        return build_claude_argv(
            request, executable=executable,
            settings_path=self._settings_path, add_dirs=self._add_dirs,
        )

    def launch(self, request: LaunchRequest) -> LaunchResult:
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError(
                "the Claude CLI is not available on PATH", "adapter-unavailable"
            )
        argv = build_claude_argv(
            request, executable=executable,
            settings_path=self._settings_path, add_dirs=self._add_dirs,
        )
        completed = self._runner(
            argv,
            prompt=request.prompt,
            cwd=self._cwd_for(request),
            timeout=request.timeout or self._timeout,
            env=self._env,
        )
        extracted_text = parse_result_text(completed.stdout)
        return LaunchResult(
            exit_code=completed.exit_code,
            # Fail closed the same way the wrapper parse already does: a stream that never
            # yielded a `result` string (empty, plain text, a truncated/killed partial write)
            # is passed through unchanged rather than invented as empty.
            stdout=extracted_text if extracted_text is not None else completed.stdout,
            stderr=completed.stderr,
            session_id=parse_session_id(completed.stdout) or request.resume_session_id,
            raw_stdout=completed.stdout,
        )

    def _cwd_for(self, request: LaunchRequest) -> Path | None:
        working_root = request.working_root or "."
        if self._working_root is not None:
            return self._working_root / working_root
        if working_root != ".":
            return Path(working_root)
        return None
