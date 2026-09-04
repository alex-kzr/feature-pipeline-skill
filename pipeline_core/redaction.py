"""Redact machine-local paths before persisting portable artifacts."""

from __future__ import annotations

import re
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

REPO_TOKEN = "<repo>"
TMP_TOKEN = "<tmp>"
HOME_TOKEN = "<home>"
SECRET_TOKEN = "<redacted>"
Rule = tuple[re.Pattern[str], str]

#: Secret shapes redacted from any captured output or assembled report before it is
#: persisted. Each entry is ``(pattern, replacement)`` and the replacement may reference
#: capture groups, so a ``KEY=value`` assignment keeps ``KEY=`` and loses only the value.
#: These run *before* the path rules and *before* any byte-budget truncation so a secret can
#: never be split into an unrecognizable fragment that survives the cut.
_SECRET_RULES: list[Rule] = [
    (re.compile(
        r"(?i)\b([A-Za-z0-9_.\-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key"
        r"|client[_-]?secret|auth(?:oriz(?:ation|ed))?)[A-Za-z0-9_.\-]*)"
        r"(\s*[:=]\s*|\s+)"
        r"[\"']?([^\s\"']{3,})[\"']?"),
     rf"\1\2{SECRET_TOKEN}"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), f"Bearer {SECRET_TOKEN}"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), SECRET_TOKEN),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), SECRET_TOKEN),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), SECRET_TOKEN),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), SECRET_TOKEN),
    (re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        re.DOTALL),
     SECRET_TOKEN),
]


def secret_rules() -> list[Rule]:
    """Return the secret-redaction rules; applied before path rules and truncation."""
    return list(_SECRET_RULES)


def output_rules(repo_root: str | Path | None = None) -> list[Rule]:
    """Full rule set for captured process output and assembled reports: secrets first,
    then the machine-local path spellings."""
    return secret_rules() + build_rules(repo_root)


def build_rules(repo_root: str | Path | None = None) -> list[Rule]:
    """Build case-insensitive rules, retaining both supplied and resolved spellings."""
    roots = (
        (Path(repo_root) if repo_root else None, REPO_TOKEN),
        (Path(tempfile.gettempdir()), TMP_TOKEN),
        (Path.home(), HOME_TOKEN),
    )
    rules: list[Rule] = []
    for root, token in roots:
        if root is None or len(str(root)) < 4:
            continue
        for path in _path_variants(root):
            native = str(path)
            for spelling in (native.replace("\\", "\\\\"), native, path.as_posix()):
                rules.append((re.compile(re.escape(spelling), re.IGNORECASE), token))
    return rules


def _path_variants(path: Path) -> tuple[Path, ...]:
    """Return native aliases for a root, including Windows short and long paths."""
    variants = [path, path.resolve()]
    if os.name == "nt":
        import ctypes

        for function in (ctypes.windll.kernel32.GetLongPathNameW, ctypes.windll.kernel32.GetShortPathNameW):
            buffer = ctypes.create_unicode_buffer(32768)
            if function(str(path), buffer, len(buffer)):
                variants.append(Path(buffer.value))
    return tuple(dict.fromkeys(variants))


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
