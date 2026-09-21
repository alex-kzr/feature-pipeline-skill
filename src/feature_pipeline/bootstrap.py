"""Production composition root for the feature-pipeline CLI."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping, Sequence

from feature_pipeline.application.compile_plan import (
    ControlOverrides,
    ShallowTaskInput,
    compile_run_plan,
)
from feature_pipeline.application.profile_bridge import compiled_profile_from_core, route_reasons
from feature_pipeline.application.results import PipelineResult
from feature_pipeline.application.verified_reuse import (
    EvidenceEligibilityError,
    VerifiedEvidenceStore,
)
from feature_pipeline.cli.commands import RunCommand
from feature_pipeline.cli.errors import CliError
from feature_pipeline.cli.parser import EXIT_ERROR, FEATURE_RE, SOURCE_FEATURE_RE
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.ports.adapters import (
    CLAUDE,
    CODEX,
    AdapterCapabilities,
    AdapterRegistry,
    IsolationCapabilityProof,
    STRICT_ISOLATION_CAPABILITIES,
)
from feature_pipeline.contracts import SchemaError, TaskSpec

from pipeline_core.adapters import (
    CONTEXT_UNAVAILABLE,
    REQUIRED_INPUT_INVALID,
    Adapter,
    AdapterError,
    ClaudeAdapter,
    CLAUDE_ISOLATION_CAPABILITIES,
    CodexAdapter,
    DockerCodexAdapter,
    CODEX_ISOLATION_CAPABILITIES,
    ContextEntry,
    ExecutorContextBundle,
    RequiredInput,
    derive_required_input_dirs,
)
from pipeline_core.execution import (
    ExecuteControls,
    ExecuteRequest,
    ExecutionError,
    effective_task_spec,
    _resolve_source_run_dir,
    _validate_attestation_scope,
    execute_run,
    replacement_feature,
)
from pipeline_core.integrations import load_tool_integration
from pipeline_core.plan_md import MarkdownPlanError, load_markdown_plan
from pipeline_core.post_task import POST_TASK_STAGES
from pipeline_core.profiles import Anchors
from pipeline_core.project_profile import load_runnable_profile
from pipeline_core.plan import (
    AmendmentError,
    AmendmentRequest,
    build_amendment_revision,
    canonical_amendment_fields,
    contract_digest,
)
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.redaction import build_rules, redact_text
from pipeline_core.release import ReleasePolicy, load_release_policy
from pipeline_core.stages import plan_release_dry_run
from pipeline_core.state import Run, StateError, pid_alive, read_lease
from pipeline_core.task_files import load_task_spec
from pipeline_core.verification import (
    RunnerOwnedIsolationProofVerifier,
    VerifierAnchors,
    VerifierLaunchers,
)

ExecuteAdapters = Callable[[Path, Path, Path], tuple[object, VerifierLaunchers, dict[str, bool]]]

#: **KLC-03**. The active-board file :func:`run_execute` projects lifecycle transitions onto
#: for a Markdown-backed board plan (``--plan`` naming a ``.md`` plan beside ``tasks/*.md``
#: task files — the same convention :mod:`pipeline_core.plan_md` reads). A boardless JSON plan
#: never sets ``ExecuteRequest.board_path``, so it stays exactly as projection-free as before.
BOARD_RELATIVE_PATH = "docs/kanban.md"


@dataclass(frozen=True)
class AdapterRuntime:
    """Resolved runtime roots a concrete adapter factory needs."""

    project_dir: Path
    agents_root: Path
    core_root: Path
    scope_roots: tuple[tuple[str, Path], ...]
    #: Runner-owned immutable executor context, keyed by task id. Empty unless a caller
    #: (``run_execute``) built bundles for the selected tasks.
    executor_contexts: Mapping[str, ExecutorContextBundle] = field(default_factory=dict)
    #: Runner-owned minimal mandatory-input directory grants, keyed by task id (REC-05).
    #: Empty unless ``run_execute`` derived them for a nested-root task.
    required_input_dirs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Runner-owned minimal declared-``allowed_scope`` directory grants outside a task's own
    #: working root, keyed by task id (CSR-01). Empty unless ``run_execute`` derived them for
    #: a nested-root task whose allowed scope names a repository-internal path it cannot
    #: otherwise reach.
    scope_dirs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: The exact resolved record used by plan compilation.  Concrete adapters must enforce
    #: this same record rather than silently falling back to a local default.
    isolation_capabilities: AdapterCapabilities | None = None
    isolation_probe_validator: Callable[[str | Sequence[str], str], AdapterCapabilities] | None = None


@dataclass(frozen=True)
class AdapterFactory:
    """Factory plus declared capabilities for one execution adapter."""

    name: str
    create: Callable[[AdapterRuntime], Adapter]
    available: Callable[[], bool]
    supports_resume: bool
    supports_read_only: bool
    supports_write: bool
    default_timeout_s: float = 3600.0
    isolation_capabilities: AdapterCapabilities | None = None
    isolation_probe_validator: Callable[[str | Sequence[str], str], AdapterCapabilities] | None = None

    def capabilities(self) -> AdapterCapabilities:
        declared = self.isolation_capabilities
        return AdapterCapabilities(
            name=self.name,
            available=self.available(),
            supports_resume=self.supports_resume,
            supports_read_only=self.supports_read_only,
            supports_write=self.supports_write,
            default_timeout_s=self.default_timeout_s,
            supports_bundle_validated=(
                declared.supports_bundle_validated if declared is not None else False
            ),
            supports_discovery_isolated=(
                declared.supports_discovery_isolated if declared is not None else False
            ),
            supports_skill_reads_enforced=(
                declared.supports_skill_reads_enforced if declared is not None else False
            ),
            supports_subprocess_isolated=(
                declared.supports_subprocess_isolated if declared is not None else False
            ),
            supports_nested_delegation_isolated=(
                declared.supports_nested_delegation_isolated if declared is not None else False
            ),
            isolation_proofs=declared.isolation_proofs if declared is not None else (),
            runtime=declared.runtime if declared is not None else "",
            cli_surface=declared.cli_surface if declared is not None else "",
            observed_version=declared.observed_version if declared is not None else "",
        )


@dataclass(frozen=True)
class BootstrapComposition:
    """Production composition: registry data and adapter factories from one source."""

    project_dir: Path
    agents_root: Path
    core_root: Path
    logical_scope_roots: tuple[tuple[str, str], ...]
    factories: tuple[AdapterFactory, ...]
    adapter_registry: AdapterRegistry
    executor_contexts: Mapping[str, ExecutorContextBundle] = field(default_factory=dict)
    required_input_dirs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    scope_dirs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def make_execute_adapters(
        self, adapter_name: str | None = None, *, run: Run | None = None,
    ) -> tuple[Adapter, VerifierLaunchers, dict[str, bool]]:
        registry = self.adapter_registry
        resolved = registry.select(adapter_name)
        factory = next(factory for factory in self.factories if factory.name == resolved.name)
        runtime = AdapterRuntime(
            project_dir=self.project_dir,
            agents_root=self.agents_root,
            core_root=self.core_root,
            scope_roots=resolve_scope_roots(
                self.project_dir,
                self.agents_root,
                self.core_root,
                dict(self.logical_scope_roots),
            ),
            executor_contexts=self.executor_contexts,
            required_input_dirs=self.required_input_dirs,
            scope_dirs=self.scope_dirs,
            isolation_capabilities=resolved,
            isolation_probe_validator=factory.isolation_probe_validator,
        )
        executor = factory.create(runtime)
        # Docker's executor surface cannot serve read-only verifiers. Retain the proof
        # diagnostic hook for callers, but verification keeps an incompatible launch blocked:
        # containment evidence cannot supply either independent semantic verdict.
        deterministic_isolation = (
            RunnerOwnedIsolationProofVerifier(run)
            if run is not None and isinstance(executor, DockerCodexAdapter)
            and all(resolved.has(token) for token in STRICT_ISOLATION_CAPABILITIES)
            else None
        )
        launchers = VerifierLaunchers(
            task=executor,
            test=executor,
            deterministic_isolation=deterministic_isolation,
        )
        environment = {cap.name: cap.available for cap in registry.adapters}
        environment.setdefault(CODEX, False)
        return executor, launchers, environment


def codex_factory(
    *,
    resolver: Callable[[], str | Sequence[str] | None] | None = None,
    isolation_probe_validator: Callable[[str | Sequence[str], str], AdapterCapabilities] | None = None,
) -> AdapterFactory:
    """Declare the Codex runtime and construct it with the run's resolved anchors."""
    def create_codex(runtime: AdapterRuntime) -> CodexAdapter:
        return CodexAdapter(
            resolver=resolver,
            working_root=runtime.project_dir,
            scope_roots=runtime.scope_roots,
            required_input_dirs=runtime.required_input_dirs,
            task_scope_dirs=runtime.scope_dirs,
            isolation_capabilities=runtime.isolation_capabilities,
            isolation_probe_validator=runtime.isolation_probe_validator,
        )

    def codex_available() -> bool:
        return CodexAdapter(resolver=resolver).available()

    return AdapterFactory(
        name=CODEX,
        create=create_codex,
        available=codex_available,
        supports_resume=False,
        supports_read_only=True,
        supports_write=True,
        # A durable observation is diagnostic evidence, not a positive strict capability.
        # Keep the production composition fail-closed until an executor-equivalent probe
        # has a separately implemented and verified proof contract.
        isolation_capabilities=CODEX_ISOLATION_CAPABILITIES,
        isolation_probe_validator=isolation_probe_validator,
    )


