"""Redact machine-local paths before persisting portable artifacts."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

REPO_TOKEN = "<repo>"
TMP_TOKEN = "<tmp>"
HOME_TOKEN = "<home>"
Rule = tuple[re.Pattern[str], str]


def build_rules(repo_root: str | Path | None = None) -> list[Rule]:
    """Build case-insensitive rules, with narrower roots taking precedence."""
    roots = (
        (Path(repo_root).resolve() if repo_root else None, REPO_TOKEN),
        (Path(tempfile.gettempdir()).resolve(), TMP_TOKEN),
        (Path.home().resolve(), HOME_TOKEN),
    )
    rules: list[Rule] = []
    for root, token in roots:
        if root is None or len(str(root)) < 4:
            continue
        native = str(root)
        for spelling in (native.replace("\\", "\\\\"), native, root.as_posix()):
            rules.append((re.compile(re.escape(spelling), re.IGNORECASE), token))
    return rules


def redact_text(text: str | None, rules: Iterable[Rule]) -> str:
    """Replace only configured absolute-path spellings in text."""
    result = text or ""
    for pattern, token in rules:
        result = pattern.sub(token, result)
    return result


def redact(value: Any, rules: Iterable[Rule]) -> Any:
    """Recursively redact strings without altering non-string evidence values."""
    if isinstance(value, str):
        return redact_text(value, rules)
    if isinstance(value, dict):
        return {key: redact(item, rules) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, rules) for item in value]
    return value
