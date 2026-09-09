"""REC-01 — the versioned catalog resources are packaged and runtime-resolvable.

* ``pyproject.toml`` explicitly declares the catalog JSON/Markdown as build artifacts so a
  wheel and an sdist carry the exact revision without a source checkout (AC-5);
* every packaged revision directory resolves through ``importlib.resources`` (the same
  mechanism an installed consumer uses) and its ``catalog.json`` + generated ``INVENTORY.md``
  are present and loadable.

Standard library only.
"""

from __future__ import annotations

import tomllib
import unittest
from importlib import resources
from pathlib import Path

from feature_pipeline.domain.task_kinds import (
    CATALOG_PACKAGE,
    available_versions,
    load_catalog,
)

_CORE_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _CORE_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


class CatalogPackagingConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _load_pyproject()

    def test_catalog_resources_are_declared_as_build_artifacts(self) -> None:
        artifacts = (
            self.cfg.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("artifacts", [])
        )
        joined = " ".join(artifacts)
        self.assertIn("catalogs", joined, "catalog resources must be declared as artifacts")
        self.assertIn(".json", joined)
        self.assertIn(".md", joined)

    def test_sdist_includes_the_source_tree(self) -> None:
        sdist = (
            self.cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        )
        self.assertIn("src", sdist)

    def test_wheel_ships_the_feature_pipeline_package(self) -> None:
        packages = self.cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        self.assertIn("src/feature_pipeline", packages)


class CatalogResourceResolution(unittest.TestCase):
    def test_catalog_package_is_importable_resource_root(self) -> None:
        root = resources.files(CATALOG_PACKAGE).joinpath("task_kinds")
        self.assertTrue(root.is_dir())

    def test_every_packaged_revision_has_json_and_generated_inventory(self) -> None:
        versions = available_versions()
        self.assertIn("v1", versions)
        for version in versions:
            base = resources.files(CATALOG_PACKAGE).joinpath("task_kinds", version)
            self.assertTrue(base.joinpath("catalog.json").is_file(), version)
            self.assertTrue(base.joinpath("INVENTORY.md").is_file(), version)

    def test_packaged_revision_loads_via_importlib_resources(self) -> None:
        catalog = load_catalog("v1")
        self.assertGreaterEqual(len(catalog.task_kinds), 24)
        self.assertTrue(catalog.digest.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
