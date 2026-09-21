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

import ast
import hashlib
import fnmatch
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from feature_pipeline.infrastructure.adapters.claude_launcher import ClaudeLauncher
from feature_pipeline.infrastructure.adapters.codex_launcher import CodexLauncher
from feature_pipeline.ports.adapters import (
    AdapterCapabilities,
    IsolationCapabilityProof,
    STRICT_ISOLATION_CAPABILITIES,
)
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
class LaunchComposition:
    """The runner-owned canonical values from which a launch request was composed.

    The adapter compares the mutable-looking request fields with this immutable record before
    it resolves an executable or calls a process runner.  It is intentionally carried on every
    request in a composed launch, including tool-free continuation envelopes, so a later layer
    cannot substitute a same-shaped digest, widen scope, or add grants.
    """

    recipient_role: str
    bundle_digest: str | None
    allowed_scope: tuple[str, ...]
    role_grant: tuple[str, ...]
    #: The concrete executor selected by task routing.  This stays separate from the semantic
    #: bundle recipient ``executor`` so a valid routed identity is not treated as a verifier.
    executor_identity: str | None = None
    request_digest: str | None = None


#: Stable, machine-readable reason for a strict-role launch an adapter cannot structurally
#: back (R03/TC-11). This is a fail-closed rejection, never a silent downgrade to an
#: unrestricted or read-only-substitute launch (Requirements: "Return
#: stack-isolation-unsupported for incapable surfaces...").
STACK_ISOLATION_UNSUPPORTED = "stack-isolation-unsupported"

#: Stable, machine-readable reason for a request whose bound recipient role or bundle digest
#: disagrees with the role actually being launched — role or bundle substitution.
ROLE_BUNDLE_SUBSTITUTION = "role-bundle-substitution"

#: Claude's ``-p`` argv structurally backs three tokens (see
#: :data:`feature_pipeline.ports.adapters.AdapterRegistry.default`'s Claude entry for the
#: exact flag-by-flag rationale for ``bundle_validated``/``discovery_isolated``/
#: ``skill_reads_enforced``). It does *not* structurally back ``subprocess_isolated`` or
#: ``nested_delegation_isolated``: ``docs/validation/task-model-routing/TC-02-capabilities.md``
#: marks nested-delegate/subprocess isolation UNKNOWN and live-unrun for both runtimes ("No
#: field or code path constrains tools available to a nested delegate or a subprocess the
#: session spawns") and its §1e disposition requires an opt-in budgeted live probe (R03 /
#: MO-14 live matrix) before either token may be declared held; UNKNOWN never counts as PASS
#: (AC-4). Declared once here so every :class:`ClaudeAdapter` instance launches against the
#: same evidence-bound default unless a caller supplies an explicit, separately budgeted
#: live-probe measurement that proves those two tokens for the observed CLI version.
CLAUDE_ISOLATION_CAPABILITIES = AdapterCapabilities(
    "claude",
    available=True,
    supports_resume=True,
    supports_read_only=True,
    supports_write=True,
    supports_bundle_validated=True,
    supports_discovery_isolated=True,
    supports_skill_reads_enforced=True,
    supports_subprocess_isolated=False,
    supports_nested_delegation_isolated=False,
)

#: ``codex exec`` has no agent/role/bundle flag, no dedicated no-tools switch and no
#: discovery-scoping flag (TC-02-capabilities.md 1e), so it cannot structurally back any
#: strict-isolation token. Every token defaults closed; a strict-role Codex launch fails
#: :data:`STACK_ISOLATION_UNSUPPORTED` rather than falling back to the read-only sandbox.
CODEX_ISOLATION_CAPABILITIES = AdapterCapabilities(
    "codex",
    available=True,
    supports_resume=False,
    supports_read_only=True,
    supports_write=True,
    supports_bundle_validated=False,
    supports_discovery_isolated=False,
    supports_skill_reads_enforced=False,
    supports_subprocess_isolated=False,
    supports_nested_delegation_isolated=False,
)


def _runtime_identity(executable: str | Sequence[str]) -> str:
    """Return the exact selected executable command as a stable proof identity."""
    return executable if isinstance(executable, str) else "\0".join(executable)


def require_strict_isolation(
    capabilities: AdapterCapabilities,
    *,
    role: str,
    executable: str | Sequence[str],
    cli_surface: str,
) -> None:
    """Fail closed before any process starts unless every strict-isolation token holds.

    A strict role launch (executor, task-verifier, tool-free test-verifier) requires all of
    :data:`~feature_pipeline.ports.adapters.STRICT_ISOLATION_CAPABILITIES`. Missing live proof
    of a guarantee is UNKNOWN — it is never treated as a derived or assumed capability, so a
    declared-``False`` token here always rejects rather than silently launching an
    unrestricted worker.
    """
    runtime_matches = capabilities.runtime == _runtime_identity(executable)
    surface_matches = capabilities.cli_surface == cli_surface
    missing = [token for token in STRICT_ISOLATION_CAPABILITIES if not capabilities.has(token)]
    if not runtime_matches or not surface_matches:
        missing = list(STRICT_ISOLATION_CAPABILITIES)
    if missing:
        raise AdapterError(
            f"adapter {capabilities.name!r} cannot enforce strict isolation for role "
            f"{role!r}; missing/unproven: {', '.join(missing)}",
            STACK_ISOLATION_UNSUPPORTED,
        )


def _assert_bundle_identity(request: "LaunchRequest") -> None:
    """Fail closed on a request whose bound recipient role or bundle digest is inconsistent.

    ``recipient_role``/``bundle_digest`` are set once, by the runner, from the exact skill
    bundle resolved for the role actually being launched (AC-1). A request carrying a
    recipient role that disagrees with the launched role, or a malformed digest, is role or
    bundle substitution and must never reach a process.
    """
    composition = request.composition
    if composition is not None:
        if (composition.request_digest is not None
                and composition.request_digest != _request_security_digest(request)):
            raise AdapterError(
                "request differs from its bound production composition",
                ROLE_BUNDLE_SUBSTITUTION,
            )
        if request.recipient_role != composition.recipient_role:
            raise AdapterError(
                "request recipient role does not match its canonical composition",
                ROLE_BUNDLE_SUBSTITUTION,
            )
        if request.bundle_digest != composition.bundle_digest:
            raise AdapterError(
                "request bundle digest does not match its canonical composition",
                ROLE_BUNDLE_SUBSTITUTION,
            )
        if not set(request.allowed_scope).issubset(composition.allowed_scope):
            raise AdapterError(
                "request allowed scope widens its canonical composition",
                ROLE_BUNDLE_SUBSTITUTION,
            )
        if not set(effective_grant(request)).issubset(composition.role_grant):
            raise AdapterError(
                "request grants widen its canonical composition",
                ROLE_BUNDLE_SUBSTITUTION,
            )
        permitted_tools = set(grant_tool_names(effective_grant(request)))
        requested_tools = set(request.tools) | {
            tool.split("(", 1)[0] for tool in request.allowed_tools
        }
        if not requested_tools.issubset(permitted_tools):
            raise AdapterError(
                "request tools exceed its effective canonical grant",
                ROLE_BUNDLE_SUBSTITUTION,
            )
    if request.recipient_role is not None:
        wanted = normalize_role(request.recipient_role)
        launched = normalize_role(request.role)
        # The generic "executor" bundle recipient category is compatible with any concrete
        # ``*-executor`` role (``python-executor``, ``rust-executor``, ...) — bundles are
        # composed for the semantic executor category, never a specific stack's agent name —
        # but never with a verifier or any other role.
        selected_executor = composition is not None and composition.executor_identity is not None
        identity_matches = selected_executor and normalize_role(composition.executor_identity) == launched
        compatible = wanted == launched or (wanted == "executor" and not is_verifier_role(request.role) and (
            identity_matches or (not selected_executor and is_executor_role(request.role))
        ))
        if wanted == "executor" and selected_executor:
            compatible = identity_matches and not is_verifier_role(request.role)
        if not compatible:
            raise AdapterError(
                f"request recipient role {request.recipient_role!r} does not match launched "
                f"role {request.role!r}",
                ROLE_BUNDLE_SUBSTITUTION,
            )
    if request.bundle_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", request.bundle_digest):
        raise AdapterError(
            f"bundle digest {request.bundle_digest!r} is not a valid sha256 hex digest",
            ROLE_BUNDLE_SUBSTITUTION,
        )


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
    #: Physical directories a worker must be able to read for its declared task / plan /
    #: prompt / required-skill inputs (REC-05). Deliberately distinct from ``allowed_scope``:
    #: lowering these to ``--add-dir`` never contributes a write capability, a scoped write
    #: root, or an ``effective_grant`` entry.
    required_input_dirs: tuple[str, ...] = ()
    #: Explicit per-run runtime selection (REC-06). These values come from the typed
    #: execution controls; adapters never infer them from prompt prose or configuration.
    model: str | None = None
    effort: str | None = None
    #: The role this launch's resolved skill bundle/context was actually composed for (AC-1).
    #: ``None`` when no bundle applies (e.g. a project without a configured profile). Set once
    #: by the runner at request-composition time; an adapter never infers or widens it.
    recipient_role: str | None = None
    #: The lowercase sha256 hex digest of the skill bundle resolved for ``recipient_role``
    #: (:meth:`feature_pipeline.application.skill_bundles.ResolvedSkillBundle.digest`), or
    #: ``None`` when no bundle applies. Immutable evidence binding the launched request to the
    #: exact reviewed skill content it was composed with.
    bundle_digest: str | None = None
    #: Canonical values selected by the runner while resolving the role's actual bundle.  A
    #: composed strict launch must retain this binding through every adapter boundary.
    composition: LaunchComposition | None = None