def docker_codex_factory(
    *,
    image: str,
    proxy_image: str,
    codex_version: str,
    auth_file: Path,
    docker_executable: str = "docker",
    image_validator: Callable[[str, str], bool] | None = None,
    isolation_capabilities: AdapterCapabilities | None = None,
) -> AdapterFactory:
    """Build the opt-in, digest-pinned Docker Codex path.

    This is intentionally not part of :func:`_production_factories`: a caller must explicitly
    supply both the pinned image and runner-selected auth file.  The ordinary host Codex path
    therefore remains fail-closed and cannot fall back to Docker (or vice versa).
    """
    def create_container_codex(runtime: AdapterRuntime) -> DockerCodexAdapter:
        return DockerCodexAdapter(
            image=image, proxy_image=proxy_image, codex_version=codex_version,
            auth_file=auth_file, docker_executable=docker_executable,
            isolation_capabilities=runtime.isolation_capabilities,
            image_validator=image_validator,
            executor_contexts=runtime.executor_contexts,
            runtime_root=runtime.core_root,
        )

    return AdapterFactory(
        name=CODEX,
        create=create_container_codex,
        available=lambda: shutil.which(docker_executable) is not None and auth_file.is_file(),
        supports_resume=False,
        supports_read_only=True,
        supports_write=True,
        isolation_capabilities=isolation_capabilities or CODEX_ISOLATION_CAPABILITIES,
    )


def docker_capabilities_from_durable_proof(
    run: Run,
    *,
    task_ids: Sequence[str],
    image: str,
    proxy_image: str,
    codex_version: str,
    auth_file: Path,
) -> AdapterCapabilities:
    """Materialize only one digest-checked, exact Docker containment proof.

    A probe row is audit evidence, not a capability by itself.  It becomes capability evidence
    only when its canonical artifact still hashes to the row and binds the selected image,
    package/version, canonical Docker argv, current task contract, and probe contract.
    """
    baseline = CODEX_ISOLATION_CAPABILITIES
    expected = DockerCodexAdapter.probe_binding(
        image=image, proxy_image=proxy_image, codex_version=codex_version, auth_file=auth_file,
    )
    expected_observed = {f"codex {codex_version}", f"codex-cli {codex_version}"}
    required_observations = {
        "exact_controls", "allowed_write", "parent_read_attempted", "parent_read_contained",
        "child_read_attempted", "child_read_contained", "nested_surface_absent",
        "network_contained", "process_contained",
    }
    selected = set(task_ids)
    # Capabilities are adapter-wide once composed.  Do not let one task's proof authorize a
    # mixed-contract dispatch set; the strict caller must select the exact proved task.
    if len(selected) != 1:
        return baseline
    for row in reversed(run.live_probe_evidence):
        if not isinstance(row, dict) or row.get("task_id") not in selected:
            continue
        task_id = row["task_id"]
        try:
            task = run.task(task_id)
            proof_rel = str(row["proof_path"])
            if proof_rel != f"reports/{task_id}/live-probe.json":
                continue
            proof_path = Path(run.run_dir) / proof_rel
            content = proof_path.read_bytes()
            proof = json.loads(content)
        except (KeyError, OSError, json.JSONDecodeError):
            continue
        if (
            hashlib.sha256(content).hexdigest() != row.get("proof_digest")
            or row.get("adapter") != CODEX
            or row.get("role") != "runner-live-isolation-probe"
            or row.get("disposition") != "CONTAINMENT_PROVEN"
            or row.get("task_contract_digest") != task.task_contract_digest
            or row.get("task_contract_revision") != task.current_revision
            or row.get("cli_version") not in expected_observed
            or not isinstance(proof, dict)
            or proof.get("schema_version") != 1
            or proof.get("task_id") != task_id
            or proof.get("attempt_id") != row.get("attempt_id")
            or proof.get("role") != "runner-live-isolation-probe"
            or proof.get("disposition") != "CONTAINMENT_PROVEN"
            or proof.get("cleanup_removed") is not True
            or proof.get("exit_zero") is not True
        ):
            continue
        observations = proof.get("observations")
        binding = proof.get("binding")
        if not isinstance(observations, dict) or set(observations) != required_observations:
            continue
        if any(value is not True for value in observations.values()) or not isinstance(binding, dict):
            continue
        observed = binding.get("observed_version")
        if observed not in expected_observed or row.get("cli_version") != observed:
            continue
        bound = {**expected, "observed_version": observed}
        if binding != bound:
            continue
        proofs = tuple(
            IsolationCapabilityProof(token, CODEX, image, "codex exec", observed,
                                     f"{row['proof_path']}#{row['proof_digest']}")
            for token in STRICT_ISOLATION_CAPABILITIES
        )
        return AdapterCapabilities(
            CODEX, True, False, True, True,
            supports_bundle_validated=True, supports_discovery_isolated=True,
            supports_skill_reads_enforced=True, supports_subprocess_isolated=True,
            supports_nested_delegation_isolated=True, isolation_proofs=proofs,
            runtime=image, cli_surface="codex exec", observed_version=observed,
        )
    return baseline


