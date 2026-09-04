"""Filesystem fixture helpers shared across the suite (QG-01).

Most test modules open their own ``tempfile.TemporaryDirectory()`` and then write one or two
files into it by hand (a task file, a ``prompt.md``, a plan Markdown file). ``temp_root`` and
``write_file`` name that pattern once so new tests reach for a helper instead of re-deriving
``Path(...).parent.mkdir(parents=True, exist_ok=True)`` boilerplate.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def temp_root() -> Iterator[Path]:
    """Yield a fresh temporary directory as a :class:`Path`, cleaned up on exit."""
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def write_file(path: object, text: str) -> Path:
    """Write ``text`` to ``path`` as UTF-8, creating parent directories as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