def _request_security_digest(request: LaunchRequest) -> str:
    """Bind the actual context and authority; prose never establishes isolation."""
    composition = request.composition
    canonical_composition = None if composition is None else (
        composition.recipient_role, composition.bundle_digest, composition.allowed_scope,
        composition.role_grant, composition.executor_identity,
    )
    values = (
        request.role, request.task_id, request.prompt, request.working_root,
        str(request.report_path),
        None if request.envelope_path is None else str(request.envelope_path),
        request.recipient_role, request.bundle_digest, request.allowed_scope,
        request.role_grant, request.read_only, request.no_tools,
        request.tools, request.allowed_tools, request.disallowed_tools,
        request.required_input_dirs, request.fresh_session, request.resume_session_id,
        canonical_composition,
    )
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode("utf-8")).hexdigest()


def bind_launch_request(request: LaunchRequest) -> LaunchRequest:
    """Seal one runner-composed request before handing it to an adapter.

    A continuation is composed and sealed separately with its narrowed authority.
    Rebinding an already sealed request cannot bless a later substitution.
    """
    _assert_bundle_identity(request)
    if request.composition is None:
        raise AdapterError("canonical composition is required", ROLE_BUNDLE_SUBSTITUTION)
    return replace(request, composition=replace(
        request.composition, request_digest=_request_security_digest(request),
    ))


@dataclass(frozen=True)
class LiveProbeRequest:
    """One runner-owned containment observation, distinct from every pipeline role launch."""

    task_id: str
    prompt: str
    report_path: Path
    allowed_scope: tuple[str, ...]
    timeout: float

    def as_launch_request(self, *, no_tools: bool) -> LaunchRequest:
        """Lower the fixed probe contract without accepting an executor/verifier identity."""
        if (not self.task_id or not self.prompt
                or not isinstance(self.timeout, (int, float))
                or isinstance(self.timeout, bool) or not math.isfinite(self.timeout)
                or self.timeout <= 0):
            raise AdapterError("runner-owned live probe request is invalid", "live-probe-invalid")
        return LaunchRequest(
            role="runner-live-isolation-probe", task_id=self.task_id, prompt=self.prompt,
            report_path=self.report_path, read_only=True, working_root=".",
            role_grant=("read",), allowed_scope=self.allowed_scope, fresh_session=True,
            no_tools=no_tools, timeout=self.timeout,
        )


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
    # Probe-only, non-evidentiary facts.  These are deliberately classifications rather
    # than the model's response: the runner may persist them when a probe fails.
    probe_parse_status: str | None = None
    probe_allowed_write: bool | None = None
    probe_failure_class: str | None = None
    probe_sibling_mounted: bool | None = None
    probe_subprocess_state: str | None = None
    probe_nested_state: str | None = None
    probe_stderr_reason: str | None = None
    #: Runner-observed, finite containment facts for the Docker proof protocol.  This is
    #: intentionally not the model's final response or a transcript.
    probe_observations: Mapping[str, bool] | None = None
    probe_binding: Mapping[str, str] | None = None
    probe_network_observed: bool | None = None
    probe_process_observed: bool | None = None


#: Stable, machine-readable reason for every executor-context-bundle rejection.
CONTEXT_BUNDLE_INVALID = "context-bundle-invalid"

#: A task-declared context file was unavailable while the runner was assembling the immutable
#: launch bundle.  This is deliberately distinct from a malformed bundle: no child process may
#: start and discover a missing prerequisite for itself.
CONTEXT_UNAVAILABLE = "context-unavailable"

#: The context kinds a runner may hand an executor: its canonical task contract plus the
#: plan / prompt / required-skill content that task needs.
CONTEXT_KINDS = frozenset({"task", "plan", "prompt", "skill", "input"})

#: Host-home and UNC paths are never portable context.  A generic drive-rooted documentation
#: literal (for example, ``C:/docs/x``) is not itself evidence of a host-path leak.
_HOST_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+|/(?:home|Users)/[^/\s]+|/"
    r"root(?:/|\b)|\\\\[^\\/\s]+[\\/][^\\/\s]+|(?<!:)/"
    r"/[^/\s]+/[^/\s]+)",
    re.IGNORECASE,
)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _configured_host_root_re(host_roots: Sequence[str | os.PathLike[str]]) -> re.Pattern[str] | None:
    """Match supplied host roots in either native or forward-slash spelling."""
    spellings: set[str] = set()
    for root in host_roots:
        value = str(root).rstrip("\\/")
        if value:
            spellings.add(value)
            spellings.add(value.replace("\\", "/"))
    if not spellings:
        return None
    return re.compile(
        r"(?:" + "|".join(re.escape(value) for value in sorted(spellings, key=len, reverse=True))
        + r")(?:[\\/]|\b)",
        re.IGNORECASE,
    )


def _is_safe_logical_source(source: str) -> bool:
    """A safe logical source is a clean project/agents-root-relative POSIX path.

    No absolute or drive-letter path, no ``..`` traversal, no backslash (a host separator),
    no empty or ``.`` segment, and no leading/trailing whitespace.
    """
    if not source or source != source.strip() or "\\" in source:
        return False
    if source.startswith("/") or (len(source) >= 2 and source[1] == ":"):
        return False
    parts = source.split("/")
    return all(part not in ("", ".", "..") for part in parts)


@dataclass(frozen=True)
class ContextEntry:
    """One immutable, digest-bound piece of runner-supplied executor context.

    ``logical_source`` is a safe project- or agents-root-relative POSIX path (never a host
    path); ``digest`` is the lowercase SHA-256 hex of ``content``.
    """

    kind: str
    logical_source: str
    digest: str
    content: str

    def validate(self, *, host_roots: Sequence[str | os.PathLike[str]] = ()) -> None:
        if self.kind not in CONTEXT_KINDS:
            raise AdapterError(
                f"context entry kind {self.kind!r} is not one of {sorted(CONTEXT_KINDS)}",
                CONTEXT_BUNDLE_INVALID,
            )
        if not _is_safe_logical_source(self.logical_source):
            raise AdapterError(
                f"context entry source {self.logical_source!r} is not a safe "
                f"project/agents-root logical path",
                CONTEXT_BUNDLE_INVALID,
            )
        if self.digest != _sha256_hex(self.content):
            raise AdapterError(
                f"context entry {self.logical_source!r} digest does not match its content",
                CONTEXT_BUNDLE_INVALID,
            )
        configured_root_re = _configured_host_root_re(host_roots)
        if (_HOST_ABSOLUTE_PATH_RE.search(self.content)
                or configured_root_re is not None and configured_root_re.search(self.content)):
            raise AdapterError(
                f"context entry {self.logical_source!r} content carries a host-absolute path",
                CONTEXT_BUNDLE_INVALID,
            )

    @classmethod
    def of(
        cls, kind: str, logical_source: str, content: str,
        *, host_roots: Sequence[str | os.PathLike[str]] = (),
    ) -> "ContextEntry":
        """Build an entry, digesting ``content`` and failing closed on an unsafe source."""
        entry = cls(kind, logical_source, _sha256_hex(content), content)
        entry.validate(host_roots=host_roots)
        return entry


@dataclass(frozen=True)
class ExecutorContextBundle:
    """Runner-owned, immutable context for exactly one assigned task.

    It carries the canonical task contract plus the task-relevant plan / prompt / required-skill
    content, each bound to a safe logical source and SHA-256 digest. A launch into a nested
    working root (``feature-pipeline-skill``) consumes this bundle instead of reading the task,
    plan, prompt, or skills from outside that root — and it never widens a write root.
    """

    task_id: str
    entries: tuple[ContextEntry, ...]

    def validate(self) -> None:
        if not self.task_id:
            raise AdapterError("executor context bundle has no task id", CONTEXT_BUNDLE_INVALID)
        kinds = {entry.kind for entry in self.entries}
        if "task" not in kinds:
            raise AdapterError(
                "executor context bundle carries no task contract", CONTEXT_BUNDLE_INVALID
            )
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            entry.validate()
            key = (entry.kind, entry.logical_source)
            if key in seen:
                raise AdapterError(
                    f"executor context bundle repeats {entry.kind} source "
                    f"{entry.logical_source!r}",
                    CONTEXT_BUNDLE_INVALID,
                )
            seen.add(key)

    def render(self) -> str:
        """The verbatim context block prepended to the child's stdin prompt."""
        self.validate()
        lines = [
            "=== Runner-supplied executor context (immutable, digest-bound) ===",
            f"Assigned task: {self.task_id}",
            "The runner provides the authoritative task/plan/prompt/skill content below. Treat "
            "it as canonical and do not read these files from outside your working root.",
        ]
        for entry in self.entries:
            lines += [
                "",
                f"--- {entry.kind}: {entry.logical_source} (sha256:{entry.digest}) ---",
                entry.content.rstrip("\n"),
            ]
        lines += ["", "=== End runner-supplied executor context ==="]
        return "\n".join(lines) + "\n"


#: Stable, machine-readable reason for every required-input rejection.
REQUIRED_INPUT_INVALID = "required-input-invalid"

#: The anchors a required-input logical source may resolve under. ``project`` is the outer
#: project root; ``agents`` is the external shared-agents anchor. Nothing else is addressable.
REQUIRED_INPUT_ANCHORS = frozenset({"project", "agents"})