def docker_codex_factories(
    *, image: str, proxy_image: str, codex_version: str, auth_file: Path,
    isolation_capabilities: AdapterCapabilities | None = None,
) -> tuple[AdapterFactory, ...]:
    """Return the normal Claude entry plus one explicitly selected Docker Codex entry."""
    factories = _production_factories(Path("."))
    return (factories[0], docker_codex_factory(
        image=image, proxy_image=proxy_image, codex_version=codex_version, auth_file=auth_file,
        isolation_capabilities=isolation_capabilities,
    ))


def docker_factories_for_command(
    command: RunCommand, *, run: Run | None = None, task_ids: Sequence[str] = (),
) -> tuple[AdapterFactory, ...] | None:
    """Select Docker only from complete typed controls; host Codex remains the default."""
    if command.codex_runtime in {None, "host"}:
        return None
    if command.adapter not in {None, CODEX}:
        raise AdapterError("Docker Codex runtime requires --adapter codex", "docker-runtime-invalid")
    values = (
        command.docker_codex_image, command.docker_proxy_image,
        command.docker_codex_version, command.docker_codex_auth_file,
    )
    if not all(isinstance(value, str) and value for value in values):
        raise AdapterError(
            "Docker Codex runtime requires pinned images, version, and auth file",
            "docker-runtime-incomplete",
        )
    capabilities = (
        docker_capabilities_from_durable_proof(
            run, task_ids=task_ids, image=command.docker_codex_image,
            proxy_image=command.docker_proxy_image, codex_version=command.docker_codex_version,
            auth_file=Path(command.docker_codex_auth_file),
        ) if run is not None else None
    )
    return docker_codex_factories(
        image=command.docker_codex_image,
        proxy_image=command.docker_proxy_image,
        codex_version=command.docker_codex_version,
        auth_file=Path(command.docker_codex_auth_file),
        isolation_capabilities=capabilities,
    )


def _docker_auth_file_control_value(value: str | None, project_dir: Path) -> str | None:
    """Persist a Docker auth-file identity without leaking an absolute host path."""
    if value is None:
        return None
    path = Path(value).resolve()
    default_auth = (Path.home() / ".codex" / "auth.json").resolve()
    if path == default_auth:
        return "<home>/.codex/auth.json"
    try:
        return path.relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        raise CliError(
            EXIT_ERROR,
            "docker-runtime-invalid: Docker Codex auth file must be project-local",
        ) from None


def _production_factories(project_dir: Path) -> tuple[AdapterFactory, ...]:
    def create_claude(runtime: AdapterRuntime) -> ClaudeAdapter:
        return ClaudeAdapter(
            working_root=str(runtime.project_dir),
            scope_roots=runtime.scope_roots,
            executor_contexts=runtime.executor_contexts,
            required_input_dirs=runtime.required_input_dirs,
            task_scope_dirs=runtime.scope_dirs,
            isolation_capabilities=runtime.isolation_capabilities,
            isolation_probe_validator=runtime.isolation_probe_validator,
        )

    def claude_available() -> bool:
        return ClaudeAdapter().available()

    return (
        AdapterFactory(
            name=CLAUDE,
            create=create_claude,
            available=claude_available,
            supports_resume=True,
            supports_read_only=True,
            supports_write=True,
            isolation_capabilities=CLAUDE_ISOLATION_CAPABILITIES,
        ),
        codex_factory(
        ),
    )


def build_bootstrap(
    project_dir: Path,
    agents_root: Path,
    core_root: Path,
    factories: Sequence[AdapterFactory] | None = None,
    *,
    logical_paths: Mapping[str, str] | None = None,
    executor_contexts: Mapping[str, ExecutorContextBundle] | None = None,
    required_input_dirs: Mapping[str, Sequence[str]] | None = None,
    scope_dirs: Mapping[str, Sequence[str]] | None = None,
) -> BootstrapComposition:
    registered_factories = (
        tuple(factories) if factories is not None else _production_factories(Path(project_dir))
    )
    paths = logical_paths or {"agents": ".agents", "core": "core"}
    return BootstrapComposition(
        Path(project_dir),
        Path(agents_root),
        Path(core_root),
        tuple((name, paths[name]) for name in ("agents", "core")),
        registered_factories,
        AdapterRegistry(tuple(factory.capabilities() for factory in registered_factories)),
        dict(executor_contexts or {}),
        {key: tuple(value) for key, value in dict(required_input_dirs or {}).items()},
        {key: tuple(value) for key, value in dict(scope_dirs or {}).items()},
    )


