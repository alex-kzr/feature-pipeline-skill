"""UGA-04 — topology and drift contract validation for GitHub Actions workflows.

The UGA-01 regression proved a single failure shape: a workflow whose ``working-directory``
assumes one specific checkout layout breaks silently in the other (a nested submodule versus a
standalone checkout). This module generalizes that one hand-written regression test into a
reusable validator that:

* rejects a ``working-directory`` value that is absolute, contains a ``..`` traversal segment,
  is home-prefixed (``~``), uses a backslash separator, or is derived from a known repository
  name (Requirements bullet 2);
* resolves every literal checkout/source path against the actual filesystem below an explicit
  source root — the same check the UGA-01 regression made by hand — so a workflow that only
  happens to work because a checkout folder was named after the repository fails the same way
  under an arbitrarily renamed checkout (bullet 3, ``tests/fixtures/ci/standalone`` and
  ``tests/fixtures/ci/nested``);
* rejects a workflow step that names a gate ID absent from ``ci/gates.toml``, and a step that
  runs a declared gate's command directly instead of through ``ci/run.py run GATE_ID`` (bullet
  4 — "direct workflow commands that bypass the driver");
* compares each workflow's ``strategy.matrix.suite`` list, the manifest's own multi-OS "core"
  gates, and ``tests/README.md``'s documented suite headings, reporting drift in any direction
  (bullet 5);
* requires ``--expected-source-sha`` on every driver invocation this module recognizes (bullet
  6 — the umbrella gitlink SHA binding UGA-03 added to ``ci/run.py run``).

Every rule above raises or reports independently (Implementation Notes: "make one rule fail at
a time"), so a workflow with several unrelated problems reports all of them at once instead of
stopping at the first.

**Dependency boundary.** Parsing real workflow YAML needs a maintained parser (PyYAML), added
to ``[project.optional-dependencies].dev`` — never to ``[project].dependencies`` — so the
installable runtime stays standard-library only (docs/adr/003; AC-5). PyYAML is therefore not
guaranteed to be installed: this module never imports it at module scope, only inside
:func:`_yaml_module`, and callers that cannot tolerate a missing parser get a deterministic
:class:`YamlUnavailable` naming the install command. This module (like the rest of ``ci/``) is
CI tooling; the installable runtime never imports it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ci import contract

#: Real GitHub repository names this project ships under. A ``working-directory`` value that
#: equals one of these (or is nested under one) only "works" because a checkout folder happened
#: to be named after the repository — the exact UGA-01 bug shape.
DEFAULT_REPOSITORY_NAMES: tuple[str, ...] = ("feature-pipeline-skill", "feature-pipeline")

#: Filesystem entries every gate's source root must carry (mirrors ``ci/runner.py``'s own
#: ``pyproject.toml`` / ``tests`` expectations plus the driver's manifest path).
_LAYOUT_MARKERS: tuple[str, ...] = ("pyproject.toml", "tests")

_DRIVE_LETTER_RE = re.compile(r"\A[A-Za-z]:[\\/]")

#: Recognizes a step that invokes the UGA-03 driver's ``run`` subcommand and captures the gate
#: ID token that follows it, e.g. ``uv run python -m ci.run run lint --source-root .`` or
#: ``python ci/run.py run coverage --source-root .``.
_DRIVER_INVOCATION_RE = re.compile(
    r"\bci(?:[\\/]run\.py|\.run)\b.*?\brun\s+(?P<gate>[A-Za-z0-9_-]+)"
)

#: A ``### <name>`` heading under ``tests/README.md``'s "## Suites" section.
_SUITE_HEADING_RE = re.compile(r"^### (\S+)", re.MULTILINE)


class YamlUnavailable(RuntimeError):
    """The dev-only YAML parser is not installed in this environment.

    Raised instead of letting ``ImportError`` propagate, so every caller sees the same
    deterministic remediation instead of a bare traceback.
    """


def _yaml_module():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised only without the dev extra
        raise YamlUnavailable(
            "workflow topology validation needs the dev-only YAML parser: install it with "
            "`uv sync --extra dev` or run this one command with "
            "`uv run --with pyyaml ...`"
        ) from exc
    return yaml


@dataclass(frozen=True)
class Violation:
    """One independent drift/topology finding, always naming where it was found."""

    rule: str
    workflow: str
    job: str | None
    step: str | None
    detail: str

    def render(self) -> str:
        location = self.workflow
        if self.job is not None:
            location += f":{self.job}"
        if self.step is not None:
            location += f":{self.step}"
        return f"[{self.rule}] {location}: {self.detail}"


class WorkflowValidationError(ValueError):
    """Every violation found in one :func:`validate_topology` call, reported together."""

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(v.render() for v in self.violations))


def iter_workflow_files(workflow_dir: Path) -> list[Path]:
    """Every ``*.yml``/``*.yaml`` file directly under ``workflow_dir``, sorted for determinism."""

    if not workflow_dir.is_dir():
        return []
    return sorted(
        p for p in workflow_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def unsafe_working_directory_reason(
    value: str | None, *, repository_names: Iterable[str] = DEFAULT_REPOSITORY_NAMES
) -> str | None:
    """The rejection reason for an unsafe ``working-directory`` value, or ``None`` if it is safe.

    Checked in this order so the first applicable reason is reported (Requirements bullet 2):
    absolute, home-prefixed, backslash-separated, path-traversal, repository-name-derived.
    """

    if not value:
        return None
    if value.startswith("/") or _DRIVE_LETTER_RE.match(value):
        return "absolute path"
    if value.startswith("~"):
        return "home-prefixed path"
    if "\\" in value:
        return "backslash path separator"
    segments = re.split(r"[\\/]", value)
    if ".." in segments:
        return "path traversal"
    for name in repository_names:
        if value == name or value.startswith(f"{name}/"):
            return f"repository-name-derived path (matches {name!r})"
    return None


def resolve_checkout_root(source_root: Path, working_directory: str | None) -> Path:
    """The directory a job actually runs its commands from below ``source_root``."""

    return source_root if not working_directory else source_root / working_directory


def checkout_layout_problem(
    source_root: Path,
    working_directory: str | None,
    *,
    markers: Sequence[str] = _LAYOUT_MARKERS,
) -> str | None:
    """The layout diagnostic for a ``working-directory``, or ``None`` if it resolves cleanly.

    This is the generalized UGA-01 regression check (bullet 3): a checkout-derived path is only
    valid if it actually exists — and carries the source markers — below the real source root,
    proven independently against synthetic standalone and nested layouts whose directory names
    differ from any real repository name.
    """

    resolved = resolve_checkout_root(source_root, working_directory)
    if not resolved.is_dir():
        return f"nonexistent root: {resolved}"
    missing = [name for name in markers if not (resolved / name).exists()]
    if missing:
        return f"missing {', '.join(missing)} under {resolved}"
    return None


def _documented_suite_headings(readme_text: str) -> set[str]:
    return set(_SUITE_HEADING_RE.findall(readme_text))


def _core_matrix_gate_ids(loaded: contract.Contract) -> set[str]:
    """Manifest "core" gates that run across more than one OS — the ones a policy-suites style
    job matrixes over (mirrors the committed ``platform``/``fault-injection``/``performance``
    trio without naming them, so a future manifest change is picked up automatically)."""

    return {gate.id for gate in loaded.group("core") if len(gate.os) > 1}


def _matrix_and_documentation_drift(
    workflow_name: str,
    workflow_suites: set[str],
    manifest_suites: set[str],
    documented_suites: set[str],
) -> list[Violation]:
    violations: list[Violation] = []
    missing = manifest_suites - workflow_suites
    extra = workflow_suites - manifest_suites
    undocumented = manifest_suites - documented_suites
    if missing:
        violations.append(
            Violation(
                "matrix-drift",
                workflow_name,
                None,
                None,
                f"manifest gate(s) missing from the workflow matrix: {sorted(missing)}",
            )
        )
    if extra:
        violations.append(
            Violation(
                "matrix-drift",
                workflow_name,
                None,
                None,
                f"workflow matrix names gate(s) absent from the manifest: {sorted(extra)}",
            )
        )
    if undocumented:
        violations.append(
            Violation(
                "documentation-drift",
                workflow_name,
                None,
                None,
                f"tests/README.md is missing suite heading(s): {sorted(undocumented)}",
            )
        )
    return violations


def _gate_command_strings(loaded: contract.Contract) -> Mapping[str, tuple[str, ...]]:
    return {
        gate_id: tuple(" ".join(command.argv) for command in gate.commands)
        for gate_id, gate in loaded.gates.items()
    }


def _step_violations(
    *,
    workflow_name: str,
    job_name: str,
    step: Mapping[str, object],
    source_root: Path,
    repository_names: Iterable[str],
    gate_commands: Mapping[str, tuple[str, ...]],
) -> list[Violation]:
    violations: list[Violation] = []
    step_name = str(step.get("name") or "")
    step_working_directory = step.get("working-directory")
    if isinstance(step_working_directory, str):
        reason = unsafe_working_directory_reason(
            step_working_directory, repository_names=repository_names
        )
        if reason is not None:
            violations.append(
                Violation(
                    "unsafe-working-directory",
                    workflow_name,
                    job_name,
                    step_name or None,
                    f"{reason}: {step_working_directory!r}",
                )
            )

    run_text = step.get("run")
    if not isinstance(run_text, str) or not run_text.strip():
        return violations
    if not step_name:
        step_name = run_text.strip().splitlines()[0][:60]

    match = _DRIVER_INVOCATION_RE.search(run_text)
    if match is not None:
        gate_id = match.group("gate")
        if gate_id not in gate_commands:
            violations.append(
                Violation(
                    "unknown-gate",
                    workflow_name,
                    job_name,
                    step_name,
                    f"gate id {gate_id!r} is not declared in ci/gates.toml",
                )
            )
        elif "--expected-source-sha" not in run_text:
            violations.append(
                Violation(
                    "missing-expected-sha",
                    workflow_name,
                    job_name,
                    step_name,
                    f"gate {gate_id!r} invocation does not pass --expected-source-sha",
                )
            )
        return violations

    for gate_id, commands in gate_commands.items():
        for command in commands:
            if command and command in run_text:
                violations.append(
                    Violation(
                        "bypasses-driver",
                        workflow_name,
                        job_name,
                        step_name,
                        f"runs gate {gate_id!r} command directly instead of "
                        f"`ci/run.py run {gate_id}`",
                    )
                )
    return violations


def validate_topology(
    source_root: Path,
    *,
    repository_names: Iterable[str] = DEFAULT_REPOSITORY_NAMES,
    workflow_relpath: str = ".github/workflows",
) -> list[Violation]:
    """Every topology/drift violation found below ``source_root``, independent of each other.

    Returns an empty list for a source root with no ``.github/workflows`` directory (nothing to
    validate — the driver tests' fixtures never need a workflow tree) without importing PyYAML
    at all. Raises :class:`YamlUnavailable` only once an actual workflow file needs parsing.
    """

    workflow_dir = source_root / workflow_relpath
    files = iter_workflow_files(workflow_dir)
    if not files:
        return []

    yaml = _yaml_module()
    manifest_path = source_root / "ci" / "gates.toml"
    loaded = contract.load(manifest_path) if manifest_path.is_file() else contract.load()
    gate_commands = _gate_command_strings(loaded)
    manifest_suites = _core_matrix_gate_ids(loaded)
    readme = source_root / "tests" / "README.md"
    documented_suites = (
        _documented_suite_headings(readme.read_text(encoding="utf-8")) if readme.is_file() else set()
    )

    violations: list[Violation] = []
    for path in files:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            violations.append(Violation("unparseable-yaml", path.name, None, None, str(exc)))
            continue

        jobs = document.get("jobs")
        if not isinstance(jobs, Mapping):
            jobs = {}
        workflow_suites: set[str] = set()
        for job_name, job in jobs.items():
            # A ``job`` value that is not a mapping (``null``, or a bare ``${{ ... }}``
            # expression) carries no working-directory, matrix, or steps to inspect.
            if not isinstance(job, Mapping):
                continue
            job_working_directory = ((job.get("defaults") or {}).get("run") or {}).get(
                "working-directory"
            )
            reason = unsafe_working_directory_reason(
                job_working_directory, repository_names=repository_names
            )
            if reason is not None:
                violations.append(
                    Violation(
                        "unsafe-working-directory",
                        path.name,
                        job_name,
                        None,
                        f"{reason}: {job_working_directory!r}",
                    )
                )
            # Independent of the value-shape check above (bullet 2): even a value that *looks*
            # safe must still resolve on disk, and a repository-name-derived value that also
            # fails to resolve (the exact UGA-01 bug shape, AC-1) reports both findings rather
            # than the layout check being short-circuited by the shape rejection.
            problem = checkout_layout_problem(source_root, job_working_directory)
            if problem is not None:
                violations.append(
                    Violation("checkout-path-drift", path.name, job_name, None, problem)
                )

            # ``strategy``/``matrix`` may each be a run-time-resolved ``${{ ... }}``
            # expression string rather than a literal mapping (``quality-gates.yml`` sets
            # ``matrix: ${{ fromJSON(needs.prepare-core-gates.outputs.matrix) }}``). Only a
            # literal ``matrix.suite`` list can be compared against the manifest and README;
            # anything else (an expression, ``matrix.include``, an absent matrix) simply
            # contributes no suites for this job instead of raising.
            strategy = job.get("strategy")
            matrix = strategy.get("matrix") if isinstance(strategy, Mapping) else None
            if isinstance(matrix, Mapping):
                suite_values = matrix.get("suite")
                if isinstance(suite_values, Sequence) and not isinstance(
                    suite_values, (str, bytes)
                ):
                    workflow_suites.update(str(value) for value in suite_values)

            steps = job.get("steps")
            if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
                steps = ()
            for step in steps:
                if not isinstance(step, Mapping):
                    continue
                violations.extend(
                    _step_violations(
                        workflow_name=path.name,
                        job_name=job_name,
                        step=step,
                        source_root=source_root,
                        repository_names=repository_names,
                        gate_commands=gate_commands,
                    )
                )

        if workflow_suites:
            violations.extend(
                _matrix_and_documentation_drift(
                    path.name, workflow_suites, manifest_suites, documented_suites
                )
            )

    return violations


def check(source_root: Path, **kwargs: object) -> None:
    """Raise :class:`WorkflowValidationError` with every violation, or return cleanly."""

    violations = validate_topology(source_root, **kwargs)  # type: ignore[arg-type]
    if violations:
        raise WorkflowValidationError(violations)
