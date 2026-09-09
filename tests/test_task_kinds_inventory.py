"""REC-01 — the generated Markdown inventory tracks the JSON catalog exactly.

The committed ``src/feature_pipeline/catalogs/task_kinds/<version>/INVENTORY.md`` is a
generated view of ``catalog.json``; it is never hand-edited. This module is the drift guard:
regenerating it from the packaged catalog must reproduce the committed bytes, and the view
must mention every record and every distinct implementation status.

Regenerate with::

    uv run python -m feature_pipeline.domain.task_kinds

Standard library only.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from feature_pipeline.domain.task_kinds import (
    DEFAULT_CATALOG_VERSION,
    load_catalog,
    render_inventory,
)

_CORE_ROOT = Path(__file__).resolve().parents[1]
_INVENTORY = (
    _CORE_ROOT
    / "src" / "feature_pipeline" / "catalogs" / "task_kinds"
    / DEFAULT_CATALOG_VERSION / "INVENTORY.md"
)


class GeneratedInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.rendered = render_inventory(self.catalog)

    def test_committed_inventory_matches_the_generator(self) -> None:
        self.assertTrue(_INVENTORY.is_file(), f"missing generated inventory {_INVENTORY}")
        self.assertEqual(
            _INVENTORY.read_text(encoding="utf-8"),
            self.rendered,
            "INVENTORY.md is stale — regenerate with "
            "`uv run python -m feature_pipeline.domain.task_kinds`",
        )

    def test_inventory_is_marked_generated(self) -> None:
        self.assertIn("GENERATED", self.rendered.splitlines()[0])

    def test_every_record_and_status_appears(self) -> None:
        for kind in self.catalog.task_kinds:
            self.assertIn(f"`{kind.id}`", self.rendered)
        for status in {kind.status for kind in self.catalog.task_kinds}:
            self.assertIn(status, self.rendered)

    def test_digest_is_recorded_in_the_view(self) -> None:
        self.assertIn(self.catalog.digest, self.rendered)


if __name__ == "__main__":
    unittest.main()
