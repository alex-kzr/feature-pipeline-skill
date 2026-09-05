"""DOC-02 — the expired ``schemas`` compatibility shim is fully removed.

``docs/adr/007-compatibility-and-versioning-policy.md`` §8 grants the ``schemas`` ->
``feature_pipeline.contracts`` shim exactly one deprecation window and gates its removal on a
repository-wide import scan (this test). Once every call site has migrated to
``feature_pipeline.contracts``, the top-level ``schemas`` package must not exist, and no tracked
source file may reference the old namespace.

Standard library only.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]
_LEGACY_IMPORT_RE = re.compile(r"^\s*(?:import schemas\b|from schemas\b)")

# This file itself legitimately mentions the old namespace in prose/comments/patterns; it is
# excluded from the scan of *other* files.
_SELF = Path(__file__).resolve()


class NoLegacySchemasNamespace(unittest.TestCase):
    def test_schemas_package_does_not_exist(self) -> None:
        self.assertFalse(
            (_CORE_ROOT / "schemas").exists(),
            "the expired 'schemas' compatibility shim package must be deleted (DOC-02)",
        )

    def test_no_tracked_python_file_imports_the_legacy_namespace(self) -> None:
        offenders: list[str] = []
        for path in _CORE_ROOT.rglob("*.py"):
            if path.resolve() == _SELF:
                continue
            if any(part in {".git", "dist", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _LEGACY_IMPORT_RE.match(line):
                    offenders.append(f"{path.relative_to(_CORE_ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [], "old 'schemas' namespace import(s) found:\n" + "\n".join(offenders)
        )


if __name__ == "__main__":
    unittest.main()
