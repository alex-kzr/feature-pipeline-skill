"""Production composition root for the feature-pipeline CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Sequence

from feature_pipeline.application.compile_plan import (
    ControlOverrides,
    ShallowTaskInput,
    compile_run_plan,
)
from feature_pipeline.application.profile_bridge import compiled_profile_from_core, route_reasons
from feature_pipeline.application.results import PipelineResult
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
from schemas import SchemaError
from schemas.contracts import TaskSpec

from pipeline_core.adapters import Adapter, ClaudeAdapter, CodexAdapter
from pipeline_core.execution import (
    ExecuteControls,
    ExecuteRequest,
    ExecutionError,
    _resolve_attestation,
    _validate_attestation_scope,
    execute_run,
)
from pipeline_core.integrations import load_tool_integration
from pipeline_core.plan_md import MarkdownPlanError, load_markdown_plan
from pipeline_core.post_task import POST_TASK_STAGES
from pipeline_core.profiles import Anchors
from pipeline_core.project_profile import load_runnable_profile
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.redaction import build_rules, redact_text
from pipeline_core.release import ReleasePolicy, load_release_policy
from pipeline_core.stages import plan_release_dry_run
from pipeline_core.state import Run, StateError, pid_alive, read_lease
from pipeline_core.task_files import load_task_spec
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers

ExecuteAdapters = Callable[[Path, Path, Path], tuple[object, VerifierLaunchers, dict[str, bool]]]


@dataclass(frozen=True)
class AdapterRuntime:
    """Resolved runtime roots a concrete adapter factory needs."""

    project_dir: Path
    agents_root: Path
    core_root: Path
    add_dirs: tuple[Path, ...]


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
    factories: tuple[AdapterFactory, ...]
    adapter_registry: AdapterRegistry

    def make_execute_adapters(
        self, adapter_name: str | None = None
    ) -> tuple[Adapter, VerifierLaunchers, dict[str, bool]]:
        registry = self.adapter_registry
        resolved = registry.select(adapter_name)
        runtime = AdapterRuntime(
            project_dir=self.project_dir,
            agents_root=self.agents_root,
            core_root=self.core_root,
            add_dirs=resolve_add_dirs(self.project_dir, self.agents_root, self.core_root),
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
            add_dirs=runtime.add_dirs,
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
            add_dirs=runtime.add_dirs,
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
) -> BootstrapComposition:
    registered_factories = tuple(factories) if factories is not None else _production_factories()
    return BootstrapComposition(
        Path(project_dir),
        Path(agents_root),
        Path(core_root),
        registered_factories,
        AdapterRegistry(tuple(factory.capabilities() for factory in registered_factories)),
    )


def project_relative(path: Path, project_dir: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return None


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
    run_dir: Path,
    repo_root: Path,
    prompt_path: Path,
    plan_path: Path,
) -> set[str]:
    attestations = parse_attestations(raw_attestations)
    if not attestations:
        return set()
    by_id = {t["id"]: SimpleNamespace(depends_on=t["depends_on"]) for t in plan_tasks}
    controls = ExecuteControls(task=selected_task, attested_dependencies=attestations)
    try:
        _validate_attestation_scope(controls, by_id)
        attested_ids = set()
        for dep_id, source_feature in attestations:
            _resolve_attestation(
                dep_id=dep_id,
                source_feature=source_feature,
                run_dir=run_dir,
                repo_root=repo_root,
                prompt_path=prompt_path,
                plan_path=plan_path,
            )
            attested_ids.add(dep_id)
        return attested_ids
    except ExecutionError as exc:
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
    if not feature or not FEATURE_RE.match(feature):
        raise CliError(EXIT_ERROR, "feature name must be a single [A-Za-z0-9._-] token")

    reason_by_type = route_reasons(profile, (spec.task_type for spec in specs))
    if reason_by_type:
        bad = ", ".join(f"{t} ({r})" for t, r in sorted(reason_by_type.items()))
        raise CliError(EXIT_ERROR, f"execute mode: unroutable task type(s): {bad}")

    composition = build_bootstrap(project_dir, agents_root, anchors.core_root)
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
                )
                for s in specs
            ],
            profile=compiled_profile_from_core(profile),
            overrides=ControlOverrides(
                max_repair_attempts=command.max_repair_attempts,
                routine_output_byte_budget=command.routine_output_byte_budget,
                diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
                adapter=command.adapter,
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

    if executor is None or launchers is None or environment is None:
        executor, launchers, environment = composition.make_execute_adapters(command.adapter)
    controls = ExecuteControls(
        plan_approved=command.approve_plan,
        unattended=command.unattended,
        resume=command.resume,
        adapter=command.adapter,
        adapter_explicit=command.adapter is not None,
        max_repair_attempts=command.max_repair_attempts,
        routine_output_byte_budget=command.routine_output_byte_budget,
        diagnostic_output_byte_budget=command.diagnostic_output_byte_budget,
        task=command.task,
        through=command.through,
        attested_dependencies=parse_attestations(command.attest_dependency),
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
    )
    result = execute_run(request)
    if result.status == "error":
        raise CliError(result.exit_code, result.message)
    return result_factory(
        redact_text(result.message.rstrip("\n") + "\n", build_rules()), result.exit_code
    )


__all__ = [
    "load_execute_specs",
    "AdapterFactory",
    "AdapterRuntime",
    "BootstrapComposition",
    "build_bootstrap",
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
    "resolve_attested_dependency_ids",
    "run_execute",
]