def project_relative(path: Path, project_dir: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return None


def _project_logical_source(path: str | Path, project_dir: Path) -> str:
    """Normalize one resolved mandatory input to a project-relative logical source.

    Task specs may have passed through a loader that resolves their path already.  That
    physical path is valid only when it remains below the project anchor; returning its
    relative logical spelling prevents a drive-qualified host path reaching grants or worker
    context.
    """
    root = Path(project_dir).resolve()
    raw = Path(path)
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        raise AdapterError(
            f"mandatory input {str(path)!r} resolves outside the project anchor",
            REQUIRED_INPUT_INVALID,
        ) from None


def load_execute_specs(
    plan_path: Path, feature_override: str | None
) -> tuple[str | None, list[TaskSpec]]:
    """Resolve an execute-mode plan to complete task specifications."""
    if plan_path.suffix.lower() == ".md":
        try:
            feature, entries = load_markdown_plan(plan_path)
        except MarkdownPlanError as exc:
            raise CliError(EXIT_ERROR, str(exc)) from None
        tasks_dir = plan_path.parent / "tasks"
        specs: list[TaskSpec] = []
        for entry in entries:
            tid = entry["id"]
            matches = sorted(tasks_dir.glob(f"{tid}_*.md"))
            if not matches:
                raise CliError(
                    EXIT_ERROR,
                    f"execute mode: no task file 'tasks/{tid}_*.md' beside the plan",
                )
            try:
                specs.append(load_task_spec(matches[0]))
            except SchemaError as exc:
                raise CliError(EXIT_ERROR, f"{tid}: {exc}") from None
        return (feature_override or feature), specs

    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(EXIT_ERROR, "plan file not found") from None
    except json.JSONDecodeError:
        raise CliError(EXIT_ERROR, "plan file is not valid JSON") from None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise CliError(EXIT_ERROR, "plan must be an object with a non-empty 'tasks' list")
    specs = []
    seen: set[str] = set()
    for entry in tasks:
        if not isinstance(entry, dict):
            raise CliError(EXIT_ERROR, "every plan task must be an object")
        kwargs = dict(entry)
        if "task_type" not in kwargs and "type" in kwargs:
            kwargs["task_type"] = kwargs.pop("type")
        if "allowed_scope" not in kwargs:
            raise CliError(
                EXIT_ERROR,
                "execute mode needs full task metadata: give each JSON task 'task_type', "
                "'executor', 'allowed_scope', and 'acceptance_criteria', or point --plan at a "
                "Markdown plan with task files",
            )
        try:
            spec = TaskSpec.build(**kwargs)
        except (SchemaError, TypeError) as exc:
            raise CliError(EXIT_ERROR, f"{entry.get('id', '?')}: {exc}") from None
        if spec.id in seen:
            raise CliError(EXIT_ERROR, f"duplicate task id in plan: {spec.id}")
        seen.add(spec.id)
        specs.append(spec)
    feature = str(data["feature"]) if data.get("feature") else None
    return (feature_override or feature), specs


def parse_attestations(raw: list[str] | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if "=" not in item:
            raise CliError(
                EXIT_ERROR, f"--attest-dependency must be DEP_ID=SOURCE_FEATURE: '{item}'"
            )
        dep_id, source_feature = item.split("=", 1)
        if not dep_id:
            raise CliError(EXIT_ERROR, f"--attest-dependency has an empty DEP_ID: '{item}'")
        if not SOURCE_FEATURE_RE.match(source_feature):
            raise CliError(
                EXIT_ERROR,
                "--attest-dependency source feature must be a bare directory name (letters, "
                f"digits, '-', '_' only; no path separator, '.', '..', or absolute/drive path): "
                f"'{source_feature}'",
            )
        parsed.append((dep_id, source_feature))
    return tuple(parsed)


def resolve_attested_dependency_ids(
    *,
    raw_attestations: list[str] | None,
    selected_task: str | None,
    plan_tasks: Sequence[dict],
    definitions: Mapping[str, object],
    run_dir: Path,
    repo_root: Path,
    prompt_path: Path,
    plan_path: Path,
    resolve_sources: bool = True,
) -> set[str]:
    attestations = parse_attestations(raw_attestations)
    if not attestations:
        return set()
    by_id = {t["id"]: SimpleNamespace(depends_on=t["depends_on"]) for t in plan_tasks}
    controls = ExecuteControls(task=selected_task, attested_dependencies=attestations)
    try:
        _validate_attestation_scope(controls, by_id)
        if not resolve_sources:
            return {dep_id for dep_id, _ in attestations}
        store = VerifiedEvidenceStore(run_dir.parent, repo_root)
        attested_ids = set()
        for dep_id, source_feature in attestations:
            store.find_at(
                _resolve_source_run_dir(run_dir, source_feature),
                definitions[dep_id],
            )
            attested_ids.add(dep_id)
        return attested_ids
    except (ExecutionError, EvidenceEligibilityError) as exc:
        raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None


def resolve_add_dirs(project_dir: Path, *anchors: Path) -> tuple[Path, ...]:
    """Resolve anchors and keep only roots not already covered by an earlier grant."""
    covered: list[Path] = [Path(project_dir).resolve()]
    granted: list[Path] = []
    for anchor in anchors:
        resolved = Path(anchor).resolve()
        if any(resolved.is_relative_to(root) for root in covered):
            continue
        granted.append(resolved)
        covered.append(resolved)
    return tuple(granted)


def resolve_scope_roots(
    project_dir: Path,
    agents_root: Path,
    core_root: Path,
    logical_paths: Mapping[str, str],
) -> tuple[tuple[str, Path], ...]:
    """Map external runtime anchors to the logical roots task scopes may name.

    Roots already inside the project workspace need no ``--add-dir`` grant.  The retained
    logical names are explicit adapter capabilities, rather than a list of directories handed
    to every role.
    """
    external = resolve_add_dirs(project_dir, agents_root, core_root)
    by_path = {path.resolve(): path for path in external}
    roots: list[tuple[str, Path]] = []
    for name, physical in (("agents", agents_root), ("core", core_root)):
        logical = logical_paths[name]
        resolved = Path(physical).resolve()
        if resolved in by_path:
            roots.append((logical, by_path[resolved]))
    return tuple(roots)


def _redacted_logical_entry(
    kind: str, base: Path, rel: str, rules, *, host_roots: tuple[Path, ...] = (),
) -> ContextEntry | None:
    """Read ``base/rel``, redact known host roots, and return a digest-bound entry.

    Returns ``None`` only when the file cannot be read.  Content validation failures retain
    their typed invalid-bundle error rather than being misclassified as unavailable.
    """
    rel_posix = Path(rel).as_posix().lstrip("/")
    try:
        raw = (base / rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return ContextEntry.of(
        kind, rel_posix, redact_text(raw, rules), host_roots=host_roots,
    )


_CONTEXT_LINK_RE = re.compile(r"\[[^]]*\]\((?P<source>[^)#]+)(?:#[^)]*)?\)")
_CONTEXT_CODE_PATH_RE = re.compile(r"`(?P<source>[^`]+)`")
_SOURCE_LOCATION_SUFFIX_RE = re.compile(
    r"^(?P<path>.+?):[1-9]\d*(?:,[1-9]\d*)*(?::[1-9]\d*)?$"
)


def _declared_context_source(
    source: str, *, project_dir: Path, working_root: str, root: Path,
) -> str | None:
    """Resolve one safe declared source with a trailing positive line locator or list.

    A literal filename always wins.  A location suffix is removed only when its unsuffixed
    project- or working-root-relative file exists, so arbitrary colon-bearing text is never
    reinterpreted as a path.
    """
    def safe_path(value: str) -> bool:
        components = value.split("/")
        if (
            any(character.isspace() for character in value)
            or any(character in value for character in "*?")
            or value.startswith(("/", "./", "../"))
            or any(component in {"", ".", ".."} for component in components)
        ):
            return False
        if "/" not in value:
            return (
                (project_dir / value).is_file()
                or (project_dir / working_root / value).is_file()
            )
        return True

    def candidate_for(value: str) -> Path | None:
        if not safe_path(value):
            return None
        project_candidate = (project_dir / value).resolve()
        worker_candidate = (project_dir / working_root / value).resolve()
        for candidate in (project_candidate, worker_candidate):
            try:
                candidate.relative_to(root)
            except ValueError:
                return None
        if not project_candidate.exists() and worker_candidate.exists():
            return worker_candidate
        return project_candidate

    candidate = candidate_for(source)
    if candidate is None:
        return None
    if not candidate.is_file():
        match = _SOURCE_LOCATION_SUFFIX_RE.fullmatch(source)
        if match is not None:
            located = candidate_for(match.group("path"))
            if located is not None and located.is_file():
                candidate = located
        elif ":" in source:
            path, _, _ = source.partition(":")
            located = candidate_for(path)
            if located is not None and located.is_file():
                return None
    return candidate.relative_to(root).as_posix()


def _declared_context_sources(
    spec: TaskSpec,
    specs: Sequence[TaskSpec],
    *,
    project_dir: Path,
    working_root: str = ".",
) -> tuple[str, ...]:
    """Return only explicit project-local task prerequisites, in declaration order.

    Markdown links are task-declared inputs, rather than hints to discover an arbitrary project
    tree.  A task that explicitly requires the preceding review report receives the last prior
    review task's one concrete report path; absence of that report is a launch precondition, not
    an executor-side blocker.
    """
    task_source = _project_logical_source(spec.path, project_dir)
    task_path = project_dir / task_source
    try:
        text = task_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # A plan can legitimately be evaluated before a historical task file is materialized.
        # There is then no declaration to extract; preserve the existing no-bundle behavior.
        return ()
    sources: list[str] = []
    root = project_dir.resolve()
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line.strip().lower()
            continue
        for match in _CONTEXT_LINK_RE.finditer(line):
            source = match.group("source").strip()
            if not source or "://" in source or source.startswith(("/", "#")):
                continue
            candidate = (task_path.parent / source).resolve()
            try:
                logical = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if logical not in sources:
                sources.append(logical)

        if section not in {"## context", "## affected files / components"}:
            continue
        for match in _CONTEXT_CODE_PATH_RE.finditer(line):
            source = match.group("source").strip().replace("\\", "/")
            logical = _declared_context_source(
                source, project_dir=project_dir, working_root=working_root, root=root,
            )
            if logical is None:
                continue
            if logical not in sources:
                sources.append(logical)

    if "latest preceding review report" in text.lower():
        try:
            current = list(specs).index(spec)
        except ValueError:  # pragma: no cover - callers pass the source plan specs
            current = 0
        report: str | None = None
        for prior in reversed(specs[:current]):
            if "review" not in prior.title.lower():
                continue
            candidates = [
                path for path in prior.allowed_scope
                if path.startswith("docs/validation/") and path.endswith(".md")
                and "*" not in path and "?" not in path
            ]
            if len(candidates) == 1:
                report = candidates[0]
                break
        if report is None:
            raise AdapterError(
                f"declared preceding review context for {spec.id!r} is unavailable",
                CONTEXT_UNAVAILABLE,
            )
        if report not in sources:
            sources.append(report)
    return tuple(sources)


def _required_context_entry(kind: str, base: Path, rel: str, rules) -> ContextEntry:
    entry = _redacted_logical_entry(kind, base, rel, rules, host_roots=(base,))
    if entry is None:
        raise AdapterError(
            f"declared {kind} context {rel!r} is unavailable", CONTEXT_UNAVAILABLE
        )
    return entry


def build_executor_context_bundles(
    specs: Sequence[TaskSpec],
    *,
    project_dir: Path,
    agents_root: Path,
    plan_path: Path,
    prompt_path: Path,
    task_ids: Sequence[str] | None = None,
    plan_specs: Sequence[TaskSpec] | None = None,
    working_root_by_id: Mapping[str, str] | None = None,
) -> dict[str, ExecutorContextBundle]:
    """Assemble one runner-owned immutable context bundle per selected task.

    Each bundle carries the task's canonical contract plus every explicitly declared local
    prerequisite, plan, prompt, and required skill.  Every entry is redacted of known host
    roots and bound to a logical source and SHA-256 digest.  A missing declared input is a
    typed pre-launch failure; it is never silently omitted for an executor to discover later.
    """
    rules = build_rules(project_dir)
    prerequisite_specs = plan_specs if plan_specs is not None else specs
    wanted = set(task_ids) if task_ids is not None else {spec.id for spec in specs}
    working_roots = dict(working_root_by_id or {})
    plan_rel = _project_logical_source(plan_path, project_dir)
    plan_entry = _required_context_entry("plan", project_dir, plan_rel, rules)
    prompt_rel = _project_logical_source(prompt_path, project_dir)
    prompt_entry = _required_context_entry("prompt", project_dir, prompt_rel, rules)
    bundles: dict[str, ExecutorContextBundle] = {}
    for spec in specs:
        if spec.id not in wanted or not spec.path:
            continue
        task_rel = _project_logical_source(spec.path, project_dir)
        # Historical gate/CLI tests and plan-only routes may carry a task spec before a physical
        # task contract exists.  Preserve their external behavior: no contract means no bundle.
        # Once a contract is readable, every explicit prerequisite it declares is mandatory.
        task_entry = _redacted_logical_entry(
            "task", project_dir, task_rel, rules, host_roots=(project_dir, agents_root),
        )
        if task_entry is None:
            continue
        entries: list[ContextEntry] = [task_entry]
        entries.append(plan_entry)
        if prompt_rel != plan_rel:
            entries.append(prompt_entry)
        standard_sources = {task_rel, plan_rel, prompt_rel}
        for input_rel in _declared_context_sources(
            spec, prerequisite_specs, project_dir=project_dir,
            working_root=working_roots.get(spec.id, "."),
        ):
            if input_rel not in standard_sources:
                entries.append(_required_context_entry("input", project_dir, input_rel, rules))
        for skill_rel in spec.required_skills:
            skill_entry = _redacted_logical_entry(
                "skill", project_dir, skill_rel, rules, host_roots=(project_dir, agents_root),
            )
            if skill_entry is None and not Path(skill_rel).is_absolute():
                agents_rel = skill_rel.removeprefix(".agents/")
                skill_entry = _redacted_logical_entry(
                    "skill", agents_root, agents_rel, rules,
                    host_roots=(project_dir, agents_root),
                )
                if skill_entry is not None and agents_rel != skill_rel:
                    skill_entry = ContextEntry.of(
                        "skill", skill_rel, skill_entry.content,
                    )
            if skill_entry is None:
                raise AdapterError(
                    f"declared skill context {skill_rel!r} is unavailable", CONTEXT_UNAVAILABLE
                )
            entries.append(skill_entry)
        try:
            bundle = ExecutorContextBundle(spec.id, tuple(entries))
            bundle.validate()
        except AdapterError:
            raise
        bundles[spec.id] = bundle
    return bundles


def _compiled_working_root(compiled_plan: object, task_id: str) -> str:
    """The nested working root the compiler routed ``task_id`` to, or ``"."`` if unknown."""
    try:
        return str(compiled_plan.task(task_id).working_root)  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - defensive: a plan without this task
        return "."


def _resolve_required_skill(
    skill_rel: str,
    *,
    project_dir: Path,
    agents_root: Path,
    agents_logical_prefix: str,
) -> RequiredInput | None:
    """Resolve one task-declared required-skill path to a validated :class:`RequiredInput`.

    The repository's logical ``.agents/`` prefix (``agents_logical_prefix``) is normalized to
    the physical external agents anchor, so a skill declared as
    ``.agents/skills/.../SKILL.md`` resolves against the real shared-agents tree instead of
    silently escaping the project anchor when ``.agents`` is a symlink out of the workspace
    (REC-08).

    Returns ``None`` only for an input this derivation does not grant a directory for: an
    absolute path (the runner-supplied context bundle carries it) or a bare anchor-root file
    with no directory component. Anything else that cannot be resolved to a readable file
    under the project or the agents anchor fails closed with :class:`AdapterError` rather than
    being dropped from the grant set.
    """
    skill_path = Path(skill_rel)
    if skill_path.is_absolute():
        return None
    posix = skill_path.as_posix().lstrip("/")
    prefix = agents_logical_prefix.strip("/") if agents_logical_prefix else ""
    if prefix and (posix == prefix or posix.startswith(f"{prefix}/")):
        agents_rel = posix[len(prefix):].strip("/")
        if "/" in agents_rel and (agents_root / agents_rel).is_file():
            return RequiredInput("skill", "agents", agents_rel)
        raise AdapterError(
            f"required skill {skill_rel!r} is declared under the {agents_logical_prefix!r} "
            f"agents prefix but names no readable file in the external agents anchor",
            REQUIRED_INPUT_INVALID,
        )
    if "/" not in posix:
        return None
    if (project_dir / posix).is_file():
        return RequiredInput("skill", "project", posix)
    if (agents_root / posix).is_file():
        return RequiredInput("skill", "agents", posix)
    raise AdapterError(
        f"required skill {skill_rel!r} could not be resolved to a readable file under the "
        f"project or the agents anchor",
        REQUIRED_INPUT_INVALID,
    )


def _project_required_input(
    kind: str,
    logical_source: str | None,
    *,
    project_dir: Path,
    reachable: Sequence[Path],
) -> RequiredInput | None:
    """A project-anchored :class:`RequiredInput` for a mandatory task / plan / prompt file, or
    ``None`` when no minimal directory grant is needed for it (REC-08).

    ``None`` is returned when the worker's own working root already reaches the file, or when
    the declared path is a bare anchor-root artifact such as ``plan.json`` that exists but
    names no sub-anchor directory to grant. Widening the grant to the whole project anchor is
    never acceptable, and such a mandatory file's content still reaches the worker through the
    runner-composed executor context bundle, so this is not a silent omission of required-input
    context — it keeps the pre-existing project-root execute flows green instead of aborting
    them. A path that *does* name a sub-anchor directory is returned as a
    :class:`RequiredInput`; a bare anchor-root path that cannot even be resolved to a readable
    file still fails closed with :class:`AdapterError`.
    """
    if not logical_source:
        return None
    posix = Path(logical_source).as_posix().lstrip("/")
    target = (project_dir / posix).resolve()
    grant = target.parent
    if any(grant == root or root in grant.parents for root in reachable):
        return None
    if "/" not in posix:
        if not target.is_file():
            raise AdapterError(
                f"required input {posix!r} names no directory to grant under the project "
                f"anchor and could not be resolved to a readable file",
                REQUIRED_INPUT_INVALID,
            )
        return None
    return RequiredInput(kind, "project", posix)


def build_required_input_dirs(
    specs: Sequence[TaskSpec],
    *,
    project_dir: Path,
    agents_root: Path,
    plan_path: Path,
    prompt_path: Path,
    working_root_by_id: Mapping[str, str] | None = None,
    task_ids: Sequence[str] | None = None,
    plan_specs: Sequence[TaskSpec] | None = None,
    agents_logical_prefix: str = ".agents",
) -> dict[str, tuple[str, ...]]:
    """Derive each selected task's minimal mandatory-input directory grants (REC-05 / REC-08).

    For every task this returns the smallest physical directory set a worker in that task's
    (possibly nested) working root needs in order to read its declared task file, the plan and
    prompt files, and each required skill — resolved under the project anchor, or the external
    agents anchor (including the repository's logical ``agents_logical_prefix``) for a skill
    that only exists there. The grants are always distinct from ``allowed_scope`` and never
    widen a write capability.

    Fail-closed: a declared mandatory input that cannot be validated, resolved, or reduced to a
    safe minimal directory raises :class:`AdapterError` rather than yielding a task with a
    silently truncated (or absent) grant set. A task whose working root already reaches
    everything legitimately gets no entry.
    """
    project_dir = Path(project_dir)
    agents_root = Path(agents_root)
    working_roots = dict(working_root_by_id or {})
    prerequisite_specs = plan_specs if plan_specs is not None else specs
    wanted = set(task_ids) if task_ids is not None else {spec.id for spec in specs}
    plan_rel = _project_logical_source(plan_path, project_dir)
    prompt_rel = _project_logical_source(prompt_path, project_dir)
    grants: dict[str, tuple[str, ...]] = {}
    for spec in specs:
        if spec.id not in wanted or not spec.path:
            continue
        # Everything under the task's own working root is already reachable; a worker routed
        # to the project root therefore needs no extra grant at all.
        reachable: tuple[Path, ...] = (
            (project_dir / working_roots.get(spec.id, ".")).resolve(),
        )
        inputs: list[RequiredInput] = []
        for kind, rel in (
            ("task", _project_logical_source(spec.path, project_dir)),
            ("plan", plan_rel),
            ("prompt", prompt_rel if prompt_rel != plan_rel else None),
        ):
            required = _project_required_input(
                kind, rel, project_dir=project_dir, reachable=reachable
            )
            if required is not None:
                inputs.append(required)
        standard_sources = {str(rel) for _kind, rel in (
            ("task", _project_logical_source(spec.path, project_dir)),
            ("plan", plan_rel),
            ("prompt", prompt_rel),
        ) if rel is not None}
        for source in _declared_context_sources(
            spec, prerequisite_specs, project_dir=project_dir,
            working_root=working_roots.get(spec.id, "."),
        ):
            if source not in standard_sources:
                required = _project_required_input(
                    "input", source, project_dir=project_dir, reachable=reachable
                )
                if required is not None:
                    inputs.append(required)
        for skill_rel in spec.required_skills:
            skill_input = _resolve_required_skill(
                skill_rel,
                project_dir=project_dir,
                agents_root=agents_root,
                agents_logical_prefix=agents_logical_prefix,
            )
            if skill_input is not None:
                inputs.append(skill_input)
        dirs = derive_required_input_dirs(
            inputs,
            project_root=project_dir,
            agents_root=agents_root,
            reachable_roots=reachable,
        )
        if dirs:
            grants[spec.id] = dirs
    return grants


def _scope_entry_directory(entry: str) -> tuple[str, ...] | None:
    """The safe path-segment prefix ``entry`` names, stripped of its leaf or wildcard part.

    Mirrors the wildcard handling :func:`scoped_add_dirs` already applies to an external
    scope root: a segment carrying ``*``/``?`` truncates the directory there, otherwise only
    the entry's own leaf (the file, or the final concrete segment) is dropped. Returns
    ``None`` for an entry with no directory component at all (a bare project-root file), since
    that never needs an ``--add-dir`` grant of its own.
    """
    normalized = str(entry).replace("\\", "/").strip("/")
    parts = tuple(part for part in normalized.split("/") if part)
    if len(parts) <= 1:
        return None
    wildcard = next(
        (index for index, part in enumerate(parts) if "*" in part or "?" in part), None
    )
    safe_parts = parts[:wildcard] if wildcard is not None else parts[:-1]
    return safe_parts or None


def build_scope_dirs(
    specs: Sequence[TaskSpec],
    *,
    project_dir: Path,
    working_root_by_id: Mapping[str, str] | None = None,
    task_ids: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Derive each selected task's minimal declared ``allowed_scope`` directories that sit
    outside its own (possibly nested) working root, but inside the project anchor (CSR-01).

    A task routed to a nested working root may legitimately declare an allowed-scope path
    that is not reachable from that root at all — for example, repository-root documentation
    alongside a nested Python route. This grants only the smallest directory needed to reach
    each such declared entry; it never grants a path the task did not declare, and it is kept
    entirely separate from :func:`build_required_input_dirs` so callers can apply the
    write-capability and read-only-verifier rules that only apply to declared scope.

    A task whose working root already reaches an entry contributes no grant for it. An entry
    that resolves outside the project anchor entirely is left to the existing external
    ``scope_roots`` mechanism and is not granted here.
    """
    project_dir = Path(project_dir).resolve()
    working_roots = dict(working_root_by_id or {})
    wanted = set(task_ids) if task_ids is not None else {spec.id for spec in specs}
    grants: dict[str, tuple[str, ...]] = {}
    for spec in specs:
        if spec.id not in wanted:
            continue
        working_root = (project_dir / working_roots.get(spec.id, ".")).resolve()
        kept: list[Path] = []
        for entry in spec.allowed_scope:
            safe_parts = _scope_entry_directory(entry)
            if not safe_parts:
                continue
            candidate = project_dir.joinpath(*safe_parts).resolve()
            try:
                candidate.relative_to(project_dir)
            except ValueError:
                continue
            if candidate == working_root or working_root in candidate.parents:
                continue
            if any(candidate == existing or existing in candidate.parents for existing in kept):
                continue
            kept = [
                existing for existing in kept
                if not (existing == candidate or candidate in existing.parents)
            ]
            kept.append(candidate)
        if kept:
            grants[spec.id] = tuple(str(directory) for directory in kept)
    return grants


def make_execute_adapters(
    project_dir: Path,
    agents_root: Path,
    core_root: Path,
):
    """Build production launch adapters from the same registry family as plan compilation."""
    return build_bootstrap(project_dir, agents_root, core_root).make_execute_adapters()


def redact_cli_error(message: str) -> str:
    """Render a CLI error without exposing host-specific paths or credentials."""
    return redact_text(message, build_rules())


def _registry_from_environment(environment: dict[str, bool]) -> AdapterRegistry:
    production = AdapterRegistry.default()
    return AdapterRegistry(
        tuple(
            AdapterCapabilities(
                adapter.name,
                bool(environment.get(adapter.name, False)),
                adapter.supports_resume,
                adapter.supports_read_only,
                adapter.supports_write,
                adapter.default_timeout_s,
            )
            for adapter in production.adapters
        )
    )


def run_execute(
    *,
    command: RunCommand,
    anchors: Anchors,
    agents_root: Path,
    project_dir: Path,
    profile,
    plan_path: Path,
    prompt_rel: str,
    result_factory: Callable[[str, int], PipelineResult],
    make_adapters: ExecuteAdapters = make_execute_adapters,
) -> PipelineResult:
    feature, specs = load_execute_specs(plan_path, command.feature)
    if command.recovery_source_feature and not command.resume:
        try:
            feature = replacement_feature(command.recovery_source_feature, command.adapter or "")
        except ExecutionError as exc:
            raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None
    if not feature or not FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    reason_by_type = route_reasons(profile, (spec.task_type for spec in specs))
    if reason_by_type:
        bad = ", ".join(f"{t} ({r})" for t, r in sorted(reason_by_type.items()))
        raise CliError(EXIT_ERROR, f"execute mode: unroutable task type(s): {bad}")

    try:
        factories = docker_factories_for_command(command)
    except AdapterError as exc:
        raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None
    composition = build_bootstrap(
        project_dir,
        agents_root,
        anchors.core_root,
        factories=factories,
        logical_paths={
            "agents": profile.logical_paths.agents,
            "core": profile.logical_paths.core,
        },
    )
    executor = None
    launchers = None
    environment = None
    recorded_run: Run | None = None
    if make_adapters is make_execute_adapters:
        adapter_registry = composition.adapter_registry
    else:
        executor, launchers, environment = make_adapters(
            project_dir, agents_root, anchors.core_root)
        adapter_registry = _registry_from_environment(environment)
    try:
        compiled_plan = compile_run_plan(
            feature=feature,
            definitions=[
                ShallowTaskInput(
                    s.id,
                    s.task_type,
                    tuple(s.depends_on),
                    s.executor,
                    tuple(s.allowed_scope),
                    tuple(s.out_of_scope),
                    s.max_repair_attempts,
                    s.preconditions,
                )
                for s in specs
            ],
            profile=compiled_profile_from_core(profile),
            overrides=ControlOverrides(
                max_repair_attempts=command.max_repair_attempts,
                routine_output_byte_budget=command.routine_output_byte_budget,
                diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
                adapter=command.adapter,
                model=command.model,
                effort=command.effort,
                verify_dependency_chain=(
                    True if command.verify_dependency_chain else None
                ),
            ),
            adapters=adapter_registry,
            task=command.task,
            through=command.through,
            allow_unavailable_adapter=True,
        )
    except DomainError as exc:
        raise CliError(EXIT_ERROR, f"{getattr(exc, 'code', 'plan-error')}: {exc}") from None

    storage_rel = str(compiled_plan.tasks[0].storage_root) if compiled_plan.tasks else None
    run_dir = (
        (project_dir / storage_rel) if storage_rel else project_dir / ".pipeline" / "runs"
    ) / feature

    # A resumed run owns its adapter, model/effort pair, and Docker runtime identity. The first
    # compilation only finds the storage root; then the durable controls replace omitted CLI
    # values before final composition and immutable-plan compilation.
    if command.resume:
        try:
            recorded_run = Run.load(run_dir, project_dir)
        except StateError:
            recorded_run = None
        if recorded_run is not None:
            changed = False
            recorded_adapter = (
                recorded_run.controls.get("adapter_requested", {}) or {}
            ).get("value")
            model = (recorded_run.controls.get("model", {}) or {}).get("value")
            effort = (recorded_run.controls.get("effort", {}) or {}).get("value")
            if command.adapter is None and recorded_adapter in {"claude", "codex"}:
                command = replace(command, adapter=recorded_adapter)
                changed = True
            if command.model is None and command.effort is None and (model is not None or effort is not None):
                if not isinstance(model, str) or not isinstance(effort, str):
                    raise CliError(
                        EXIT_ERROR,
                        "runtime-control-invalid: recorded model and effort controls "
                        "must be a complete string pair",
                    )
                command = replace(command, model=model, effort=effort)
                changed = True
            if command.codex_runtime is None and "codex_runtime" in recorded_run.controls:
                runtime = recorded_run.controls["codex_runtime"].get("value")
                image = recorded_run.controls.get("docker_codex_image", {}).get("value")
                proxy_image = recorded_run.controls.get("docker_proxy_image", {}).get("value")
                version = recorded_run.controls.get("docker_codex_version", {}).get("value")
                auth_file = recorded_run.controls.get("docker_codex_auth_file", {}).get("value")
                if runtime not in {"host", "docker"}:
                    raise CliError(EXIT_ERROR, "runtime-control-invalid: recorded Codex runtime is invalid")
                if runtime == "docker" and not all(
                    isinstance(value, str) and value
                    for value in (image, proxy_image, version, auth_file)
                ):
                    raise CliError(
                        EXIT_ERROR,
                        "runtime-control-invalid: recorded Docker runtime controls are incomplete",
                    )
                if runtime == "docker":
                    if auth_file == "<home>/.codex/auth.json":
                        auth_file = str(Path.home() / ".codex" / "auth.json")
                    else:
                        auth_path = Path(auth_file)
                        if auth_path.is_absolute() or any(
                            part in {"", ".", ".."} for part in auth_path.parts
                        ):
                            raise CliError(
                                EXIT_ERROR,
                                "runtime-control-invalid: recorded Docker auth-file identity is unsafe",
                            )
                        auth_file = str(project_dir / auth_path)
                command = replace(
                    command, codex_runtime=runtime, docker_codex_image=image,
                    docker_proxy_image=proxy_image, docker_codex_version=version,
                    docker_codex_auth_file=auth_file,
                )
                changed = True
            if changed:
                try:
                    factories = docker_factories_for_command(
                        command, run=recorded_run, task_ids=tuple(compiled_plan.selection),
                    )
                except AdapterError as exc:
                    raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None
                composition = build_bootstrap(
                    project_dir, agents_root, anchors.core_root, factories=factories,
                    logical_paths={"agents": profile.logical_paths.agents, "core": profile.logical_paths.core},
                )
                if make_adapters is make_execute_adapters:
                    adapter_registry = composition.adapter_registry
                try:
                    compiled_plan = compile_run_plan(
                        feature=feature,
                        definitions=[
                            ShallowTaskInput(
                                s.id, s.task_type, tuple(s.depends_on), s.executor,
                                tuple(s.allowed_scope), tuple(s.out_of_scope),
                                s.max_repair_attempts, s.preconditions,
                            )
                            for s in specs
                        ],
                        profile=compiled_profile_from_core(profile),
                        overrides=ControlOverrides(
                            max_repair_attempts=command.max_repair_attempts,
                            routine_output_byte_budget=command.routine_output_byte_budget,
                            diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
                            adapter=command.adapter,
                            model=command.model,
                            effort=command.effort,
                            verify_dependency_chain=(
                                True if command.verify_dependency_chain else None
                            ),
                        ),
                        adapters=adapter_registry,
                        task=command.task,
                        through=command.through,
                        allow_unavailable_adapter=True,
                    )
                except DomainError as exc:
                    raise CliError(
                        EXIT_ERROR, f"{getattr(exc, 'code', 'plan-error')}: {exc}"
                    ) from None

    try:
        execution_scope = tuple(compiled_plan.execution_scope)
        executor_contexts = build_executor_context_bundles(
            specs,
            project_dir=project_dir,
            agents_root=agents_root,
            plan_path=plan_path,
            prompt_path=project_dir / prompt_rel,
            task_ids=execution_scope,
            plan_specs=specs,
            working_root_by_id={
                spec.id: _compiled_working_root(compiled_plan, spec.id) for spec in specs
            },
        )
        required_input_dirs = build_required_input_dirs(
            specs,
            project_dir=project_dir,
            agents_root=agents_root,
            plan_path=plan_path,
            prompt_path=project_dir / prompt_rel,
            working_root_by_id={
                spec.id: _compiled_working_root(compiled_plan, spec.id) for spec in specs
            },
            task_ids=execution_scope,
            plan_specs=specs,
            agents_logical_prefix=profile.logical_paths.agents,
        )
        scope_dirs = build_scope_dirs(
            specs,
            project_dir=project_dir,
            working_root_by_id={
                spec.id: _compiled_working_root(compiled_plan, spec.id) for spec in specs
            },
            task_ids=execution_scope,
        )
    except AdapterError as exc:
        raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None
    composition = replace(
        composition,
        executor_contexts=executor_contexts,
        required_input_dirs=required_input_dirs,
        scope_dirs=scope_dirs,
    )

    if executor is None or launchers is None or environment is None:
        executor, launchers, environment = composition.make_execute_adapters(
            command.adapter, run=recorded_run,
        )
    controls = ExecuteControls(
        plan_approved=command.approve_plan,
        unattended=command.unattended,
        resume=command.resume,
        adapter=command.adapter,
        adapter_explicit=command.adapter is not None,
        model=command.model,
        effort=command.effort,
        codex_runtime=command.codex_runtime,
        docker_codex_image=command.docker_codex_image,
        docker_proxy_image=command.docker_proxy_image,
        docker_codex_version=command.docker_codex_version,
        docker_codex_auth_file=_docker_auth_file_control_value(
            command.docker_codex_auth_file, project_dir
        ) if command.codex_runtime == "docker" else None,
        max_repair_attempts=command.max_repair_attempts,
        routine_output_byte_budget=command.routine_output_byte_budget,
        diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
        task=command.task,
        through=command.through,
        attested_dependencies=parse_attestations(command.attest_dependency),
        verify_dependency_chain=command.verify_dependency_chain,
        grants=tuple(command.grants), approvals=tuple(command.approvals),
        published_refs=tuple(_published_refs(command.published_refs)),
        recovery_source_feature=command.recovery_source_feature,
        recovery_task=command.recovery_task,
        operational_unblock_task=command.operational_unblock_task,
        human_authorized_operational_unblock=command.human_authorized_operational_unblock,
        uv_cache_dir=command.uv_cache_dir,
    )
    request = ExecuteRequest(
        feature=feature,
        repo_root=project_dir,
        run_dir=run_dir,
        prompt_path=project_dir / prompt_rel,
        plan_path=plan_path,
        specs=tuple(specs),
        adapter=executor,
        launchers=launchers,
        envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
        verifier_anchors=VerifierAnchors(
            project_root=str(project_dir), agents_root=str(agents_root)
        ),
        environment=environment,
        controls=controls,
        plan_prompt_path=project_relative(plan_path, project_dir),
        compiled_plan=compiled_plan,
        board_path=(
            project_dir / BOARD_RELATIVE_PATH if plan_path.suffix.lower() == ".md" else None
        ),
        core_root=anchors.core_root,
    )
    result = execute_run(request)
    if result.status == "error":
        raise CliError(result.exit_code, result.message)
    return result_factory(
        redact_text(result.message.rstrip("\n") + "\n", build_rules()), result.exit_code
    )


def _published_refs(raw: Sequence[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in raw:
        if "=" not in item:
            raise CliError(EXIT_ERROR, f"--published-ref must be SOURCE=REF: '{item}'")
        source, ref = item.split("=", 1)
        if source not in {"parent-head", "core-gitlink"} or not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/")):
            raise CliError(EXIT_ERROR, f"invalid --published-ref '{item}'")
        if source in dict(result):
            raise CliError(EXIT_ERROR, f"duplicate --published-ref source '{source}'")
        result.append((source, ref))
    return result


__all__ = [
    "load_execute_specs",
    "AdapterFactory",
    "AdapterRuntime",
    "BOARD_RELATIVE_PATH",
    "BootstrapComposition",
    "build_bootstrap",
    "build_executor_context_bundles",
    "build_required_input_dirs",
    "build_scope_dirs",
    "codex_factory",
    "docker_codex_factory",
    "docker_codex_factories",
    "docker_factories_for_command",
    "load_markdown_plan",
    "MarkdownPlanError",
    "load_runnable_profile",
    "load_tool_integration",
    "load_release_policy",
    "ReleasePolicy",
    "POST_TASK_STAGES",
    "plan_release_dry_run",
    "Run",
    "StateError",
    "read_lease",
    "pid_alive",
    "build_rules",
    "redact_text",
    "Anchors",
    "make_execute_adapters",
    "parse_attestations",
    "project_relative",
    "resolve_add_dirs",
    "resolve_scope_roots",
    "resolve_attested_dependency_ids",
    "run_execute",
]
