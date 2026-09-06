"""Metadata for historical contract tests that have an approved replacement."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar


TestCallable = TypeVar("TestCallable", bound=Callable)


@dataclass(frozen=True)
class SupersededCharacterization:
    finding: str
    adr: str
    replacement: str
    test_id: str


_RECORDS: dict[str, SupersededCharacterization] = {}


def superseded_characterization(
    *, finding: str, adr: str, replacement: str
) -> Callable[[TestCallable], TestCallable]:
    """Keep a historical test auditable without making it a release gate."""

    if not finding or not adr or not replacement:
        raise ValueError("superseded characterizations require finding, ADR, and replacement")

    def decorate(test: TestCallable) -> TestCallable:
        test_id = f"{test.__module__}.{test.__qualname__}"
        _RECORDS[test_id] = SupersededCharacterization(
            finding=finding,
            adr=adr,
            replacement=replacement,
            test_id=test_id,
        )
        test.__unittest_skip__ = True
        test.__unittest_skip_why__ = (
            f"superseded {finding}; see {adr}; replacement: {replacement}"
        )
        return test

    return decorate


def superseded_characterizations() -> tuple[SupersededCharacterization, ...]:
    return tuple(sorted(_RECORDS.values(), key=lambda record: record.test_id))


def validate_registry(project_root: Path) -> tuple[str, ...]:
    """Return deterministic diagnostics for invalid superseded-test metadata."""

    errors: list[str] = []
    for record in superseded_characterizations():
        if not (project_root / record.adr).is_file():
            errors.append(f"{record.test_id}: missing ADR '{record.adr}'")
        if _resolve(record.replacement) is None:
            errors.append(f"{record.test_id}: missing replacement '{record.replacement}'")
    return tuple(errors)


def _resolve(dotted_name: str) -> object | None:
    parts = dotted_name.split(".")
    for split in range(len(parts), 0, -1):
        try:
            resolved: object = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        for part in parts[split:]:
            resolved = getattr(resolved, part, None)
            if resolved is None:
                break
        if resolved is not None:
            return resolved
    return None
