"""Repository-relative path and glob value objects.

The normalization and rejection rules are exactly those of
``feature_pipeline.contracts.validate_relative_path`` (the function every current call site
uses):

* a backslash is folded to ``/`` before anything else;
* a drive-qualified (``C:/…``), root-absolute (``/…``) or home (``~…``) path is rejected;
* any ``..`` segment is rejected — no anchor traversal;
* ``.`` and empty segments are dropped; an empty result normalizes to ``"."``.

``RelativePath`` additionally rejects a ``*`` (parity with
``contracts._spec_paths(allow_glob=False)``, used for ``documentation_impact`` and
``required_skills``). ``RelativeGlob`` keeps ``*`` so it can describe an allowed-scope entry
such as ``src/feature_pipeline/domain/**``, and — since **WT-02** — knows how to *match* a
path against itself: :meth:`RelativeGlob.matches` is the one canonical scope matcher the
deterministic scope gate (:mod:`feature_pipeline.application.execute_task`) and
:class:`feature_pipeline.domain.scope.AllowedScope` build on. Its ``*`` / ``**`` semantics
are byte-identical to the ``_scope_regex`` translation ``pipeline_core.worktree`` has always
used: ``**`` spans path separators (and swallows a following ``/``), ``*`` and ``?`` do not,
and no other metacharacter is honoured.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .errors import InvalidGlob, InvalidPath, PathNotContained

_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a normalized repository-relative scope glob to an anchored regex.

    ``**`` -> ``.*`` (and a directly following ``/`` is consumed); ``*`` -> ``[^/]*``;
    ``?`` -> ``[^/]``; every other character is matched literally. Byte-for-byte the
    translation in ``pipeline_core.worktree._scope_regex`` and
    ``feature_pipeline.infrastructure.worktree.inspector._scope_regex``.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                out.append(".*")
                index += 2
                if pattern[index : index + 1] == "/":
                    index += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def _normalize(value: object, *, error: type[InvalidPath | InvalidGlob]) -> str:
    """Apply the ``validate_relative_path`` rules and return the canonical string."""
    if not isinstance(value, str) or not value:
        raise error("path must be a non-empty string")
    path = value.replace("\\", "/")
    if _DRIVE_RE.match(path) or path.startswith(("/", "~")):
        raise error(f"{value!r} must be relative")
    parts = path.split("/")
    if any(part == ".." for part in parts):
        raise error(f"{value!r} must not traverse outside its anchor")
    return "/".join(part for part in parts if part not in ("", ".")) or "."


@dataclass(frozen=True)
class RelativePath:
    """A normalized repository-relative path with no glob metacharacter."""

    value: str

    @classmethod
    def parse(cls, value: object) -> "RelativePath":
        normalized = _normalize(value, error=InvalidPath)
        if "*" in normalized:
            raise InvalidPath(f"{value!r} must be a concrete path, not a glob")
        return cls(normalized)

    @property
    def segments(self) -> tuple[str, ...]:
        return () if self.value == "." else tuple(self.value.split("/"))

    def within(self, anchor: "RelativePath") -> bool:
        """True when this path is ``anchor`` itself or lies beneath it (lexical, no disk)."""
        if anchor.value == ".":
            return True
        return self.segments[: len(anchor.segments)] == anchor.segments

    def relative_to(self, anchor: "RelativePath") -> "RelativePath":
        """Return the portion of this path beneath ``anchor`` or raise ``PathNotContained``."""
        if not self.within(anchor):
            raise PathNotContained(f"{self.value!r} is not contained by {anchor.value!r}")
        rest = self.segments[len(anchor.segments) :]
        return RelativePath("/".join(rest) or ".")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RelativeGlob:
    """A normalized repository-relative path that may contain ``*`` glob metacharacters."""

    value: str

    @classmethod
    def parse(cls, value: object) -> "RelativeGlob":
        return cls(_normalize(value, error=InvalidGlob))

    @property
    def segments(self) -> tuple[str, ...]:
        return () if self.value == "." else tuple(self.value.split("/"))

    @property
    def is_glob(self) -> bool:
        return "*" in self.value

    def matches(self, path: "str | RelativePath") -> bool:
        """True when ``path`` equals this glob or matches its ``*`` / ``**`` translation.

        ``path`` is normalized with the same rules as :meth:`RelativePath.parse` (a
        backslash is folded, ``.`` / empty segments drop) before the test, so a
        Windows-style path is matched identically to its POSIX form. Mirrors
        ``pipeline_core.worktree._in_allowed_scope`` for a single pattern: an exact string
        equality shortcut, then the anchored regex.
        """
        target = (
            path.value if isinstance(path, RelativePath)
            else _normalize(path, error=InvalidPath)
        )
        return target == self.value or _glob_regex(self.value).match(target) is not None

    def __str__(self) -> str:
        return self.value


def ensure_within(anchor: RelativePath, candidate: RelativePath) -> RelativePath:
    """Return ``candidate`` when it is contained by ``anchor``; raise ``PathNotContained``."""
    if not candidate.within(anchor):
        raise PathNotContained(
            f"{candidate.value!r} escapes its anchor {anchor.value!r}"
        )
    return candidate


__all__ = [
    "RelativePath",
    "RelativeGlob",
    "ensure_within",
]