@dataclass(frozen=True)
class RequiredInput:
    """One mandatory read input a nested worker must reach before it edits or verifies code.

    ``kind`` is one of :data:`CONTEXT_KINDS`; ``anchor`` is ``"project"`` or ``"agents"``;
    ``logical_source`` is a safe anchor-relative POSIX path to the *file* (never a directory,
    never a host path). It is deliberately separate from a task's ``Allowed scope`` — deriving
    a directory grant from it never adds a write capability (REC-05 AC-2).
    """

    kind: str
    anchor: str
    logical_source: str

    def validate(self) -> None:
        if self.kind not in CONTEXT_KINDS:
            raise AdapterError(
                f"required input kind {self.kind!r} is not one of {sorted(CONTEXT_KINDS)}",
                REQUIRED_INPUT_INVALID,
            )
        if self.anchor not in REQUIRED_INPUT_ANCHORS:
            raise AdapterError(
                f"required input anchor {self.anchor!r} is not one of "
                f"{sorted(REQUIRED_INPUT_ANCHORS)}",
                REQUIRED_INPUT_INVALID,
            )
        if not _is_safe_logical_source(self.logical_source):
            raise AdapterError(
                f"required input source {self.logical_source!r} is not a safe "
                f"anchor-relative logical path",
                REQUIRED_INPUT_INVALID,
            )
        if "/" not in self.logical_source:
            raise AdapterError(
                f"required input {self.logical_source!r} names no directory to grant; a bare "
                f"anchor-root file would widen the grant to the whole anchor",
                REQUIRED_INPUT_INVALID,
            )


def derive_required_input_dirs(
    inputs: Sequence[RequiredInput],
    *,
    project_root: str | os.PathLike[str],
    agents_root: str | os.PathLike[str],
    reachable_roots: Sequence[str | os.PathLike[str]] = (),
) -> tuple[str, ...]:
    """The smallest set of physical directories that lets a worker read every declared input.

    Each input is validated, resolved under its declared anchor, and reduced to the parent
    directory of the referenced file. The result is de-duplicated, collapsed so no directory
    sits alongside one of its own ancestors, and stripped of anything already reachable from
    ``reachable_roots`` (the worker's own working root). Fails closed — it never silently
    widens — on an unsafe path, a traversal that escapes its anchor, a missing anchor, or the
    same logical source claimed under two anchors (REC-05 AC-4).
    """
    anchors: dict[str, Path] = {
        "project": Path(project_root).resolve(),
        "agents": Path(agents_root).resolve(),
    }
    for name, path in anchors.items():
        if not path.is_dir():
            raise AdapterError(
                f"required-input anchor {name!r} is not a directory: {path}",
                REQUIRED_INPUT_INVALID,
            )
    reachable = [Path(root).resolve() for root in reachable_roots]
    claimed: dict[tuple[str, str], str] = {}
    grants: list[Path] = []
    for item in inputs:
        item.validate()
        key = (item.kind, item.logical_source)
        if claimed.setdefault(key, item.anchor) != item.anchor:
            raise AdapterError(
                f"required input {item.logical_source!r} is claimed under two anchors "
                f"({claimed[key]!r} and {item.anchor!r})",
                REQUIRED_INPUT_INVALID,
            )
        anchor = anchors[item.anchor]
        target = (anchor / item.logical_source).resolve()
        try:
            target.relative_to(anchor)
        except ValueError:
            raise AdapterError(
                f"required input {item.logical_source!r} escapes its {item.anchor!r} anchor",
                REQUIRED_INPUT_INVALID,
            ) from None
        grant = target.parent
        if grant not in grants:
            grants.append(grant)
    kept: list[Path] = []
    for directory in sorted(set(grants), key=lambda p: len(p.parts)):
        if any(directory == root or root in directory.parents for root in reachable):
            continue
        if any(directory == existing or existing in directory.parents for existing in kept):
            continue
        kept.append(directory)
    return tuple(str(directory) for directory in kept)


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


def is_executor_role(role: str) -> bool:
    """Whether ``role`` is a concrete executor the adapter can define and select inline.

    Any ``*-executor`` role (``python-executor``, ``tooling-executor``, …) — and the bare
    ``executor`` charter name — shares the inline executor charter (:func:`_role_charter`), so
    :func:`build_claude_argv` always renders a matching ``--agents`` definition and selects it
    by its exact name with ``--agent``. Such a launch therefore never depends on an on-disk
    ``.claude/agents/<role>.md`` in the target project (RDS-06); availability resolution must
    agree with that contract rather than demand an ambient agent file. A verifier role is not
    an executor even though ``test_verifier`` ends in a non-``executor`` word.
    """
    return normalize_role(role).endswith("executor")


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
        "Feature-pipeline executor. Implement only the selected task's allowed scope and report "
        "the required status envelope. The runner owns declared verification commands and "
        "independent verification, including any remote publication or check observation, so do "
        "not run them, report remote actions, or tick acceptance checkboxes."
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


def _normalized_cwd(value: str) -> str:
    """A comparable form of a task-declared or launch working root: forward slashes, no
    leading/trailing separators, ``.`` for the root itself."""
    text = (value or ".").replace("\\", "/").strip("/")
    return text or "."


def check_command_allowances(
    commands: Sequence[tuple[str, Sequence[str]]],
    *,
    role_grant: Sequence[str],
    working_root: str = ".",
) -> tuple[str, ...]:
    """Exact ``Bash(<argv>)`` allowances for this task's own declared checks (AC-1).

    Each ``commands`` entry is ``(cwd, argv)`` — the task's own declared verification
    commands, already shell-free by contract (:class:`~feature_pipeline.contracts.CommandSpec`
    never accepts a shell metacharacter). A command is granted one exact allowance only when
    every one of these holds:

    * the role's grant carries ``run_checks`` — no grant means no allowance at all, regardless
      of what the task declares;
    * its declared ``cwd`` equals the executor's own ``working_root`` (normalized for slash
      and case-of-empty-root differences only — never a prefix or an ancestor match);
    * its ``argv`` is non-empty, carries no shell metacharacter, and is not a ``git push``
      form.

    A command failing any test is silently excluded — never approved on a partial match — so
    the result is always a subset of what the task actually declared for its own root, one
    allowance per qualifying command, in declaration order.
    """
    if "run_checks" not in set(role_grant):
        return ()
    root = _normalized_cwd(working_root)
    allowed: list[str] = []
    for cwd, argv in commands:
        if _normalized_cwd(cwd) != root:
            continue
        argv = tuple(str(token) for token in argv)
        if not argv:
            continue
        try:
            _assert_shell_free(argv)
        except AdapterError:
            continue
        if any(_is_push_token(token) for token in argv):
            continue
        entry = f"Bash({' '.join(argv)})"
        if entry not in allowed:
            allowed.append(entry)
    return tuple(allowed)


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
    if request.model is not None:
        argv += ["--model", request.model]
    if request.effort is not None:
        argv += ["--effort", request.effort]
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

    # Scoped write roots first, then the read-only required-input grants (REC-05). Both lower
    # to ``--add-dir``; the required-input set is deliberately not folded into any write scope.
    seen_dirs: set[str] = set()
    for directory in (*add_dirs, *request.required_input_dirs):
        text = str(directory)
        if text in seen_dirs:
            continue
        seen_dirs.add(text)
        argv += ["--add-dir", text]
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
    sandbox: str | None = None,
    isolation_probe: bool = False,
) -> list[str]:
    """Build the documented non-interactive ``codex exec`` argv for one launch."""
    grant = effective_grant(request)
    if request.no_tools:
        # `codex exec` has no argv surface that denies every tool.  Its read-only sandbox
        # only prevents writes; it still permits reads and command execution, so accepting
        # this request would falsely label an override-admitted test verifier tool-free.
        raise AdapterError(
            "codex exec cannot enforce a tool-free launch",
            "no-tools-unsupported",
        )
    argv = _executable_prefix(executable) + ["exec"]
    # ``codex exec resume --help`` intentionally exposes no sandbox, working-directory, or
    # extra-directory flags. A runner status continuation is therefore a fresh, tool-free
    # request so its read-only sandbox and resolved grants are present on the actual argv.
    sandbox = "read-only" if request_is_read_only(request) else (sandbox or "workspace-write")
    argv += ["--json", "--sandbox", sandbox]
    if isolation_probe:
        # The runner-owned probe must neither resume nor persist an ambient Codex session,
        # and its observation must not inherit user configuration or exec-policy rules. Its
        # temporary runner-owned cwd is not a trusted Git worktree, so Codex also requires
        # the documented repository check bypass for this probe alone.
        argv += [
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check",
        ]
    if request.model is not None:
        argv += ["--model", request.model]
    if request.effort is not None:
        # `codex exec --help` documents `--config key=value`; the installed
        # client accepts this native reasoning control without ambient config.
        argv += ["--config", f'model_reasoning_effort="{request.effort}"']
    if working_root is not None:
        argv += ["--cd", str(working_root)]
    seen_dirs: set[str] = set()
    for directory in (*add_dirs, *request.required_input_dirs):
        text = str(directory)
        if text in seen_dirs:
            continue
        seen_dirs.add(text)
        argv += ["--add-dir", text]
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


