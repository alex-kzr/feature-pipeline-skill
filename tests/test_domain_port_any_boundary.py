"""QG-02 — AC-2: no new ``Any`` crosses a domain or port boundary.

``docs/plans/tasks/QG-02_quality-performance-ratchets.md``. mypy's strict override
(``pyproject.toml``, ``[[tool.mypy.overrides]]`` for ``feature_pipeline.domain.*`` /
``feature_pipeline.ports.*``) catches *generic* ``Any`` (``disallow_any_generics``, e.g.
``list[Any]``) but not an explicit ``Any`` used directly as a type — a parameter or return
annotated ``Any``, or a bare ``Any`` reachable through a re-export. This module closes that gap
with a static, structural check independent of mypy: it walks every module under
``src/feature_pipeline/domain/`` and ``src/feature_pipeline/ports/`` and asserts none references
``typing.Any`` (however imported — ``from typing import Any``, ``import typing`` + ``typing.Any``,
or an aliased import), except in a prose comment or docstring, which this AST-based check never
sees in the first place.

``_ALLOWED_ANY_MODULES`` is empty today (measured at QG-02) — the assertion below counts as its
own evidence. Any future addition must be named here, same as the mypy debt lists in
``[tool.mypy]``, so a change that silently reintroduces ``Any`` at this boundary fails loudly
instead of drifting in unnoticed.

Standard library only.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _CORE_ROOT / "src" / "feature_pipeline"
_BOUNDARY_PACKAGES = ("domain", "ports")

# Named, not blanket: a module added here must say why. Empty at QG-02 (AC-2 measured evidence).
_ALLOWED_ANY_MODULES: frozenset[str] = frozenset()


def _iter_boundary_modules() -> list[Path]:
    modules: list[Path] = []
    for package in _BOUNDARY_PACKAGES:
        modules.extend(sorted((_SRC_ROOT / package).rglob("*.py")))
    return modules


def _module_name(path: Path) -> str:
    relative = path.relative_to(_SRC_ROOT).with_suffix("")
    return "feature_pipeline." + ".".join(relative.parts)


def _any_usages(path: Path) -> list[int]:
    """Line numbers where ``typing.Any`` is imported or referenced in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    any_local_names: set[str] = set()
    typing_module_names: set[str] = set()
    hits: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if alias.name == "Any":
                    any_local_names.add(alias.asname or alias.name)
                    hits.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    typing_module_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in any_local_names:
            hits.append(node.lineno)
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "Any"
            and isinstance(node.value, ast.Name)
            and node.value.id in typing_module_names
        ):
            hits.append(node.lineno)

    return sorted(set(hits))


class DomainPortAnyBoundaryTests(unittest.TestCase):
    def test_boundary_packages_exist(self) -> None:
        # A silently-empty walk would make every other assertion in this module vacuous.
        modules = _iter_boundary_modules()
        self.assertGreater(len(modules), 0)
        names = {_module_name(path).split(".")[1] for path in modules}
        self.assertEqual(names, set(_BOUNDARY_PACKAGES))

    def test_no_unnamed_any_at_the_domain_or_port_boundary(self) -> None:
        offenders: dict[str, list[int]] = {}
        for path in _iter_boundary_modules():
            module = _module_name(path)
            if module in _ALLOWED_ANY_MODULES:
                continue
            hits = _any_usages(path)
            if hits:
                offenders[module] = hits
        self.assertEqual(
            offenders,
            {},
            "typing.Any reaches the domain/port boundary in module(s) not named in "
            "_ALLOWED_ANY_MODULES: " + repr(offenders),
        )

    def test_allowlist_names_only_modules_that_exist(self) -> None:
        existing = {_module_name(path) for path in _iter_boundary_modules()}
        self.assertTrue(_ALLOWED_ANY_MODULES <= existing)


if __name__ == "__main__":
    unittest.main()
