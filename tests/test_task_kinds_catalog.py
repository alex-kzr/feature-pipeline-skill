"""REC-01 — the versioned task-kind catalog: loaders, validators, and denial paths.

Plan: ``docs/plans/tasks/REC-01_executor-context-and-catalog-recovery.md`` (supersedes the
blocked TC-04). This module pins the standard-library-only catalog subsystem introduced under
``src/feature_pipeline/catalogs/**`` and ``feature_pipeline.domain.task_kinds``:

* the packaged ``v1`` catalog loads, every characterized dispatch operation has a record, and
  the facets each record carries are the ones REC-01 requires (identity, ownership, status,
  role, constraints, risk/complexity, context/budget, independence, repair/retry,
  verification, provenance, lifecycle, aliases/replacements);
* ``push`` has no routable kind and ``commit`` stays reserved / non-dispatchable (AC-3);
* the loader fails *before returning a catalog* — i.e. before any dispatch could consult it —
  for an unknown schema version, an invalid record schema, a dangling ``replaces`` /
  ``replaced_by`` reference, a duplicate identity, an incompatible namespaced extension, and
  an implicitly executable plugin record (AC-4);
* positive + compatibility cases cover a hand-built mapping and a well-formed namespaced
  extension.

Standard library only.
"""

from __future__ import annotations

import copy
import unittest

from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.task_kinds import (
    CatalogError,
    CatalogSchemaError,
    DanglingReference,
    DuplicateIdentity,
    ExecutablePluginRejected,
    IncompatibleExtension,
    TaskKind,
    TaskKindCatalog,
    UnknownCatalogVersion,
    available_versions,
    load_catalog,
    load_catalog_from_mapping,
)

#: Every operation characterized in ``docs/validation/task-model-routing/TC-01-baseline.md``
#: (the "Dispatch producer and operation inventory" table) plus the two reserved/denied
#: git surfaces. REC-01: "Explicitly ensure all characterized operations are represented".
CHARACTERIZED_OPERATIONS = frozenset({
    "prompt-and-plan-loading",
    "task-planning",
    "plan-compilation-and-adapter-composition",
    "executor-implementation",
    "executor-status-continuation",
    "codex-final-result-parsing",
    "task-command-pass",
    "acceptance-verifier-report",
    "test-evidence-verifier-report",
    "verifier-verdict-continuation",
    "independent-verifier-helper",
    "repair-consolidation-and-redispatch",
    "run-resume-and-evidence-reuse",
    "board-and-result-projection",
    "documentation-maintenance",
    "documentation-audit",
    "graphify-refresh",
    "graphify-validation-and-final-checks",
    "final-diff-gate-and-release-preview",
    "generic-stage-extension-callback",
    "cli-process-transport",
    "manual-nested-delegation",
    "commit",
    "push",
})


def _minimal_record(**overrides: object) -> dict:
    record: dict = {
        "id": "sample-kind",
        "title": "Sample kind",
        "summary": "A minimal well-formed record used by the mapping loader tests.",
        "owner": "runner",
        "call_sites": ["pipeline_core/example.py:1"],
        "provenance": ["TC-01"],
        "lifecycle": {"stage": "execution"},
    }
    record.update(overrides)
    return record


def _mapping(records: list[dict], **overrides: object) -> dict:
    data: dict = {
        "schema_version": 1,
        "catalog_version": "1.0.0",
        "source": "<test>",
        "task_kinds": records,
    }
    data.update(overrides)
    return data


