"""Atomic, redacted JSON artifact persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .redaction import build_rules, redact


class ArtifactError(Exception):
    """Base failure for portable artifact I/O."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ArtifactReadError(ArtifactError):
    """Raised when an artifact cannot be parsed."""


class ArtifactSchemaError(ArtifactError):
    """Raised when a parsed artifact fails a caller schema."""


def read_json(path: str | Path, *, schema: Callable[[Any], Any] | None = None) -> Any:
    """Read JSON and normalize I/O and schema failures to stable errors."""
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ArtifactReadError(f"cannot read JSON artifact at '{target}': {exc}", "unreadable-artifact") from None
    except json.JSONDecodeError as exc:
        raise ArtifactReadError(f"'{target}' is not valid JSON: {exc}", "invalid-json") from None
    if schema:
        try:
            schema(data)
        except ArtifactError:
            raise
        except Exception as exc:
            raise ArtifactSchemaError(f"'{target}' failed schema validation: {exc}", "invalid-schema") from exc
    return data


def write_json_atomic(path: str | Path, payload: Any, *, repo_root: str | Path | None = None) -> Path:
    """Serialize before touching disk, then replace the target atomically."""
    text = json.dumps(redact(payload, build_rules(repo_root)), indent=2, ensure_ascii=False) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
