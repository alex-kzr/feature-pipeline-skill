"""The feature-pipeline domain vocabulary.

One small, standard-library-only layer of validated value objects and closed enumerations,
introduced by **DF-01** (``docs/plans/tasks/DF-01_typed-vocabulary-paths-errors.md``,
``docs/adr/002``). It replaces repeated string literals and fragmented ad-hoc validation
scattered across ``pipeline_core``.

The domain knows nothing about processes, the filesystem, or CLI exit codes. Every rejection
is a :class:`~feature_pipeline.domain.errors.DomainError`; the single mapping from a domain
outcome to a stable CLI exit code lives in
:mod:`feature_pipeline.application.results`.

Nothing here is wired into a production call site yet — DF-02 migrates the first complete
slice once parity tests prove the new and old values are identical.
"""

from __future__ import annotations

from .argv import SHELL_TOKENS, Argv
from .controls import ControlSource, ResolvedControl
from .errors import (
    DomainError,
    InvalidArgv,
    InvalidControl,
    InvalidGlob,
    InvalidIdentifier,
    InvalidPath,
    InvalidTerm,
    PathNotContained,
)
from .identifiers import (
    FEATURE_ID_PATTERN,
    TASK_ID_PATTERN,
    FeatureId,
    TaskId,
)
from .paths import RelativeGlob, RelativePath, ensure_within
from .plan import (
    CompiledRunPlan,
    ConflictingStorage,
    PlanCompilationError,
    ResolvedCheck,
    ResolvedTask,
    UnsupportedControl,
)
from .repair import DEFAULT_MAX_REPAIR_ATTEMPTS, RepairBound
from .vocabulary import (
    Actor,
    AdapterName,
    Disposition,
    RunStatus,
    StageId,
    Subagent,
    TaskStatus,
    Verdict,
)

__all__ = [
    # errors
    "DomainError",
    "InvalidArgv",
    "InvalidControl",
    "InvalidGlob",
    "InvalidIdentifier",
    "InvalidPath",
    "InvalidTerm",
    "PathNotContained",
    # identifiers
    "TaskId",
    "FeatureId",
    "TASK_ID_PATTERN",
    "FEATURE_ID_PATTERN",
    # paths + argv
    "RelativePath",
    "RelativeGlob",
    "ensure_within",
    "Argv",
    "SHELL_TOKENS",
    # compiled execution plan (CP-01)
    "CompiledRunPlan",
    "ResolvedTask",
    "ResolvedCheck",
    "PlanCompilationError",
    "ConflictingStorage",
    "UnsupportedControl",
    # controls
    "ControlSource",
    "ResolvedControl",
    "RepairBound",
    "DEFAULT_MAX_REPAIR_ATTEMPTS",
    # vocabulary
    "TaskStatus",
    "RunStatus",
    "Verdict",
    "Disposition",
    "Actor",
    "Subagent",
    "AdapterName",
    "StageId",
]
