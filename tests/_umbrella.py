"""Locate the umbrella (``alex-kzr/feature-pipeline``) working tree from a core test.

``quality-gates`` runs the core suite on a standalone ``alex-kzr/feature-pipeline-skill``
checkout whose root carries **no ``docs/``, no ``tools/``**, and only ``quality-gates.yml``
under ``.github/workflows/``. A handful of test modules assert properties of files that ship
*only* in the umbrella working tree — ``docs/**``,
``tools/feature-pipeline/config/pipeline.profile.json``,
``.github/workflows/core-promotion.yml`` and ``.github/workflows/installed-package.yml``. On a
standalone checkout those files structurally cannot exist (the mirror never carries them), so
the assertions are *inapplicable*, not failing.

* :func:`umbrella_root` walks the parents of this file for the first directory that carries
  the marker ``docs/contracts/feature-pipeline.md``. On the umbrella tree (the Windows dev box
  and the umbrella CI) that is byte-for-byte the legacy ``Path(__file__).resolve().parents[2]``
  anchor every dependent module used before UGA-18, so nothing changes there. With no marker
  it falls back to that same legacy anchor, so a caller that does not gate on
  :func:`require_umbrella` behaves exactly as before.
* :func:`require_umbrella` raises :class:`unittest.SkipTest`, naming the missing umbrella
  artifact, **iff** the marker is absent from every parent directory — i.e. the checkout is a
  standalone ``feature-pipeline-skill`` mirror. It is never a device to hide a code failure:
  on the umbrella tree the marker is present and nothing is skipped.

Standard library only.
"""

from __future__ import annotations

import unittest
from pathlib import Path

#: Ships only in the umbrella working tree, never in a standalone ``feature-pipeline-skill``
#: mirror; its presence marks the umbrella root.
_MARKER = Path("docs") / "contracts" / "feature-pipeline.md"

#: The anchor every dependent module used before UGA-18: ``.../feature-pipeline``.
_LEGACY_ROOT = Path(__file__).resolve().parents[2]


def _find_marker_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / _MARKER).is_file():
            return parent
    return None


def has_umbrella() -> bool:
    """True when this checkout sits inside an umbrella working tree."""
    return _find_marker_root() is not None


def umbrella_root() -> Path:
    """Return the umbrella working-tree root.

    The first parent directory carrying ``docs/contracts/feature-pipeline.md`` when one
    exists; otherwise the legacy ``Path(__file__).resolve().parents[2]`` anchor unchanged.
    """
    return _find_marker_root() or _LEGACY_ROOT


def require_umbrella(artifact: str = "docs/contracts/feature-pipeline.md") -> None:
    """Skip the calling test/module on a standalone core checkout.

    Raised solely when the umbrella marker ``docs/contracts/feature-pipeline.md`` is absent
    from every parent of this file — a structurally-inapplicable case, never a failure to
    hide. ``artifact`` names the umbrella-only file the caller needs, for the skip reason.
    """
    if _find_marker_root() is None:
        raise unittest.SkipTest(
            "standalone feature-pipeline-skill checkout: umbrella artifact "
            f"{artifact!r} is not present (no docs/contracts/feature-pipeline.md marker in "
            "any parent directory)"
        )
