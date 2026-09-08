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
``--agents <json>`` + ``--agent <name>``      the role is defined *inline* and selected by name,
                                             so a launch never depends on an on-disk
                                             ``.claude/agents/<name>.md`` in the target project;
                                             the universal runner operates arbitrary projects
                                             that carry no pipeline agent files (RDS-06). An
                                             on-disk definition, when present, still wins.
``--setting-sources user,project``           a machine-local settings file cannot loosen
                                             the grant
``--strict-mcp-config`` (no ``--mcp-config``) no MCP server is reachable at all
``--add-dir <dir>``                          read/reach for a role launched into a subtree
``-r/--resume <id>``                         continue a healthy session
===========================================  =========================================

``run_subprocess()`` spawns through the shared
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner` (RS-01), which pins
``encoding="utf-8"`` (with ``errors="strict"``) on the ``subprocess.Popen`` call explicitly —
the CLI is a Node.js process that always speaks UTF-8 on its stdio pipes, but Python's own
``text=True`` mode falls back to ``locale.getpreferredencoding()`` for both directions when no
``encoding=`` is given, which is not UTF-8 on every host locale (RDS-11). Leaving it implicit
silently corrupts every non-ASCII character crossing the pipe in either direction.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from feature_pipeline.infrastructure.adapters.claude_launcher import ClaudeLauncher
from feature_pipeline.infrastructure.adapters.codex_launcher import CodexLauncher
from feature_pipeline.ports.process import ProcessError

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


#: The exact on-disk Claude CLI agent name for each normalized verifier role. The CLI's
#: ``--agent`` flag needs the hyphenated on-disk spelling; :func:`normalize_role`'s underscored
#: output exists only so ``"task-verifier"`` and ``"task_verifier"`` *compare* equal for an
#: internal table lookup — it must never be the value put on the wire (RDS-14).
_ON_DISK_VERIFIER_AGENTS: dict[str, str] = {
    "task_verifier": "task-verifier",
    "test_verifier": "test-verifier",
}

# https://code.claude.com/docs/en/sub-agents#built-in-subagents
_CLAUDE_BUILTIN_AGENTS = frozenset({
    "general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide",
})


def on_disk_agent_name(role: str) -> str:
    """The exact on-disk agent name the CLI's ``--agent`` flag expects for ``role``.

    A verifier role is canonicalized to its hyphenated on-disk spelling no matter which
    spelling the caller passed; every other role already names itself on disk (a
    ``custom-executor`` role, say, is spelled that way in both places) and is returned
    untouched. This is the single conversion
    point between :func:`normalize_role`'s internal underscored form and the ``--agent`` value
    — keep it the only one so an underscored spelling can never reach the CLI again (RDS-14).
    """
    return _ON_DISK_VERIFIER_AGENTS.get(normalize_role(role), role)


#: One-line, shell-free charter per pipeline role. It is only the ``--agents`` *definition* the
#: CLI needs so ``--agent <name>`` resolves without an on-disk file — the actual task
#: instructions still travel on stdin. Any ``*-executor`` role (``python-executor``,
#: ``rust-executor``, …) shares the executor charter; anything unrecognized gets the generic one.
_ROLE_CHARTER: dict[str, str] = {
    "executor": (
        "Feature-pipeline executor. Implement only the selected task's allowed scope, run the "
        "declared verification commands, and report the required status envelope. Do not verify "
        "your own work and do not tick acceptance checkboxes."
    ),
    "task_verifier": (
        "Feature-pipeline task verifier. Read-only: never edit files. Judge the task's "
        "acceptance criteria against the actual implementation and report a PASS or FAIL verdict."
    ),
    "test_verifier": (
        "Feature-pipeline test verifier. Read-only apart from running the declared checks. "
        "Confirm the recorded verification commands genuinely pass and report a PASS or FAIL "
        "verdict."
    ),
}
_GENERIC_CHARTER = (
    "Feature-pipeline worker. Follow the instructions provided on stdin, stay within the stated "
    "allowed scope and tool policy, and report the required envelope."
)


def _role_charter(role: str, *, read_only: bool) -> str:
    normalized = normalize_role(role)
    charter = _ROLE_CHARTER.get(normalized)
    if charter is None:
        charter = _ROLE_CHARTER["executor"] if normalized.endswith("executor") else _GENERIC_CHARTER
    if read_only and "read-only" not in charter.lower():
        charter += " This session is read-only. Never modify the working tree or push."
    return charter


def inline_agents_json(request: LaunchRequest, *, read_only: bool) -> str:
    """The ``--agents`` value: an inline definition of exactly the role being launched.

    Selected by the ``--agent <name>`` that follows it. This is what makes a launch independent
    of whatever ``.claude/agents`` files the target project happens to carry (RDS-06); the CLI
    still prefers an on-disk definition of the same name when one exists. Compact, ASCII-only,
    and free of shell metacharacters so :func:`_assert_shell_free` stays satisfied.
    """
    name = on_disk_agent_name(request.role)
    definition = {
        name: {
            "description": f"Feature-pipeline {name} role, defined inline for target-project independence",
            "prompt": _role_charter(request.role, read_only=read_only),
        }
    }
    return json.dumps(definition, ensure_ascii=True, separators=(",", ":"))


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


def scoped_add_dirs(
    request: LaunchRequest,
    scope_roots: Sequence[tuple[str, str | os.PathLike[str]]],
) -> tuple[str, ...]:
    """Return external write roots justified by this request's validated scope.

    ``--add-dir`` widens Codex's workspace-write sandbox, so it is deliberately unavailable to
    read-only roles.  Each configured root has a logical, project-relative prefix; only an
    allowed-scope entry at or below that prefix can select it.
    """
    if request_is_read_only(request) or not set(effective_grant(request)) & WRITE_CAPABILITIES:
        return ()
    granted: list[str] = []
    normalized_scope = tuple(entry.replace("\\", "/").strip("/") for entry in request.allowed_scope)
    for logical_root, physical_root in scope_roots:
        root = logical_root.replace("\\", "/").strip("/")
        if not root or root.startswith("../") or Path(root).is_absolute():
            raise AdapterError("external root has no safe logical scope", "unscoped-external-root")
        for entry in normalized_scope:
            if entry != root and not entry.startswith(f"{root}/"):
                continue
            suffix = entry.removeprefix(root).lstrip("/")
            parts = tuple(part for part in suffix.split("/") if part)
            wildcard = next(
                (index for index, part in enumerate(parts) if "*" in part or "?" in part),
                None,
            )
            safe_parts = parts[:wildcard] if wildcard is not None else parts[:-1]
            directory = str(Path(physical_root).joinpath(*safe_parts))
            if directory not in granted:
                granted.append(directory)
    return tuple(granted)


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

    argv += ["--agents", inline_agents_json(request, read_only=read_only)]
    argv += ["--agent", on_disk_agent_name(request.role)]

    for directory in add_dirs:
        argv += ["--add-dir", str(directory)]
    if request.resume_session_id:
        argv += ["--resume", str(request.resume_session_id)]

    _assert_shell_free(argv)
    _ = grant  # composed above so the verifier rule is enforced even for a no_tools launch
    return argv


def build_codex_argv(
    request: LaunchRequest,
    *,
    executable: str | Sequence[str] = "codex",
    working_root: str | os.PathLike[str] | None = None,
    add_dirs: Sequence[str | os.PathLike[str]] = (),
) -> list[str]:
    """Build the documented non-interactive ``codex exec`` argv for one launch."""
    grant = effective_grant(request)
    argv = _executable_prefix(executable) + ["exec"]
    # ``codex exec resume --help`` intentionally exposes no sandbox, working-directory, or
    # extra-directory flags. A runner status continuation is therefore a fresh, tool-free
    # request so its read-only sandbox and resolved grants are present on the actual argv.
    sandbox = "read-only" if request_is_read_only(request) else "workspace-write"
    argv += ["--json", "--sandbox", sandbox]
    if working_root is not None:
        argv += ["--cd", str(working_root)]
    for directory in add_dirs:
        argv += ["--add-dir", str(directory)]
    _assert_shell_free(argv)
    _ = grant
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


def parse_codex_session_id(stdout: str) -> str | None:
    """Return the thread id from Codex's documented JSONL event stream, if present."""
    for event in _result_objects(stdout):
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                return thread_id
    return None


def parse_codex_result_text(stdout: str) -> str | None:
    """Return the latest Codex JSONL ``agent_message`` text, never a fabricated value."""
    result: str | None = None
    for event in _result_objects(stdout):
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                result = text
    return result


@dataclass(frozen=True)
class CodexFinalResult:
    """One validated terminal result from a Codex JSONL launch."""

    status: str
    reason: str | None
    raw_payload: str


def _parse_codex_jsonl_events(stdout: str) -> list[dict]:
    """Parse a complete Codex JSONL stream without dropping malformed events."""
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        raise AdapterError("Codex final-result stream is empty", "result-protocol-invalid")
    events: list[dict] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise AdapterError("Codex JSONL stream contains malformed event", "result-protocol-invalid") from None
        if not isinstance(event, dict):
            raise AdapterError("Codex JSONL event is not an object", "result-protocol-invalid")
        events.append(event)
    return events


def parse_codex_final_result(stdout: str, *, task_id: str, attempt: int) -> CodexFinalResult:
    """Parse exactly one canonical executor final-result event from ordered Codex JSONL.

    Codex's human report is an artifact, not a protocol input.  The final agent message is
    therefore required to be the one JSON object specified by the executor prompt; selecting a
    "latest" message would make an earlier report or a later aside authoritative again.
    """
    events = _parse_codex_jsonl_events(stdout)
    messages: list[tuple[int, str]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                messages.append((index, text))
            else:
                raise AdapterError("Codex final-result event has no text payload", "result-protocol-invalid")
    if not messages:
        raise AdapterError(
            "Codex launch has no final-result agent message",
            "result-protocol-invalid",
        )
    candidates: list[tuple[int, str, dict]] = []
    for index, raw in messages:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if _is_codex_terminal_schema(payload):
            candidates.append((index, raw, payload))
    if len(candidates) != 1:
        raise AdapterError(
            f"Codex launch has {len(candidates)} terminal result payloads; expected exactly one",
            "result-protocol-invalid",
        )
    index, raw, payload = candidates[0]
    if index != messages[-1][0]:
        raise AdapterError(
            "Codex terminal result is not the final agent message",
            "result-protocol-invalid",
        )
    if not any(event.get("type") == "turn.completed" for event in events[index + 1:]):
        raise AdapterError(
            "Codex terminal result is not followed by turn.completed",
            "result-protocol-invalid",
        )
    status = payload.get("status")
    expected = {"role", "task_id", "attempt", "status"}
    if status == "blocked":
        expected.add("reason")
    if set(payload) != expected:
        raise AdapterError(
            f"Codex final-result keys {sorted(payload)}, expected exactly {sorted(expected)}",
            "result-protocol-invalid",
        )
    if payload.get("role") != "executor" or payload.get("task_id") != task_id:
        raise AdapterError("Codex final-result launch identity does not match", "result-protocol-invalid")
    if payload.get("attempt") != attempt or isinstance(payload.get("attempt"), bool):
        raise AdapterError("Codex final-result attempt does not match", "result-protocol-invalid")
    if status not in {"implemented", "blocked"}:
        raise AdapterError("Codex final-result status is invalid", "result-protocol-invalid")
    reason = payload.get("reason")
    if status == "blocked" and (not isinstance(reason, str) or not reason.strip()):
        raise AdapterError("Codex blocked final-result requires a non-empty reason", "result-protocol-invalid")
    return CodexFinalResult(status=status, reason=reason if isinstance(reason, str) else None, raw_payload=raw)


def _is_codex_terminal_schema(payload: object) -> bool:
    """Return whether a message has the terminal-result shape before identity validation."""
    if not isinstance(payload, dict):
        return False
    status = payload.get("status")
    expected = {"role", "task_id", "attempt", "status"}
    if status == "blocked":
        expected.add("reason")
    if status not in {"implemented", "blocked"} or set(payload) != expected:
        return False
    if not isinstance(payload.get("role"), str) or not isinstance(payload.get("task_id"), str):
        return False
    if not isinstance(payload.get("attempt"), int) or isinstance(payload.get("attempt"), bool):
        return False
    return status != "blocked" or isinstance(payload.get("reason"), str) and bool(payload["reason"].strip())


# --- process lifetime ------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletedProcess:
    """What a runner returns: the three facts a :class:`LaunchResult` is built from."""

    exit_code: int
    stdout: str
    stderr: str


def run_subprocess(
    argv: Sequence[str],
    *,
    prompt: str = "",
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> CompletedProcess:
    """Run ``argv`` with no shell through the one shared local process runner (RS-01).

    ``prompt`` is fed on stdin and both streams are captured. On timeout the whole process
    tree is terminated, any partial output is preserved, and the conventional
    :data:`EXIT_TIMEOUT` status is returned. A process that cannot be started at all is
    surfaced as :class:`AdapterError` ``adapter-unavailable``, exactly as before — the launch
    request/response contract is unchanged.
    """
    try:
        outcome = ClaudeLauncher().run(
            list(argv), prompt=prompt, cwd=cwd, timeout=timeout, env=env
        )
    except ProcessError as exc:
        raise AdapterError(
            f"could not start '{argv[0]}': {exc}", "adapter-unavailable"
        ) from None

    exit_code = EXIT_TIMEOUT if outcome.timed_out else (
        outcome.exit_code if outcome.exit_code is not None else 0
    )
    return CompletedProcess(exit_code, outcome.stdout, outcome.stderr)


def run_codex_subprocess(
    argv: Sequence[str],
    *,
    prompt: str = "",
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> CompletedProcess:
    """Run a Codex argv through its concrete launcher and map start failures consistently."""
    try:
        outcome = CodexLauncher().run(
            list(argv), prompt=prompt, cwd=cwd, timeout=timeout, env=env
        )
    except ProcessError as exc:
        raise AdapterError(
            f"could not start '{argv[0]}': {exc}", "adapter-unavailable"
        ) from None

    exit_code = EXIT_TIMEOUT if outcome.timed_out else (
        outcome.exit_code if outcome.exit_code is not None else 0
    )
    return CompletedProcess(exit_code, outcome.stdout, outcome.stderr)


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
        scope_roots: Sequence[tuple[str, str | os.PathLike[str]]] = (),
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        working_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._executable = executable
        self._resolver = resolver or (lambda: shutil.which("claude"))
        self._runner: ProcessRunner = runner or run_subprocess
        self._settings_path = settings_path
        self._scope_roots = tuple(scope_roots)
        self._env = env
        self._timeout = timeout
        self._working_root = Path(working_root) if working_root is not None else None

    def resolved_executable(self) -> str | Sequence[str] | None:
        if self._executable is not None:
            return self._executable
        return self._resolver()

    def available(self) -> bool:
        return self.resolved_executable() is not None

    def can_resolve_executor(self, role: str, *, working_root: str = ".") -> bool:
        """Resolve custom or enabled built-in agents before opening an executor window."""
        name = on_disk_agent_name(role)
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            return False
        directory = ((self._working_root or Path.cwd()) / working_root).resolve()
        roots = (directory, *directory.parents, Path.home())
        if any((root / ".claude" / "agents" / f"{name}.md").is_file() for root in roots):
            return True
        env = os.environ if self._env is None else self._env
        if env.get("CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS") == "1":
            return False
        if (name in {"Explore", "Plan"}
                and env.get("CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS") == "1"):
            return False
        return name in _CLAUDE_BUILTIN_AGENTS

    def plan(self, request: LaunchRequest) -> list[str]:
        """The argv this request would run — used by a dry run and by the tests, so what is
        reviewed is exactly what would execute."""
        executable = self.resolved_executable() or "claude"
        return build_claude_argv(
            request, executable=executable,
            settings_path=self._settings_path, add_dirs=self._add_dirs_for(request),
        )

    def launch(self, request: LaunchRequest) -> LaunchResult:
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError(
                "the Claude CLI is not available on PATH", "adapter-unavailable"
            )
        argv = build_claude_argv(
            request, executable=executable,
            settings_path=self._settings_path, add_dirs=self._add_dirs_for(request),
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

    def _add_dirs_for(self, request: LaunchRequest) -> tuple[str, ...]:
        return scoped_add_dirs(request, self._scope_roots)


class CodexAdapter:
    """Adapter over the non-interactive ``codex exec`` CLI."""

    name = "codex"
    # ``codex exec resume`` cannot carry the sandbox or resolved-directory flags required for
    # the runner's tool-free status continuation. The coordinator supplies only the parsed
    # report token to this fresh, read-only context.
    requires_fresh_envelope_context = True

    def __init__(
        self,
        *,
        executable: str | Sequence[str] | None = None,
        resolver: Callable[[], str | Sequence[str] | None] | None = None,
        runner: ProcessRunner | None = None,
        scope_roots: Sequence[tuple[str, str | os.PathLike[str]]] = (),
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        working_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._executable = executable
        self._resolver = resolver or (lambda: shutil.which("codex"))
        self._runner: ProcessRunner = runner or run_codex_subprocess
        self._scope_roots = tuple(scope_roots)
        self._env = env
        self._timeout = timeout
        self._working_root = Path(working_root) if working_root is not None else None

    def resolved_executable(self) -> str | Sequence[str] | None:
        return self._executable if self._executable is not None else self._resolver()

    def available(self) -> bool:
        return self.resolved_executable() is not None

    def plan(self, request: LaunchRequest) -> list[str]:
        executable = self.resolved_executable() or "codex"
        return build_codex_argv(
            request,
            executable=executable,
            working_root=self._cwd_for(request),
            add_dirs=self._add_dirs_for(request),
        )

    def launch(self, request: LaunchRequest) -> LaunchResult:
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError("the Codex CLI is not available on PATH", "adapter-unavailable")
        completed = self._runner(
            build_codex_argv(
                request,
                executable=executable,
                working_root=self._cwd_for(request),
                add_dirs=self._add_dirs_for(request),
            ),
            prompt=request.prompt,
            cwd=self._cwd_for(request),
            timeout=request.timeout or self._timeout,
            env=self._env,
        )
        text = parse_codex_result_text(completed.stdout)
        return LaunchResult(
            completed.exit_code,
            text if text is not None else completed.stdout,
            completed.stderr,
            parse_codex_session_id(completed.stdout) or request.resume_session_id,
            completed.stdout,
        )

    def _cwd_for(self, request: LaunchRequest) -> Path | None:
        working_root = request.working_root or "."
        if self._working_root is not None:
            return self._working_root / working_root
        if working_root != ".":
            return Path(working_root)
        return None

    def _add_dirs_for(self, request: LaunchRequest) -> tuple[str, ...]:
        return scoped_add_dirs(request, self._scope_roots)