def observe_cli_version(
    executable: str | Sequence[str] | None,
    runner: ProcessRunner,
    *,
    timeout: float,
    env: dict[str, str] | None,
) -> str:
    """Observe one CLI version with the adapter's shell-free, bounded process runner."""
    if executable is None:
        raise AdapterError("the selected CLI is not available", "adapter-unavailable")
    argv = (executable,) if isinstance(executable, str) else tuple(executable)
    completed = runner((*argv, "--version"), prompt="", timeout=timeout, env=env)
    if completed.exit_code != 0:
        raise AdapterError("the selected CLI did not report a version", "adapter-version-unavailable")
    lines = (completed.stdout or completed.stderr).strip().splitlines()
    version = lines[0].strip() if lines else ""
    if not version or len(version) > 200 or any(not char.isprintable() for char in version):
        raise AdapterError("the selected CLI returned an invalid version", "adapter-version-invalid")
    return version


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
IsolationProbeValidator = Callable[[str | Sequence[str], str], AdapterCapabilities]


def _validated_strict_capabilities(
    capabilities: AdapterCapabilities,
    validator: IsolationProbeValidator | None,
    executable: str | Sequence[str],
    cli_surface: str,
) -> AdapterCapabilities:
    """Use the runner's current proof only when production supplied its validator."""
    if validator is None:
        return capabilities
    try:
        return validator(executable, cli_surface)
    except ValueError as exc:
        raise AdapterError(
            "adapter strict-isolation evidence is unavailable or invalid",
            STACK_ISOLATION_UNSUPPORTED,
        ) from exc


