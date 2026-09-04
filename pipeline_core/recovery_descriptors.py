"""Generic, data-only recovery descriptor loading.

**Deferred (PE-02):** no accepted pipeline stage performs a recovery — SKILL.md §3 stops at
``push`` (stage 17). This module is a primitive kept ready for a future, separately approved
recovery stage, not connected to any production caller today
(``tests/test_safety_ports.py::DeferredPrimitiveTests`` proves the absence).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class DescriptorError(ValueError):
    """A recovery descriptor that fails portable validation."""


@dataclass(frozen=True)
class RecoveryDescriptor:
    """One project-supplied recovery authorization with no core registry."""

    id: str
    feature: str
    task_id: str
    expected_state: Mapping[str, object]
    target_state: Mapping[str, object]
    single_use_marker: str


def load_descriptor(path: str | Path) -> RecoveryDescriptor:
    """Load the minimal generic descriptor boundary from project-owned data."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DescriptorError("descriptor cannot be loaded") from error
    required = {"id", "feature", "task_id", "expected_state", "target_state", "single_use_marker"}
    if not isinstance(payload, dict) or required - payload.keys():
        raise DescriptorError("descriptor is missing required fields")
    if not all(isinstance(payload[field], str) and payload[field] for field in ("id", "feature", "task_id", "single_use_marker")):
        raise DescriptorError("descriptor identity fields must be non-empty strings")
    if not isinstance(payload["expected_state"], dict) or not isinstance(payload["target_state"], dict):
        raise DescriptorError("descriptor states must be objects")
    return RecoveryDescriptor(
        payload["id"], payload["feature"], payload["task_id"], payload["expected_state"],
        payload["target_state"], payload["single_use_marker"],
    )
