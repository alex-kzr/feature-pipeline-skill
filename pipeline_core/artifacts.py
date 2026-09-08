"""Atomic, redacted JSON artifact persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .redaction import build_rules, output_rules, redact, redact_text


class ArtifactError(Exception):
    """Base failure for portable artifact I/O."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ArtifactReadError(ArtifactError):
    """Raised when an artifact cannot be parsed."""


class ArtifactSchemaError(ArtifactError):
    """Raised when a parsed artifact fails a caller schema."""


def _os_path(path: str | Path) -> str:
    r"""A filesystem string safe to pass to ``os``/``pathlib`` on this host.

    On Windows the classic Win32 API caps a path at ``MAX_PATH`` (260) unless it is given in
    extended-length form, so a deep run directory under a long parent (a temp/scratch root, a
    CI workspace) makes an otherwise valid ``…/implementation-manifest-1.json.tmp`` write fail
    with ``FileNotFoundError`` even though its parent exists. Prefixing ``\\?\`` lifts the limit;
    it requires a fully-qualified, backslash-separated path with no ``.``/``..`` segments, which
    ``os.path.abspath`` already produces. A strict no-op off Windows and for short paths.
    """
    text = os.fspath(path)
    if os.name != "nt":
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\?\\") or len(absolute) < 240:
        return text
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


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
    os.makedirs(_os_path(target.parent), exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    tmp_os, target_os = _os_path(temporary), _os_path(target)
    try:
        with open(tmp_os, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_os, target_os)
    finally:
        if os.path.exists(tmp_os):
            os.unlink(tmp_os)
    return target


def write_text_atomic(path: str | Path, text: str, *, repo_root: str | Path | None = None) -> Path:
    """Persist a redacted text artifact (command log, diagnostic) atomically.

    Every text artifact the execution engine writes passes through here so redaction of
    secrets and machine-local paths is automatic for any future caller; the write is
    serialize-then-replace so a crash never leaves a half-written log behind.
    """
    redacted = redact_text(text, output_rules(repo_root))
    target = Path(path)
    os.makedirs(_os_path(target.parent), exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    tmp_os, target_os = _os_path(temporary), _os_path(target)
    try:
        with open(tmp_os, "w", encoding="utf-8") as handle:
            handle.write(redacted)
        os.replace(tmp_os, target_os)
    finally:
        if os.path.exists(tmp_os):
            os.unlink(tmp_os)
    return target
