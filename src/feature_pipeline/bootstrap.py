"""Production composition root for the feature-pipeline CLI."""

from __future__ import annotations

import json
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
)
from feature_pipeline.contracts import SchemaError, TaskSpec

from pipeline_core.adapters import (
    REQUIRED_INPUT_INVALID,
    Adapter,
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    ContextEntry,
    ExecutorContextBundle,
    RequiredInput,
    derive_required_input_dirs,
)
from pipeline_core.execution import (
    ExecuteControls,
    ExecuteRequest,
    ExecutionError,
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
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers

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

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            self.name,
            self.available(),
            self.supports_resume,
            self.supports_read_only,
            self.supports_write,
            self.default_timeout_s,
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

    def make_execute_adapters(
        self, adapter_name: str | None = None
    ) -> tuple[Adapter, VerifierLaunchers, dict[str, bool]]:
        registry = self.adapter_registry
        resolved = registry.select(adapter_name)
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
        )
        factory = next(factory for factory in self.factories if factory.name == resolved.name)
        executor = factory.create(runtime)
        launchers = VerifierLaunchers(task=executor, test=executor)
        environment = {cap.name: cap.available for cap in registry.adapters}
        environment.setdefault(CODEX, False)
        return executor, launchers, environment


def codex_factory(
    *,
    resolver: Callable[[], str | Sequence[str] | None] | None = None,
) -> AdapterFactory:
    """Declare the Codex runtime and construct it with the run's resolved anchors."""
    def create_codex(runtime: AdapterRuntime) -> CodexAdapter:
        return CodexAdapter(
            resolver=resolver,
            working_root=runtime.project_dir,
            scope_roots=runtime.scope_roots,
            required_input_dirs=runtime.required_input_dirs,
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
    )


def _production_factories() -> tuple[AdapterFactory, ...]:
    def create_claude(runtime: AdapterRuntime) -> ClaudeAdapter:
        return ClaudeAdapter(
            working_root=str(runtime.project_dir),
            scope_roots=runtime.scope_roots,
            executor_contexts=runtime.executor_contexts,
            required_input_dirs=runtime.required_input_dirs,
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
        ),
        codex_factory(),
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
) -> BootstrapComposition:
    registered_factories = tuple(factories) if factories is not None else _production_factories()
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
    kind: str, base: Path, rel: str, rules
) -> ContextEntry | None:
    """Read ``base/rel``, redact known host roots, and return a digest-bound entry.

    Returns ``None`` when the file cannot be read or the declared path is not a safe logical
    source — the caller keeps the rest of the bundle rather than failing the run.
    """
    rel_posix = Path(rel).as_posix().lstrip("/")
    try:
        raw = (base / rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        return ContextEntry.of(kind, rel_posix, redact_text(raw, rules))
    except AdapterError:
        return None


def build_executor_context_bundles(
    specs: Sequence[TaskSpec],
    *,
    project_dir: Path,
    agents_root: Path,
    plan_path: Path,
    prompt_path: Path,
    task_ids: Sequence[str] | None = None,
) -> dict[str, ExecutorContextBundle]:
    """Assemble one runner-owned immutable context bundle per selected task.

    Each bundle carries the task's canonical contract plus the plan, prompt, and required-skill
    content it needs — every entry redacted of known host roots, then bound to a safe
    project/agents-root logical source and a SHA-256 digest. Building is best-effort: a file the
    runner cannot read here is omitted, and a task whose own contract file is unreadable gets
    no bundle (the adapter then launches with the plain envelope, exactly as before). Every
    entry that *is* built is validated and fails closed on a bad digest or unsafe source.
    """
    rules = build_rules(project_dir)
    wanted = set(task_ids) if task_ids is not None else {spec.id for spec in specs}
    plan_rel = _project_logical_source(plan_path, project_dir)
    plan_entry = _redacted_logical_entry("plan", project_dir, plan_rel, rules)
    prompt_rel = _project_logical_source(prompt_path, project_dir)
    prompt_entry = _redacted_logical_entry("prompt", project_dir, prompt_rel, rules)
    bundles: dict[str, ExecutorContextBundle] = {}
    for spec in specs:
        if spec.id not in wanted or not spec.path:
            continue
        task_rel = _project_logical_source(spec.path, project_dir)
        task_entry = _redacted_logical_entry("task", project_dir, task_rel, rules)
        if task_entry is None:
            continue
        entries: list[ContextEntry] = [task_entry]
        if plan_entry is not None:
            entries.append(plan_entry)
        if prompt_entry is not None:
            entries.append(prompt_entry)
        for skill_rel in spec.required_skills:
            skill_entry = _redacted_logical_entry("skill", project_dir, skill_rel, rules)
            if skill_entry is None and not Path(skill_rel).is_absolute():
                skill_entry = _redacted_logical_entry("skill", agents_root, skill_rel, rules)
            if skill_entry is not None:
                entries.append(skill_entry)
        try:
            bundle = ExecutorContextBundle(spec.id, tuple(entries))
            bundle.validate()
        except AdapterError:
            continue
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

    composition = build_bootstrap(
        project_dir,
        agents_root,
        anchors.core_root,
        logical_paths={
            "agents": profile.logical_paths.agents,
            "core": profile.logical_paths.core,
        },
    )
    executor = None
    launchers = None
    environment = None
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

    try:
        execution_scope = tuple(compiled_plan.execution_scope)
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
            agents_logical_prefix=profile.logical_paths.agents,
        )
    except AdapterError as exc:
        raise CliError(EXIT_ERROR, f"{exc.code}: {exc}") from None
    composition = replace(
        composition,
        executor_contexts=build_executor_context_bundles(
            specs,
            project_dir=project_dir,
            agents_root=agents_root,
            plan_path=plan_path,
            prompt_path=project_dir / prompt_rel,
            task_ids=execution_scope,
        ),
        required_input_dirs=required_input_dirs,
    )

    if executor is None or launchers is None or environment is None:
        executor, launchers, environment = composition.make_execute_adapters(command.adapter)
    controls = ExecuteControls(
        plan_approved=command.approve_plan,
        unattended=command.unattended,
        resume=command.resume,
        adapter=command.adapter,
        adapter_explicit=command.adapter is not None,
        model=command.model,
        effort=command.effort,
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
    "codex_factory",
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
