"""A shell-free argv value object.

Mirrors ``feature_pipeline.contracts._shell_free_argv`` and its ``SHELL_TOKENS`` set: a
command is a non-empty list of non-empty strings, and no token may embed a shell operator
(``|``, ``&&``, ``;``, ``>``, ``<``). Chained commands must be split into separate entries so
each one has its own recorded exit code. A bare string is rejected — it would hide a missing
split — as are ``bytes`` and mappings.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from .errors import InvalidArgv

#: Identical to ``feature_pipeline.contracts.SHELL_TOKENS``.
SHELL_TOKENS = frozenset({"|", "&&", ";", ">", "<"})


@dataclass(frozen=True)
class Argv:
    """A validated, shell-free command argument vector."""

    tokens: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> "Argv":
        if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
            raise InvalidArgv("argv must be a list of strings")
        if not isinstance(value, (list, tuple)):
            raise InvalidArgv("argv must be a list of strings")
        tokens: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise InvalidArgv("argv entry must be a non-empty string")
            tokens.append(item)
        if not tokens:
            raise InvalidArgv("argv must be a non-empty argv")
        for token in tokens:
            if any(symbol in token for symbol in SHELL_TOKENS):
                raise InvalidArgv(
                    f"argv contains a shell operator ('{token}') — split chained commands "
                    f"into separate entries so each has its own recorded exit code"
                )
        return cls(tuple(tokens))

    def __iter__(self) -> Iterator[str]:
        return iter(self.tokens)

    def __len__(self) -> int:
        return len(self.tokens)


__all__ = [
    "Argv",
    "SHELL_TOKENS",
]