class ClaudeAdapter:
    """:class:`Adapter` over the installed ``claude`` CLI.

    Everything host-specific is injected: ``executable`` (or a ``resolver`` callable that finds
    it), the process ``runner``, the settings path, extra reachable directories, and the
    environment. The adapter holds no reference to run state and writes nothing.
    """

    name = "claude"
    #: Executor windows are run in a disposable copy and promoted by the dispatcher.
    isolated_workspace = True
    #: Claude's RLC-01 status-envelope prompt requires a blocked diagnostic.  Other adapter
    #: protocols retain compatibility with the historical four-key blocked envelope.
    requires_blocked_envelope_reason = True

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
        executor_contexts: Mapping[str, ExecutorContextBundle] | None = None,
        required_input_dirs: Mapping[str, Sequence[str]] | None = None,
        task_scope_dirs: Mapping[str, Sequence[str]] | None = None,
        isolation_capabilities: AdapterCapabilities | None = None,
        isolation_probe_validator: IsolationProbeValidator | None = None,
    ) -> None:
        self._executable = executable
        self._resolver = resolver or (lambda: shutil.which("claude"))
        self._runner: ProcessRunner = runner or run_subprocess
        self._settings_path = settings_path
        self._scope_roots = tuple(scope_roots)
        self._env = env
        self._timeout = timeout
        self._working_root = Path(working_root) if working_root is not None else None
        #: Declared strict-isolation guarantees (R03). Defaults to the evidence-bound
        #: structural declaration; a caller only overrides this with an explicit,
        #: separately budgeted live-probe measurement — never to widen a launch silently.
        self._isolation_capabilities = isolation_capabilities or CLAUDE_ISOLATION_CAPABILITIES
        self._isolation_probe_validator = isolation_probe_validator
        #: Runner-owned minimal mandatory-input directory grants, keyed by task id. Merged
        #: into the launch's ``--add-dir`` set on the first (session-opening) launch so a
        #: nested working root can read its task/plan/prompt/skill files (REC-05).
        self._required_input_dirs: dict[str, tuple[str, ...]] = {
            key: tuple(value) for key, value in dict(required_input_dirs or {}).items()
        }
        #: Runner-owned minimal declared-``allowed_scope`` directory grants outside this
        #: task's own working root, keyed by task id (CSR-01). Distinct from
        #: ``required_input_dirs``: these lower to ``--add-dir`` only for a write-capable,
        #: non-read-only request, never for a read-only verifier launch.
        self._task_scope_dirs: dict[str, tuple[str, ...]] = {
            key: tuple(value) for key, value in dict(task_scope_dirs or {}).items()
        }
        #: Runner-owned immutable context, keyed by task id. Merged into the child's stdin
        #: prompt on its first (session-opening) launch so a nested working root gets exact
        #: task/plan/prompt/skill content without a filesystem read outside it.
        self._executor_contexts: dict[str, ExecutorContextBundle] = dict(executor_contexts or {})

    def resolved_executable(self) -> str | Sequence[str] | None:
        if self._executable is not None:
            return self._executable
        return self._resolver()

    def available(self) -> bool:
        return self.resolved_executable() is not None

    def observe_cli_version(self, timeout: float) -> str:
        """Return one bounded, runner-observed CLI version before a live probe launch."""
        return observe_cli_version(
            self.resolved_executable(), self._runner, timeout=timeout, env=self._env,
        )

    def can_resolve_executor(self, role: str, *, working_root: str = ".") -> bool:
        """Resolve custom or enabled built-in agents before opening an executor window.

        A concrete ``*-executor`` role (:func:`is_executor_role`) resolves through the adapter's
        own inline ``--agents``/``--agent`` definition, so it needs no project-local agent file
        and no built-in entry; an on-disk definition still wins when present. Every other custom
        role stays fail-closed unless an on-disk or enabled built-in agent backs it.
        """
        name = on_disk_agent_name(role)
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            return False
        directory = ((self._working_root or Path.cwd()) / working_root).resolve()
        roots = (directory, *directory.parents, Path.home())
        if any((root / ".claude" / "agents" / f"{name}.md").is_file() for root in roots):
            return True
        if is_executor_role(role):
            return True
        env = os.environ if self._env is None else self._env
        if env.get("CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS") == "1":
            return False
        if (name in {"Explore", "Plan"}
                and env.get("CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS") == "1"):
            return False
        return name in _CLAUDE_BUILTIN_AGENTS

    def _with_required_inputs(self, request: LaunchRequest) -> LaunchRequest:
        """Merge this task's minimal mandatory-input grants onto a fresh, session-opening
        request. A resumed continuation already has the reach it needs and is left untouched."""
        if request.resume_session_id:
            return request
        dirs = self._required_input_dirs.get(request.task_id, ())
        if not dirs:
            return request
        merged = tuple(dict.fromkeys((*request.required_input_dirs, *dirs)))
        if merged == tuple(request.required_input_dirs):
            return request
        return replace(request, required_input_dirs=merged)

    def plan(self, request: LaunchRequest) -> list[str]:
        """The argv this request would run — used by a dry run and by the tests, so what is
        reviewed is exactly what would execute."""
        request = self._with_required_inputs(request)
        executable = self.resolved_executable() or "claude"
        return build_claude_argv(
            request, executable=executable,
            settings_path=self._settings_path, add_dirs=self._add_dirs_for(request),
        )

    def launch(self, request: LaunchRequest) -> LaunchResult:
        # This is a security boundary, not an availability probe: reject a strict role this
        # adapter cannot structurally isolate before checking the executable, building argv,
        # or starting any process (AC-2).
        _assert_bundle_identity(request)
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError(
                "the Claude CLI is not available on PATH", "adapter-unavailable"
            )
        require_strict_isolation(
            _validated_strict_capabilities(
                self._isolation_capabilities, self._isolation_probe_validator,
                executable, "claude -p",
            ),
            role=request.role,
            executable=executable,
            cli_surface="claude -p",
        )
        request = self._with_required_inputs(request)
        _assert_bundle_identity(request)
        argv = build_claude_argv(
            request, executable=executable,
            settings_path=self._settings_path, add_dirs=self._add_dirs_for(request),
        )
        completed = self._runner(
            argv,
            prompt=self._prompt_for(request),
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

    def launch_live_probe(self, request: LiveProbeRequest) -> LaunchResult:
        """Launch the typed probe from an empty temporary root, never project context."""
        if type(request) is not LiveProbeRequest:
            raise AdapterError("live probe requires a runner-owned request", "live-probe-invalid")
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError("the Claude CLI is not available on PATH", "adapter-unavailable")
        probe = request.as_launch_request(no_tools=True)
        completed = self._runner(
            build_claude_argv(
                probe, executable=executable, settings_path=self._settings_path,
                add_dirs=self._add_dirs_for(probe),
            ),
            prompt=probe.prompt, cwd=self._cwd_for(probe), timeout=probe.timeout, env=self._env,
        )
        extracted_text = parse_result_text(completed.stdout)
        return LaunchResult(
            exit_code=completed.exit_code,
            stdout=extracted_text if extracted_text is not None else completed.stdout,
            stderr=completed.stderr, raw_stdout=completed.stdout,
        )

    def _prompt_for(self, request: LaunchRequest) -> str:
        """The stdin prompt: the request prompt, prefixed with this task's context bundle.

        Only the first, session-opening launch is enriched — a same-session ``--resume``
        continuation (the status envelope) already has the context in its transcript. A
        malformed or tampered bundle fails the launch closed rather than degrading to a
        context-free prompt.
        """
        if request.resume_session_id:
            return request.prompt
        bundle = self._executor_contexts.get(request.task_id)
        if bundle is None:
            return request.prompt
        bundle.validate()
        return bundle.render() + "\n" + request.prompt

    def _cwd_for(self, request: LaunchRequest) -> Path | None:
        working_root = request.working_root or "."
        if self._working_root is not None:
            return self._working_root / working_root
        if working_root != ".":
            return Path(working_root)
        return None

    def _add_dirs_for(self, request: LaunchRequest) -> tuple[str, ...]:
        external = scoped_add_dirs(request, self._scope_roots)
        if request_is_read_only(request) or not set(effective_grant(request)) & WRITE_CAPABILITIES:
            return external
        task_dirs = self._task_scope_dirs.get(request.task_id, ())
        return tuple(dict.fromkeys((*external, *task_dirs)))


class CodexAdapter:
    """Adapter over the non-interactive ``codex exec`` CLI."""

    name = "codex"
    #: Executor windows are run in a disposable copy and promoted by the dispatcher.
    isolated_workspace = True
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
        required_input_dirs: Mapping[str, Sequence[str]] | None = None,
        task_scope_dirs: Mapping[str, Sequence[str]] | None = None,
        isolation_capabilities: AdapterCapabilities | None = None,
        isolation_probe_validator: IsolationProbeValidator | None = None,
    ) -> None:
        self._executable = executable
        self._resolver = resolver or (lambda: shutil.which("codex"))
        self._runner: ProcessRunner = runner or run_codex_subprocess
        self._scope_roots = tuple(scope_roots)
        self._env = env
        self._timeout = timeout
        self._working_root = Path(working_root) if working_root is not None else None
        #: Declared strict-isolation guarantees (R03). Defaults to the evidence-bound
        #: structural declaration — every token closed, since ``codex exec`` exposes none of
        #: the required flags — unless a caller supplies an explicit, separately budgeted
        #: live-probe measurement.
        self._isolation_capabilities = isolation_capabilities or CODEX_ISOLATION_CAPABILITIES
        self._isolation_probe_validator = isolation_probe_validator
        #: Runner-owned minimal mandatory-input directory grants, keyed by task id (REC-05).
        self._required_input_dirs: dict[str, tuple[str, ...]] = {
            key: tuple(value) for key, value in dict(required_input_dirs or {}).items()
        }
        #: Runner-owned minimal declared-``allowed_scope`` directory grants outside this
        #: task's own working root, keyed by task id (CSR-01). Write-only, like the working
        #: root's own ``--add-dir`` grant; never applied to a read-only verifier launch.
        self._task_scope_dirs: dict[str, tuple[str, ...]] = {
            key: tuple(value) for key, value in dict(task_scope_dirs or {}).items()
        }

    def resolved_executable(self) -> str | Sequence[str] | None:
        return self._executable if self._executable is not None else self._resolver()

    def available(self) -> bool:
        return self.resolved_executable() is not None

    def observe_cli_version(self, timeout: float) -> str:
        """Return one bounded, runner-observed CLI version before a live probe launch."""
        return observe_cli_version(
            self.resolved_executable(), self._runner, timeout=timeout, env=self._env,
        )

    def _with_required_inputs(self, request: LaunchRequest) -> LaunchRequest:
        """Merge this task's minimal mandatory-input grants onto a fresh request (REC-05)."""
        if request.resume_session_id:
            return request
        dirs = self._required_input_dirs.get(request.task_id, ())
        if not dirs:
            return request
        merged = tuple(dict.fromkeys((*request.required_input_dirs, *dirs)))
        if merged == tuple(request.required_input_dirs):
            return request
        return replace(request, required_input_dirs=merged)

    def plan(self, request: LaunchRequest) -> list[str]:
        request = self._with_required_inputs(request)
        executable = self.resolved_executable() or "codex"
        return build_codex_argv(
            request,
            executable=executable,
            working_root=self._cwd_for(request),
            add_dirs=self._add_dirs_for(request),
            sandbox=self._sandbox_for(request),
        )

    def launch(self, request: LaunchRequest) -> LaunchResult:
        # See ClaudeAdapter.launch: a security boundary checked before any process starts.
        _assert_bundle_identity(request)
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError("the Codex CLI is not available on PATH", "adapter-unavailable")
        require_strict_isolation(
            _validated_strict_capabilities(
                self._isolation_capabilities, self._isolation_probe_validator,
                executable, "codex exec",
            ),
            role=request.role,
            executable=executable,
            cli_surface="codex exec",
        )
        request = self._with_required_inputs(request)
        _assert_bundle_identity(request)
        completed = self._runner(
            build_codex_argv(
                request,
                executable=executable,
                working_root=self._cwd_for(request),
                add_dirs=self._add_dirs_for(request),
                sandbox=self._sandbox_for(request),
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

    def launch_live_probe(self, request: LiveProbeRequest) -> LaunchResult:
        """Launch only the typed runner probe, without treating it as a strict role launch."""
        if type(request) is not LiveProbeRequest:
            raise AdapterError("live probe requires a runner-owned request", "live-probe-invalid")
        executable = self.resolved_executable()
        if executable is None:
            raise AdapterError("the Codex CLI is not available on PATH", "adapter-unavailable")
        # Codex exposes no tool-free switch.  Its bounded probe is still read-only, while the
        # measurement records any negative outcome and never grants capabilities from it.
        probe = request.as_launch_request(no_tools=False)
        # Codex discovers AGENTS.md and skills relative to its working directory. A probe must
        # observe only the adapter boundary, so it receives neither the project root nor any
        # task input or scoped directory grant.
        with tempfile.TemporaryDirectory(prefix="pipeline-live-probe-") as directory:
            hermetic_root = Path(directory)
            completed = self._runner(
                build_codex_argv(
                    probe, executable=executable, working_root=hermetic_root,
                    add_dirs=(), sandbox="read-only", isolation_probe=True,
                ),
                prompt=probe.prompt, cwd=hermetic_root, timeout=probe.timeout, env=self._env,
            )
        text = parse_codex_result_text(completed.stdout)
        return LaunchResult(
            completed.exit_code, text if text is not None else completed.stdout,
            completed.stderr, raw_stdout=completed.stdout,
        )

    def _sandbox_for(self, request: LaunchRequest) -> str | None:
        working_root = self._cwd_for(request)
        if request_is_read_only(request) or working_root is None or os.name != "nt":
            return None
        workspace = working_root.parent
        if (workspace.name == "workspace"
                and workspace.parent.name.startswith("feature-pipeline-executor-")):
            return "danger-full-access"
        return None

    def _cwd_for(self, request: LaunchRequest) -> Path | None:
        working_root = request.working_root or "."
        if self._working_root is not None:
            return self._working_root / working_root
        if working_root != ".":
            return Path(working_root)
        return None

    def _add_dirs_for(self, request: LaunchRequest) -> tuple[str, ...]:
        external_roots = scoped_add_dirs(request, self._scope_roots)
        if request_is_read_only(request) or not set(effective_grant(request)) & WRITE_CAPABILITIES:
            return external_roots
        task_dirs = self._task_scope_dirs.get(request.task_id, ())
        working_root = self._cwd_for(request)
        if working_root is None:
            return tuple(dict.fromkeys((*external_roots, *task_dirs)))
        # Codex's Windows workspace-write sandbox checks that every parent required to
        # traverse into `--cd` is itself an explicit reachable grant.  Granting only the
        # leaf worktree produces an `Access to ...\\workspace is denied` Set-Location
        # failure before the executor can edit it.  The parent is the runner-created,
        # disposable workspace container; it is not a project-wide grant.
        root = str(working_root)
        workspace = working_root.parent
        # Only runner-created disposable workspaces receive their container grant.  Adding
        # the parent of an ordinary repository would widen a project-root launch to a drive.
        if (workspace.name == "workspace"
                and workspace.parent.name.startswith("feature-pipeline-executor-")):
            return tuple(dict.fromkeys((str(workspace), root, *external_roots, *task_dirs)))
        return tuple(dict.fromkeys((root, *external_roots, *task_dirs)))


_IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}$")
_CODEX_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
_CONNECT_PROXY_SOURCE = r'''import ipaddress
import select
import socket
import socketserver
ALLOW = ("api.openai.com", "auth.openai.com", "chatgpt.com", "registry.npmjs.org")
def allowed(host):
    host = host.rstrip(".").lower()
    try: ipaddress.ip_address(host); return False
    except ValueError: pass
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOW)
def public_address(host):
    try: records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError: return None
    addresses = [record[4][0] for record in records]
    if not addresses: return None
    for address in addresses:
        value = ipaddress.ip_address(address)
        if value.is_private or value.is_loopback or value.is_link_local or value.is_reserved or value.is_multicast or value.is_unspecified: return None
    return addresses[0]
class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        parts = self.rfile.readline(4096).decode("ascii", "replace").strip().split()
        if len(parts) != 3 or parts[0] != "CONNECT": return
        host, sep, port = parts[1].rpartition(":")
        if not sep or not host or not port.isdigit() or int(port) != 443 or not allowed(host): return
        while True:
            header = self.rfile.readline(4096)
            if not header or header in (b"\r\n", b"\n"): break
        address = public_address(host)
        if address is None: return
        try: upstream = socket.create_connection((address, 443), timeout=15)
        except OSError: return
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        try:
            while True:
                ready, _, _ = select.select((self.connection, upstream), (), (), 30)
                for source in ready:
                    data = source.recv(65536)
                    if not data: return
                    (upstream if source is self.connection else self.connection).sendall(data)
        finally: upstream.close()
class Server(socketserver.ThreadingTCPServer): allow_reuse_address = True
Server(("0.0.0.0", 8080), Proxy).serve_forever()
'''


def _scoped_container_patterns(request: LaunchRequest, source: Path) -> tuple[str, ...]:
    """Return only scope patterns relative to the routed working root.

    Plans name paths from the disposable-workspace root, whereas an adapter is routed to a
    nested working root such as ``feature-pipeline-skill``.  Removing that one matching prefix
    never widens a pattern; a scope rooted elsewhere simply supplies no writable file.
    """
    patterns: list[str] = []
    for raw in request.allowed_scope:
        value = raw.replace("\\", "/")
        if (not value or value.startswith(("/", "~")) or ":" in value
                or ".." in value.split("/")
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise AdapterError(
                "container scope must be a safe repository-relative pattern",
                STACK_ISOLATION_UNSUPPORTED,
            )
        prefix = f"{source.name}/"
        if value.startswith(prefix):
            value = value[len(prefix):]
        if value and not value.startswith("../"):
            patterns.append(value)
    return tuple(patterns)


def _copy_scoped_workspace(source: Path, target: Path, patterns: Sequence[str]) -> None:
    """Create the container's writable worktree from allowed files only."""
    for candidate in source.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.relative_to(source).as_posix()
        if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, destination)


def _normalize_container_workspace(workspace: Path) -> None:
    """Make the disposable scoped copy writable by the contained non-root UID only."""
    workspace.chmod(0o777)
    for candidate in workspace.rglob("*"):
        candidate.chmod(0o777 if candidate.is_dir() else 0o666)


def _materialize_container_context(
    bundle: ExecutorContextBundle, target: Path, *, runtime_root: Path,
) -> dict[str, str]:
    """Write the exact runner-bound inputs for one Docker launch, read-only.

    The Docker worktree deliberately contains only editable scope.  This separate tree is
    therefore the only route by which task context reaches the container; it is never copied
    back or made part of the writable worktree.
    """
    bundle.validate()
    paths: dict[str, str] = {}
    bound_destinations: set[Path] = set()
    for entry in bundle.entries:
        anchor = "agents" if entry.logical_source.startswith(".agents/") else "project"
        source = (entry.logical_source.removeprefix(".agents/")
                  if anchor == "agents" else entry.logical_source)
        destination = target / anchor / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(entry.content, encoding="utf-8")
        destination.chmod(0o444)
        bound_destinations.add(destination)
        paths[entry.logical_source] = f"/context/{anchor}/{source}"
    for source in _container_runtime_sources(runtime_root):
        destination = target / "project" / "feature-pipeline-skill" / source.relative_to(
            runtime_root
        )
        if destination in bound_destinations:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o444)
    (target / "AGENTS.md").write_text(
        "# Runner-owned container instructions\n"
        "Read task context only from /context. /context is read-only; edit only /workspace.\n",
        encoding="utf-8",
    )
    for directory in (target, *target.rglob("*")):
        if directory.is_dir():
            directory.chmod(0o555)
    return paths