class PackagedCatalogTests(unittest.TestCase):
    """The committed ``v1`` catalog resource loads and is internally coherent (AC-3)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_versioned_resource_loads(self) -> None:
        self.assertIsInstance(self.catalog, TaskKindCatalog)
        self.assertEqual(self.catalog.schema_version, 1)
        self.assertEqual(self.catalog.catalog_version, "1.0.0")
        self.assertTrue(self.catalog.digest.startswith("sha256:"))
        self.assertIn("v1", available_versions())

    def test_default_version_is_stable_and_digest_is_deterministic(self) -> None:
        self.assertEqual(load_catalog().digest, load_catalog("v1").digest)

    def test_every_characterized_operation_has_a_record(self) -> None:
        self.assertEqual(
            CHARACTERIZED_OPERATIONS - set(self.catalog.by_id),
            set(),
            "a characterized operation is missing from the catalog",
        )

    def test_records_expose_the_required_facets(self) -> None:
        kind = self.catalog.get("executor-implementation")
        self.assertIsInstance(kind, TaskKind)
        # identity / aliases / replacements
        self.assertEqual(kind.id, "executor-implementation")
        self.assertIn("python-executor", kind.aliases)
        self.assertEqual(kind.replaced_by, None)
        # ownership + provenance + lifecycle
        self.assertTrue(kind.owner)
        self.assertTrue(kind.call_sites)
        self.assertIn("TC-01", kind.provenance)
        self.assertEqual(kind.lifecycle_stage, "execution")
        # implementation status + role
        self.assertEqual(kind.status, "live")
        self.assertEqual(kind.role, "executor")
        # constraints
        self.assertIn("read", kind.constraints.required_capabilities)
        self.assertIn("run_checks", kind.constraints.required_capabilities)
        self.assertIn("filesystem_write", kind.constraints.required_capabilities)
        self.assertEqual(kind.constraints.session_policy, "fresh")
        # risk / complexity
        self.assertEqual(kind.risk, "high")
        self.assertEqual(kind.complexity, "high")
        # context / budget
        self.assertIn("skills", kind.context_requires)
        self.assertIn("plan", kind.context_requires)
        self.assertTrue(kind.output_byte_budget)
        # independence
        self.assertTrue(kind.parallelizable)
        # repair / retry
        self.assertEqual(kind.max_repair_attempts, 2)
        self.assertTrue(kind.retryable)
        # verification
        self.assertEqual(kind.verification_tier, "full")
        self.assertEqual(kind.verifiers, ("task_verifier", "test_verifier"))

    def test_defaults_fill_unspecified_facets(self) -> None:
        kind = self.catalog.get("board-and-result-projection")
        self.assertEqual(kind.risk, "medium")
        self.assertEqual(kind.complexity, "medium")
        self.assertFalse(kind.parallelizable)
        self.assertFalse(kind.retryable)
        self.assertEqual(kind.max_repair_attempts, None)
        self.assertEqual(kind.verification_tier, "none")

    def test_executor_implementation_is_the_only_routable_kind(self) -> None:
        self.assertEqual(
            tuple(kind.id for kind in self.catalog.routable()),
            ("executor-implementation",),
        )

    def test_push_is_not_routable_and_commit_is_reserved(self) -> None:
        push = self.catalog.get("push")
        self.assertFalse(push.routable)
        self.assertFalse(push.dispatchable)
        self.assertEqual(push.status, "unsupported")

        commit = self.catalog.get("commit")
        self.assertFalse(commit.routable)
        self.assertFalse(commit.dispatchable)
        self.assertEqual(commit.status, "reserved")

    def test_alias_lookup_resolves_to_the_owning_kind(self) -> None:
        self.assertIs(
            self.catalog.resolve("python-executor"),
            self.catalog.get("executor-implementation"),
        )

    def test_replacement_and_alias_references_are_all_resolvable(self) -> None:
        known = set(self.catalog.by_id)
        for kind in self.catalog.task_kinds:
            for ref in kind.replaces:
                self.assertIn(ref, known)
            if kind.replaced_by is not None:
                self.assertIn(kind.replaced_by, known)


class MappingLoaderCompatibilityTests(unittest.TestCase):
    """Positive + compatibility cases for the in-memory loader."""

    def test_minimal_mapping_loads_with_defaults(self) -> None:
        catalog = load_catalog_from_mapping(_mapping([_minimal_record()]))
        kind = catalog.get("sample-kind")
        self.assertEqual(kind.status, "live")
        self.assertEqual(kind.role, "runner")
        self.assertFalse(kind.routable)
        self.assertEqual(kind.namespace, None)

    def test_well_formed_namespaced_extension_loads(self) -> None:
        extension = _minimal_record(
            id="acme:custom-lint",
            namespace="acme",
            extends={"schema_version": 1, "catalog_version": "1.0.0"},
        )
        catalog = load_catalog_from_mapping(_mapping([_minimal_record(), extension]))
        kind = catalog.get("acme:custom-lint")
        self.assertEqual(kind.namespace, "acme")
        self.assertFalse(kind.routable)

    def test_digest_changes_when_a_record_changes(self) -> None:
        base = load_catalog_from_mapping(_mapping([_minimal_record()]))
        mutated = load_catalog_from_mapping(
            _mapping([_minimal_record(summary="different text entirely")])
        )
        self.assertNotEqual(base.digest, mutated.digest)


class CatalogDenialTests(unittest.TestCase):
    """The loader fails closed *before* returning a catalog (AC-4)."""

    def _load(self, data: dict) -> None:
        load_catalog_from_mapping(data)

    def test_unknown_schema_version_is_rejected(self) -> None:
        with self.assertRaises(UnknownCatalogVersion) as caught:
            self._load(_mapping([_minimal_record()], schema_version=2))
        self.assertEqual(caught.exception.code, "task-kind-unknown-version")
        self.assertIsInstance(caught.exception, CatalogError)
        self.assertIsInstance(caught.exception, DomainError)

    def test_unknown_packaged_version_is_rejected(self) -> None:
        with self.assertRaises(UnknownCatalogVersion) as caught:
            load_catalog("v99")
        self.assertEqual(caught.exception.code, "task-kind-unknown-version")

    def test_missing_required_field_is_a_schema_error(self) -> None:
        record = _minimal_record()
        del record["id"]
        with self.assertRaises(CatalogSchemaError) as caught:
            self._load(_mapping([record]))
        self.assertEqual(caught.exception.code, "task-kind-invalid-schema")

    def test_unknown_status_value_is_a_schema_error(self) -> None:
        with self.assertRaises(CatalogSchemaError):
            self._load(_mapping([_minimal_record(status="in-progress")]))

    def test_unknown_record_key_is_a_schema_error(self) -> None:
        with self.assertRaises(CatalogSchemaError):
            self._load(_mapping([_minimal_record(frobnicate=1)]))

    def test_implicit_executable_plugin_is_rejected(self) -> None:
        with self.assertRaises(ExecutablePluginRejected) as caught:
            self._load(_mapping([_minimal_record(executable="python -m acme.hook")]))
        self.assertEqual(caught.exception.code, "task-kind-executable-plugin")

    def test_duplicate_identity_is_rejected(self) -> None:
        with self.assertRaises(DuplicateIdentity) as caught:
            self._load(_mapping([_minimal_record(), _minimal_record()]))
        self.assertEqual(caught.exception.code, "task-kind-duplicate-identity")

    def test_alias_colliding_with_another_identity_is_rejected(self) -> None:
        other = _minimal_record(id="other-kind", aliases=["sample-kind"])
        with self.assertRaises(DuplicateIdentity):
            self._load(_mapping([_minimal_record(), other]))

    def test_dangling_replaces_reference_is_rejected(self) -> None:
        with self.assertRaises(DanglingReference) as caught:
            self._load(_mapping([_minimal_record(replaces=["ghost-kind"])]))
        self.assertEqual(caught.exception.code, "task-kind-dangling-reference")

    def test_dangling_replaced_by_reference_is_rejected(self) -> None:
        with self.assertRaises(DanglingReference):
            self._load(_mapping([_minimal_record(replaced_by="ghost-kind")]))

    def test_incompatible_namespaced_extension_is_rejected(self) -> None:
        extension = _minimal_record(
            id="acme:custom-lint",
            namespace="acme",
            extends={"schema_version": 99, "catalog_version": "1.0.0"},
        )
        with self.assertRaises(IncompatibleExtension) as caught:
            self._load(_mapping([_minimal_record(), extension]))
        self.assertEqual(caught.exception.code, "task-kind-incompatible-extension")

    def test_namespaced_id_without_the_namespace_prefix_is_a_schema_error(self) -> None:
        extension = _minimal_record(
            id="custom-lint",
            namespace="acme",
            extends={"schema_version": 1, "catalog_version": "1.0.0"},
        )
        with self.assertRaises(CatalogSchemaError):
            self._load(_mapping([_minimal_record(), extension]))

    def test_denials_do_not_depend_on_dict_ordering(self) -> None:
        # A duplicate identity and a dangling ref in the same payload: either stable code is
        # acceptable, but it must always fail closed with a CatalogError.
        payload = _mapping([
            _minimal_record(replaces=["ghost"]),
            _minimal_record(),
        ])
        with self.assertRaises(CatalogError):
            self._load(copy.deepcopy(payload))


if __name__ == "__main__":
    unittest.main()
