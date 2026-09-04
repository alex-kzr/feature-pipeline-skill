"""Application layer for the portable feature-pipeline core.

Use-case sequencing and the mapping from typed outcomes to stable CLI exit codes live here.
DF-01 introduced :mod:`feature_pipeline.application.results`; DF-02 adds
:mod:`feature_pipeline.application.controls` (boundary control resolution) and
:mod:`feature_pipeline.application.identity` (the first clock-injected slice); IN-02 adds
:mod:`feature_pipeline.application.selection` (the single task-selection / dependency-gate
entry point over :class:`feature_pipeline.domain.graph.TaskGraph`); WT-02 adds
:mod:`feature_pipeline.application.execute_task` (the deterministic allowed-scope gate that
runs between attribution and verification). Later phases add the plan compiler, run service,
and pipeline engine (``docs/adr/002`` §5.4).
"""

from __future__ import annotations

from .controls import ResolvedExecuteControls, resolve_execute_controls
from .diagnostic_service import (
    DIAGNOSTIC_COLLECTION_FAILED,
    DiagnosticService,
    GitRepositoryInspection,
)
from .execute_task import (
    ATTRIBUTION_UNAVAILABLE,
    SCOPE_CLEAN,
    SCOPE_VIOLATION,
    ScopeEvidence,
    ScopeGateDecision,
    enforce_scope_gate,
)
from .identity import RunIdentity, RunIdentityFactory
from .launch_controls import (
    InvalidControl,
    LaunchControls,
    ResolvedLaunch,
    UnsupportedControl,
    launch_scope,
    plan_launch,
    resolve_launch_controls,
)
from .results import Outcome, PipelineResult, ResultMapper
from .task_engine import (
    ExecutionError,
    RepairPass,
    TaskEngine,
    TaskExecution,
    TaskRunResult,
)
from .verification_service import VerificationRequest, VerificationService
from .run_repository import (
    RunPersistence,
    initialise_run,
    resume_existing_run,
)
from .selection import (
    DependencyStatus,
    ResolvedSelection,
    SelectionError,
    dependency_status,
    resolve_selection,
)

__all__ = [
    "Outcome",
    "PipelineResult",
    "ResultMapper",
    "ResolvedExecuteControls",
    "resolve_execute_controls",
    "SCOPE_CLEAN",
    "SCOPE_VIOLATION",
    "ATTRIBUTION_UNAVAILABLE",
    "ScopeEvidence",
    "ScopeGateDecision",
    "enforce_scope_gate",
    "RunIdentity",
    "RunIdentityFactory",
    "InvalidControl",
    "LaunchControls",
    "ResolvedLaunch",
    "UnsupportedControl",
    "launch_scope",
    "plan_launch",
    "resolve_launch_controls",
    "RunPersistence",
    "initialise_run",
    "resume_existing_run",
    "DependencyStatus",
    "ResolvedSelection",
    "SelectionError",
    "dependency_status",
    "resolve_selection",
]