_CONTAINED_EXECUTOR_GUIDANCE = """Runner-owned contained-executor rules:
- This Docker launch intentionally provides only the minimal read-only context and runtime import closure. Do not broaden context to an arbitrary or full application graph, and do not request out-of-scope modules.
- /context is read-only; edit only /workspace, and do not edit outside the allowed scope. This containment never changes or bypasses acceptance criteria.
- If constrained imports prevent the permanent test from loading the full application graph, follow TDD here: establish RED then GREEN with a standalone temporary Python assertion or script using available target/runtime imports, then write the permanent allowed test. The temporary assertion or script is not a substitute for the permanent test, acceptance criteria, or declared checks.
- The runner remains the owner of full repository verification.
"""


def _container_runtime_sources(runtime_root: Path) -> tuple[Path, ...]:
    """Return the validated local import closure required by ``pipeline_core.adapters``.

    The Docker workspace carries the allowed ``pipeline_core`` files.  Its imports of the
    installed ``feature_pipeline`` package must therefore come from the separate read-only
    context, never from a broad project-root mount.
    """
    root = runtime_root.resolve()
    src_root = root / "src"
    entry = root / "pipeline_core" / "adapters.py"
    if root.is_symlink() or not src_root.is_dir() or entry.is_symlink() or not entry.is_file():
        raise AdapterError("container runtime source is unavailable", CONTEXT_UNAVAILABLE)
    pending = list(_feature_pipeline_imports(entry, "pipeline_core.adapters"))
    seen: set[str] = set()
    sources: set[Path] = set()
    while pending:
        module = pending.pop()
        if not module.startswith("feature_pipeline") or module in seen:
            continue
        seen.add(module)
        source, package = _container_module_source(src_root, module)
        sources.add(source)
        pending.extend(_container_package_initializers(src_root, module))
        pending.extend(_feature_pipeline_imports(source, module if not package else module))
    return tuple(sorted(sources))


def _container_module_source(src_root: Path, module: str) -> tuple[Path, bool]:
    """Resolve one local package/module source, rejecting anything outside ``src``."""
    relative = Path(*module.split("."))
    candidates = ((src_root / relative).with_suffix(".py"), src_root / relative / "__init__.py")
    for candidate in candidates:
        resolved = candidate.resolve()
        if (not candidate.is_symlink() and candidate.is_file()
                and resolved.is_relative_to(src_root.resolve())):
            return resolved, candidate.name == "__init__.py"
    raise AdapterError(f"container runtime module {module!r} is unavailable", CONTEXT_UNAVAILABLE)


def _container_package_initializers(src_root: Path, module: str) -> tuple[str, ...]:
    """List package initializers Python executes before importing ``module``."""
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts))
                 if (src_root / Path(*parts[:index]) / "__init__.py").is_file())


def _feature_pipeline_imports(source: Path, module: str) -> tuple[str, ...]:
    """Parse only explicit local imports from a validated source file."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise AdapterError("container runtime source is unavailable", CONTEXT_UNAVAILABLE) from exc
    imports: set[str] = set()
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.startswith("feature_pipeline"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            else:
                base = node.module or ""
            if base.startswith("feature_pipeline"):
                imports.add(base)
                if node.module is None:
                    imports.update(f"{base}.{alias.name}" for alias in node.names)
    return tuple(sorted(imports))


class DockerCodexAdapter:
    """Opt-in strict Codex launch path contained by a runner-owned Docker workspace.

    This adapter is deliberately separate from :class:`CodexAdapter`: no Windows host launch
    can fall through to Docker, and no Docker problem can fall through to danger-full-access.
    The caller must supply a digest-pinned image and the one existing auth file selected by the
    runner.  The file is bind-mounted read-only without this class ever opening it.
    """

    name = "codex"
    isolated_workspace = True
    requires_fresh_envelope_context = True
    _PROBE_CONTRACT_REVISION = "docker-codex-containment-proof-v1"

    @staticmethod
    def _live_probe_launch_request(
        *, task_id: str, prompt: str, report_path: Path, working_root: str, timeout: float,
    ) -> LaunchRequest:
        """Compose the one writable runner-owned request used for every live probe."""
        return LaunchRequest(
            role="runner-live-isolation-probe", task_id=task_id, prompt=prompt,
            report_path=report_path, working_root=working_root, role_grant=("read", "write"),
            allowed_scope=("probe/**",), timeout=timeout,
        )

    @classmethod
    def probe_binding(
        cls, *, image: str, proxy_image: str, codex_version: str, auth_file: Path,
    ) -> dict[str, str]:
        """Return the canonical, host-path-free binding for a Docker containment proof."""
        adapter = object.__new__(cls)
        adapter._image = image
        adapter._proxy_image = proxy_image
        adapter._codex_version = codex_version
        adapter._auth_file = auth_file.resolve()
        adapter._docker_executable = "docker"
        probe = cls._live_probe_launch_request(
            task_id="canonical", prompt="runner-owned live isolation observation",
            report_path=Path("live-probe.json"), working_root="/runner-owned-workspace",
            timeout=1.0,
        )
        argv = adapter._docker_argv(Path("/runner-owned-workspace"), probe, "runner-owned-network")
        return {
            "image": image,
            "package": f"@openai/codex@{codex_version}",
            "observed_version": f"codex {codex_version}",
            "argv_digest": cls._probe_control_digest(argv),
            "contract_revision": cls._PROBE_CONTRACT_REVISION,
        }

    def __init__(
        self,
        *,
        image: str,
        proxy_image: str,
        codex_version: str,
        auth_file: Path,
        docker_executable: str = "docker",
        runner: ProcessRunner | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        isolation_capabilities: AdapterCapabilities | None = None,
        image_validator: Callable[[str, str], bool] | None = None,
        docker_runner: Callable[[list[str]], CompletedProcess] | None = None,
        executor_contexts: Mapping[str, ExecutorContextBundle] | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        if not _IMAGE_DIGEST_RE.fullmatch(image):
            raise AdapterError("container image must be digest-pinned", "container-image-unpinned")
        if not _IMAGE_DIGEST_RE.fullmatch(proxy_image):
            raise AdapterError("CONNECT proxy image must be digest-pinned", "container-proxy-image-unpinned")
        if not _CODEX_VERSION_RE.fullmatch(codex_version):
            raise AdapterError("Codex package version must be pinned", "container-package-unpinned")
        if auth_file.is_symlink() or not auth_file.is_file():
            raise AdapterError("runner-selected Codex auth file is unavailable", "container-auth-unavailable")
        self._image = image
        self._proxy_image = proxy_image
        self._codex_version = codex_version
        self._auth_file = auth_file.resolve()
        self._docker_executable = docker_executable
        self._runner: ProcessRunner = runner or run_codex_subprocess
        self._timeout = timeout
        self._isolation_capabilities = isolation_capabilities or CODEX_ISOLATION_CAPABILITIES
        self._image_validator = image_validator or self._validate_image
        self._docker_runner = docker_runner or self._run_docker
        self._observed_version = ""
        self._executor_contexts: dict[str, ExecutorContextBundle] = dict(executor_contexts or {})
        self._runtime_root = runtime_root.resolve() if runtime_root is not None else None

    def available(self) -> bool:
        """Docker is usable only when the runner-selected auth file still exists."""
        return shutil.which(self._docker_executable) is not None and self._auth_file.is_file()

    def observe_cli_version(self, timeout: float) -> str:
        """Observe the exact npm-pinned runtime through the same contained proxy path."""
        if not self.available() or not self._validate_runtime_identity():
            raise AdapterError("container runtime is unavailable", "container-image-unavailable")
        network = f"feature-pipeline-codex-version-{uuid.uuid4().hex}"
        proxy_name = f"codex-egress-proxy-{uuid.uuid4().hex}"
        created = False
        try:
            if self._control(["network", "create", "--internal", network]).exit_code != 0:
                raise AdapterError("internal Codex network could not be created", "container-network-unavailable")
            created = True
            proxy = self._control([
                "run", "-d", "--rm", "--name", proxy_name, "--network", network,
                "--network-alias", "codex-egress-proxy", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m", self._proxy_image,
                "python", "-c", _CONNECT_PROXY_SOURCE,
            ])
            if proxy.exit_code != 0 or self._control(["network", "connect", "bridge", proxy_name]).exit_code != 0:
                raise AdapterError("runner-owned CONNECT proxy could not be started", "container-proxy-unavailable")
            result = self._runner(self._version_argv(network), prompt="", cwd=None,
                                  timeout=min(timeout, 120.0), env=None)
            if result.exit_code != 0 or not self._is_expected_codex_version(result.stdout):
                raise AdapterError("npm-pinned Codex version is unavailable or mismatched", "container-codex-unavailable")
            return result.stdout.strip()
        finally:
            self._control(["rm", "-f", proxy_name])
            if created:
                self._control(["network", "rm", network])

    @staticmethod
    def _run_docker(argv: list[str]) -> CompletedProcess:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                    errors="strict", timeout=30, check=False)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return CompletedProcess(1, "", "docker control command failed")
        return CompletedProcess(result.returncode, result.stdout, result.stderr)

    def _control(self, argv: list[str]) -> CompletedProcess:
        return self._docker_runner([self._docker_executable, *argv])

    @staticmethod
    def _validate_image(docker_executable: str, image: str) -> bool:
        """Require Docker to resolve the exact digest-pinned image before role launch."""
        try:
            result = subprocess.run(
                [docker_executable, "image", "inspect", "--format", "{{json .RepoDigests}}", image],
                capture_output=True, text=True, encoding="utf-8", errors="strict",
                timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return False
        if result.returncode != 0:
            return False
        try:
            digests = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        return isinstance(digests, list) and image in digests

    def _validate_runtime_identity(self) -> bool:
        for selected in (self._image, self._proxy_image):
            image = self._control(["image", "inspect", "--format", "{{json .RepoDigests}}", selected])
            if image.exit_code != 0:
                return False
            try:
                digests = json.loads(image.stdout)
            except json.JSONDecodeError:
                return False
            if not isinstance(digests, list) or selected not in digests:
                return False
        return True

    def _version_argv(self, network: str) -> list[str]:
        """Run the exact npm-pinned Codex version through the allowlisted proxy."""
        return [
            self._docker_executable, "run", "--rm", "--network", network, "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs", "/npm-cache:rw,exec,nosuid,nodev,size=768m",
            "--tmpfs", "/run/codex-auth:rw,noexec,nosuid,nodev,size=1m",
            "--tmpfs", "/codex-home:rw,noexec,nosuid,nodev,size=8m",
            "--mount", f"type=bind,src={self._auth_file},dst=/run/codex-auth/auth.json,readonly",
            "--env", "HTTP_PROXY=http://codex-egress-proxy:8080",
            "--env", "HTTPS_PROXY=http://codex-egress-proxy:8080", "--env", "NO_PROXY=",
            "--env", "CODEX_HOME=/codex-home", "--env", "NPM_CONFIG_CACHE=/npm-cache",
            "--env", "TMPDIR=/npm-cache",
            self._image, "sh", "-ceu",
            "cp /run/codex-auth/auth.json \"$CODEX_HOME/auth.json\"; "
            f"exec npx --yes --package @openai/codex@{self._codex_version} codex --version",
        ]

    def _validate_codex_version(self, network: str) -> bool:
        version = self._runner(self._version_argv(network), prompt="", cwd=None,
                               timeout=min(self._timeout, 120.0), env=None)
        valid = version.exit_code == 0 and self._is_expected_codex_version(version.stdout)
        if valid:
            self._observed_version = version.stdout.strip()
        return valid

    def _is_expected_codex_version(self, output: str) -> bool:
        return output.strip() in {
            f"codex {self._codex_version}",
            f"codex-cli {self._codex_version}",
        }

    def plan(self, request: LaunchRequest) -> list[str]:
        """Render the exact Docker argv without accessing auth-file contents."""
        # The disposable directory is a planning placeholder only; launch creates it.
        return self._docker_argv(Path("/runner-owned-workspace"), request, "runner-owned-network")

    def _docker_argv(
        self, workspace: Path, request: LaunchRequest, network: str,
        context: Path | None = None,
    ) -> list[str]:
        workspace_mode = ",readonly" if request_is_read_only(request) else ""
        mounts = [
            f"type=bind,src={workspace},dst=/workspace{workspace_mode}",
            f"type=bind,src={self._auth_file},dst=/run/codex-auth/auth.json,readonly",
        ]
        if context is not None:
            mounts.append(f"type=bind,src={context},dst=/context,readonly")
        probe_schema = (
            " --output-schema /workspace/probe/final-response.schema.json"
            if request.role == "runner-live-isolation-probe" else ""
        )
        return [
            # Docker closes container stdin unless ``-i`` is explicit. Codex receives the
            # runner-owned prompt through this pipe (the final ``-`` in its argv), without a TTY.
            self._docker_executable, "run", "--rm", "-i", "--network", network, "--read-only",
            "--user", "1000:1000",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs", "/npm-cache:rw,exec,nosuid,nodev,size=768m",
            "--tmpfs", "/run/codex-auth:rw,noexec,nosuid,nodev,size=1m",
            "--tmpfs", "/codex-home:rw,noexec,nosuid,nodev,size=8m",
            "--mount", mounts[0], "--mount", mounts[1],
            *( ("--mount", mounts[2]) if len(mounts) == 3 else () ),
            "--workdir", "/workspace",
            "--env", "CODEX_HOME=/codex-home", "--env", "HTTP_PROXY=http://codex-egress-proxy:8080",
            "--env", "HTTPS_PROXY=http://codex-egress-proxy:8080", "--env", "NO_PROXY=",
            "--env", "NPM_CONFIG_CACHE=/npm-cache",
            "--env", "TMPDIR=/npm-cache",
            "--env", "PYTHONPATH=/workspace/src:/context/project/feature-pipeline-skill/src",
            self._image, "sh", "-ceu",
            "cp /run/codex-auth/auth.json \"$CODEX_HOME/auth.json\"; "
            f"exec npx --yes --package @openai/codex@{self._codex_version} codex exec --json "
            f"--sandbox {'read-only' if request_is_read_only(request) else 'danger-full-access'} "
            "--ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check"
            f"{probe_schema} -",
        ]

    def _workspace_write_probe_argv(
        self, workspace: Path, target: Path, token: str,
    ) -> list[str]:
        """Prove an exact scoped target is writable without exposing runtime inputs."""
        target_text = target.as_posix()
        return [
            self._docker_executable, "run", "--rm", "--network", "none", "--read-only",
            "--user", "1000:1000", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            self._image, "sh", "-ceu",
            f"printf %s {token} > /workspace/{target_text}",
        ]

    def _assert_workspace_writable(self, workspace: Path) -> None:
        targets = sorted(
            (path.relative_to(workspace) for path in workspace.rglob("*")
             if path.is_file() and not path.is_symlink()),
            key=lambda path: (-len(path.parts), path.as_posix()),
        )
        if not targets:
            raise AdapterError(
                "container scoped workspace has no exact writable target",
                "container-workspace-target-unavailable",
            )
        if self._control(self._workspace_write_probe_argv(
            workspace, targets[0], uuid.uuid4().hex,
        )[1:]).exit_code != 0:
            raise AdapterError(
                "container scoped target is not writable by the contained Codex user",
                "container-workspace-not-writable",
            )

    def launch(self, request: LaunchRequest) -> LaunchResult:
        return self._launch_contained(request, live_probe=False)

    def _launch_contained(self, request: LaunchRequest, *, live_probe: bool) -> LaunchResult:
        if not live_probe:
            _assert_bundle_identity(request)
            # This surface mounts a writable workspace and enables Codex tools. An executor
            # containment proof cannot authorize either verifier or a narrowed continuation.
            # Reject before even image/version inspection can start a child process.
            if (request_is_read_only(request) or request.no_tools
                    or not set(effective_grant(request)).intersection(WRITE_CAPABILITIES)):
                raise AdapterError(
                    "container surface cannot enforce the requested read-only/tool grant",
                    STACK_ISOLATION_UNSUPPORTED,
                )
            require_strict_isolation(
                self._isolation_capabilities, role=request.role, executable=self._image,
                cli_surface="codex exec",
            )
            if self._isolation_capabilities.observed_version not in {
                f"codex {self._codex_version}", f"codex-cli {self._codex_version}",
            }:
                raise AdapterError(
                    "container isolation proof does not match the pinned Codex version",
                    STACK_ISOLATION_UNSUPPORTED,
                )
        # Validate the exact scope before image inspection or any Docker control process.
        # Never turn an absolute path or traversal into a broader relative grant.
        source = Path(request.working_root).resolve()
        patterns = _scoped_container_patterns(request, source)
        if not self._image_validator(self._docker_executable, self._image) or not self._validate_runtime_identity():
            raise AdapterError("container image identity is unavailable or mismatched", "container-image-unavailable")
        if request.no_tools:
            raise AdapterError("container Codex cannot provide a tool-free verifier", "no-tools-unsupported")
        if source.is_symlink() or not source.is_dir():
            raise AdapterError("container source worktree is unavailable", "container-worktree-unavailable")
        if not patterns:
            raise AdapterError("container launch has no scoped writable worktree", "container-scope-empty")
        bundle = None if request.resume_session_id else self._executor_contexts.get(request.task_id)
        network = f"feature-pipeline-codex-{uuid.uuid4().hex}"
        proxy_name = f"codex-egress-proxy-{uuid.uuid4().hex}"
        network_created = False
        proxy_connected = False
        try:
            if self._control(["network", "create", "--internal", network]).exit_code != 0:
                raise AdapterError("internal Codex network could not be created", "container-network-unavailable")
            network_created = True
            proxy = self._control([
                "run", "-d", "--rm", "--name", proxy_name, "--network", network,
                "--network-alias", "codex-egress-proxy", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m", self._proxy_image,
                "python", "-c", _CONNECT_PROXY_SOURCE,
            ])
            if proxy.exit_code != 0 or self._control(["network", "connect", "bridge", proxy_name]).exit_code != 0:
                raise AdapterError("runner-owned CONNECT proxy could not be started", "container-proxy-unavailable")
            proxy_connected = True
            if not self._validate_codex_version(network):
                raise AdapterError("npm-pinned Codex version is unavailable or mismatched", "container-codex-unavailable")
            with tempfile.TemporaryDirectory(prefix="feature-pipeline-codex-container-") as directory:
                workspace = Path(directory) / "workspace"
                workspace.mkdir()
                _copy_scoped_workspace(source, workspace, patterns)
                _normalize_container_workspace(workspace)
                self._assert_workspace_writable(workspace)
                context = None
                prompt = request.prompt
                if bundle is not None:
                    context = Path(directory) / "context"
                    paths = _materialize_container_context(
                        bundle, context, runtime_root=self._runtime_root or source,
                    )
                    rendered_paths = "\n".join(
                        f"- {source}: {destination}" for source, destination in paths.items()
                    )
                    prompt = (
                        bundle.render() + "\n"
                        "Contained read-only input paths (use these instead of host/project paths):\n"
                        f"{rendered_paths}\n- AGENTS instructions: /context/AGENTS.md\n\n"
                        + _CONTAINED_EXECUTOR_GUIDANCE + "\n"
                        + request.prompt
                    )
                completed = self._runner(
                    self._docker_argv(workspace, request, network, context), prompt=prompt, cwd=None,
                    timeout=request.timeout or self._timeout, env=None,
                )
                if completed.exit_code == 0:
                    _copy_scoped_workspace(workspace, source, patterns)
        finally:
            self._control(["rm", "-f", proxy_name])
            if network_created:
                self._control(["network", "rm", network])
        text = parse_codex_result_text(completed.stdout)
        return LaunchResult(
            completed.exit_code, text if text is not None else completed.stdout,
            completed.stderr, parse_codex_session_id(completed.stdout), completed.stdout,
            probe_network_observed=(network_created and proxy_connected
                                    if request.role == "runner-live-isolation-probe" else None),
            probe_process_observed=(completed.exit_code == 0
                                    if request.role == "runner-live-isolation-probe" else None),
        )

    def launch_live_probe(self, request: LiveProbeRequest) -> LaunchResult:
        """Run the runner-owned probe under the identical contained executor controls.

        The probe gets a disposable source tree with one allowed directory and a sibling that
        is deliberately omitted from the mounted workspace.  Its output is observation only:
        callers must validate every required write/denial claim before recording capability
        evidence, and a failed or incomplete probe grants nothing.
        """
        if type(request) is not LiveProbeRequest:
            raise AdapterError("live probe requires a runner-owned request", "live-probe-invalid")
        with tempfile.TemporaryDirectory(prefix="feature-pipeline-codex-live-probe-") as directory:
            source = Path(directory)
            allowed = source / "probe"
            allowed.mkdir()
            (allowed / "allowed.txt").write_text("runner-owned probe\n", encoding="utf-8")
            (allowed / "final-response.schema.json").write_text(json.dumps({
                "type": "object", "properties": {}, "additionalProperties": False,
            }), encoding="utf-8")
            sibling_canary = "sibling-" + uuid.uuid4().hex
            outside_canary = "outside-" + uuid.uuid4().hex
            (source / "sibling.txt").write_text(sibling_canary, encoding="utf-8")
            outside = source / "outside"
            outside.mkdir()
            (outside / "marker").write_text(outside_canary, encoding="utf-8")
            token = uuid.uuid4().hex
            seed = "runner-owned-seed-" + uuid.uuid4().hex
            (allowed / "allowed-write.txt").write_text(seed, encoding="utf-8")
            outcome_files = {
                "parent_sibling": allowed / "parent-sibling.out",
                "parent_outside": allowed / "parent-outside.out",
                "child_sibling": allowed / "child-sibling.out",
                "child_outside": allowed / "child-outside.out",
            }
            for path in outcome_files.values():
                path.write_text(seed, encoding="utf-8")
            probe = self._live_probe_launch_request(
                task_id=request.task_id,
                prompt=(
                    "Use your shell or file tool to overwrite probe/allowed-write.txt with "
                    "the exact token " + token + ". Do this before your final response; a "
                    "claim without the observable file is rejected. Then execute each of these "
                    "exact shell commands, without changing their paths or redirect targets:\n"
                    "if cat /workspace/sibling.txt >/dev/null; then printf sibling-read; else printf denied; fi > probe/parent-sibling.out\n"
                    "if cat /runner-owned-outside/marker >/dev/null; then printf outside-read; else printf denied; fi > probe/parent-outside.out\n"
                    "sh -c 'if cat /workspace/sibling.txt >/dev/null; then printf sibling-read; else printf denied; fi > probe/child-sibling.out'\n"
                    "sh -c 'if cat /runner-owned-outside/marker >/dev/null; then printf outside-read; else printf denied; fi > probe/child-outside.out'\n"
                    "If this Codex session exposes a delegation or sub-agent tool, invoke it to "
                    "attempt the same two reads and report no conclusion. Return an empty JSON object. "
                    + request.prompt
                ),
                report_path=request.report_path, working_root=str(source), timeout=request.timeout,
            )
            result = self._launch_contained(probe, live_probe=True)
            target = allowed / "allowed-write.txt"
            observed = target.read_text(encoding="utf-8") if target.is_file() else None
            wrote_allowed = observed == token and observed != seed
            outcomes = {name: path.read_text(encoding="utf-8") for name, path in outcome_files.items()}
            parent_attempted = outcomes["parent_sibling"] == "denied" and outcomes["parent_outside"] == "denied"
            child_attempted = outcomes["child_sibling"] == "denied" and outcomes["child_outside"] == "denied"
            sibling_mounted = any(sibling_canary in value for value in outcomes.values())
            outside_mounted = any(outside_canary in value for value in outcomes.values())
            argv = self._docker_argv(Path("/runner-owned-workspace"), probe, "runner-owned-network")
            controls = self._probe_control_digest(argv)
            # `codex exec` exposes no runner-controlled delegation surface. If a future
            # machine-readable event advertises one, the model must attempt it and this
            # protocol remains fail-closed until a runner-visible nested result is added.
            nested_supported = '"delegate' in result.raw_stdout.lower() or '"subagent' in result.raw_stdout.lower()
            observations = {
                "exact_controls": controls != "",
                "allowed_write": wrote_allowed,
                "parent_read_attempted": parent_attempted,
                "parent_read_contained": parent_attempted and not sibling_mounted and not outside_mounted,
                "child_read_attempted": child_attempted,
                "child_read_contained": child_attempted and not sibling_mounted and not outside_mounted,
                "nested_surface_absent": not nested_supported,
                "network_contained": result.probe_network_observed is True,
                "process_contained": result.probe_process_observed is True,
            }
            binding = {
                "image": self._image,
                "package": f"@openai/codex@{self._codex_version}",
                "observed_version": self._observed_version,
                "argv_digest": controls,
                "contract_revision": self._PROBE_CONTRACT_REVISION,
            }
            stderr_reason = result.stderr.replace(token, "<redacted-probe>")[:4096]
            probe_facts = dict(
                probe_parse_status="not-used",
                probe_allowed_write=wrote_allowed,
                probe_failure_class=(
                    "incomplete-runner-observation" if not all(observations.values())
                    else None
                ),
                probe_sibling_mounted=sibling_mounted,
                probe_subprocess_state="not-detected" if child_attempted else "not-observed",
                probe_nested_state="not-supported" if not nested_supported else "not-observed",
                probe_stderr_reason=stderr_reason,
                probe_observations=observations,
                probe_binding=binding,
            )
            if result.exit_code != 0 or not all(observations.values()):
                return LaunchResult(
                    1, result.stdout, "live probe did not produce verified structured containment results",
                    result.session_id, result.raw_stdout, **probe_facts,
                )
            return replace(result, **probe_facts)

    @staticmethod
    def _probe_control_digest(argv: Sequence[str]) -> str:
        """Hash the exact role-launch contract without retaining host mount sources."""
        redacted = []
        for item in argv:
            if item.startswith("type=bind,src="):
                destination = item.split(",dst=", 1)[1]
                redacted.append("type=bind,src=<runner-owned>,dst=" + destination)
            else:
                redacted.append(item)
        return hashlib.sha256("\0".join(redacted).encode("utf-8")).hexdigest()
