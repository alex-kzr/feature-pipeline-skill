"""TC-10 manifest resolution rejects cross-stack and stale-content bundles."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from feature_pipeline.application.skill_bundles import (
    SkillBundleError, load_manifests, load_project_skill_bundle, resolve_skill_bundle,
)
from feature_pipeline.contracts import SchemaError
from feature_pipeline.inputs.profile import CompiledProfile
from pipeline_core.roles import canonical_stack_role


def _manifest(skill_id: str, classification: str, *, content: str = "reviewed text",
              dependencies: list[str] | None = None) -> dict[str, object]:
    return {"id": skill_id, "classification": classification,
            "permitted_roles": ["executor", "test_verifier"],
            "source": f"skills/{skill_id}/SKILL.md",
            "sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content,
            "required_dependencies": dependencies or [], "optional_references": []}


def _profile() -> CompiledProfile:
    raw = {"schema_version": 1, "project": "p", "anchors": {"agents_root": ".agents", "core_root": "core"},
           "run_state_path": ".pipeline/runs", "roles": [
               {"role": "executor", "min_grants": ["read", "write"]},
               {"role": "test_verifier", "min_grants": ["read"]}],
           "stacks": [{"id": "python", "role": "executor", "checks": ["py"]}],
           "task_routing": [{"task_type": "python", "working_root": ".", "stack": "python"}]}
    checks = {"schema_version": 1, "checks": [{"name": "py", "stack": "python", "argv": ["true"], "cwd": "."}]}
    return CompiledProfile.from_mapping(raw, checks)


def _write_project_profile(config: Path) -> None:
    config.mkdir(parents=True)
    profile = {
        "schema_version": 1, "project": "p",
        "anchors": {"agents_root": ".agents", "core_root": "feature-pipeline-skill"},
        "run_state_path": ".pipeline/runs",
        "roles": [
            {"role": "executor", "min_grants": ["read", "write"]},
            {"role": "test_verifier", "min_grants": ["read"]},
        ],
        "stacks": [{"id": "python", "role": "executor", "checks": ["py"]}],
        "task_routing": [{"task_type": "python", "working_root": ".", "stack": "python"}],
    }
    checks = {"schema_version": 1, "checks": [
        {"name": "py", "stack": "python", "argv": ["true"], "cwd": "."},
    ]}
    (config / "pipeline.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    (config / "checks.json").write_text(json.dumps(checks), encoding="utf-8")


class SkillBundleTests(unittest.TestCase):
    def test_resolves_matching_stack_and_neutral_dependencies_deterministically(self) -> None:
        manifests = load_manifests([_manifest("python", "stack:python", dependencies=["neutral"]),
                                    _manifest("neutral", "neutral")])
        bundle = resolve_skill_bundle(_profile(), stack="python", requested_role="executor",
                                      requested_ids=["python"], manifests=manifests)
        self.assertEqual([item.id for item in bundle.manifests], ["neutral", "python"])
        self.assertIn("reviewed text", bundle.render())

    def test_rejects_cross_stack_required_dependency(self) -> None:
        manifests = load_manifests([_manifest("python", "stack:python", dependencies=["rust"]),
                                    _manifest("rust", "stack:rust")])
        with self.assertRaisesRegex(SkillBundleError, "incompatible with stack"):
            resolve_skill_bundle(_profile(), stack="python", requested_role="executor",
                                 requested_ids=["python"], manifests=manifests)

    def test_rejects_a_stack_without_a_canonical_binding(self) -> None:
        manifests = load_manifests([_manifest("rust", "stack:rust")])
        with self.assertRaisesRegex(SkillBundleError, "canonical stack binding"):
            resolve_skill_bundle(_profile(), stack="rust", requested_role="executor",
                                 requested_ids=["rust"], manifests=manifests)

    def test_rejects_unknown_role_and_dependency_cycle(self) -> None:
        manifests = load_manifests([_manifest("one", "stack:python", dependencies=["two"]),
                                    _manifest("two", "neutral", dependencies=["one"])])
        with self.assertRaisesRegex(SkillBundleError, "incompatible"):
            resolve_skill_bundle(_profile(), stack="python", requested_role="test_verifier",
                                 requested_ids=["one"], manifests=manifests)
        with self.assertRaisesRegex(SkillBundleError, "cycle"):
            resolve_skill_bundle(_profile(), stack="python", requested_role="executor",
                                 requested_ids=["one"], manifests=manifests)

    def test_rejects_unknown_bundle_recipient_and_missing_canonical_role_grant(self) -> None:
        manifests = load_manifests([_manifest("python", "stack:python")])
        with self.assertRaisesRegex(SkillBundleError, "recipient role is unknown"):
            resolve_skill_bundle(_profile(), stack="python", requested_role="executor",
                                 requested_ids=["python"], manifests=manifests,
                                 recipient_role="missing")
        profile = _profile()
        profile.role_grants.pop("executor")
        with self.assertRaisesRegex(SchemaError, "canonical stack role is unknown"):
            canonical_stack_role(profile, "python", "executor")

    def test_canonical_stack_role_rejects_a_requested_role_mismatch(self) -> None:
        with self.assertRaisesRegex(SchemaError, "canonically bound"):
            canonical_stack_role(_profile(), "python", "test_verifier")

    def test_rejects_changed_content_with_a_stale_hash(self) -> None:
        raw = _manifest("python", "stack:python")
        raw["content"] = "changed after review"
        with self.assertRaisesRegex(SkillBundleError, "does not match sha256"):
            load_manifests([raw])

    def test_rejects_a_non_object_manifest_document(self) -> None:
        with self.assertRaisesRegex(SkillBundleError, "must be an object"):
            load_manifests(["[]"])

    def test_rejects_invalid_manifest_schema_and_conflicting_duplicates(self) -> None:
        invalid_cases = [
            ("permitted_roles", [""], "permitted_roles must be a list"),
            ("id", "", "needs non-empty"),
            ("classification", "python", "unknown skill classification"),
            ("source", "../skill.json", "source escapes its anchor"),
        ]
        for key, value, message in invalid_cases:
            with self.subTest(key=key):
                raw = _manifest("python", "stack:python")
                raw[key] = value
                with self.assertRaisesRegex(SkillBundleError, message):
                    load_manifests([raw])
        with self.assertRaisesRegex(SkillBundleError, "duplicate skill id"):
            load_manifests([_manifest("python", "stack:python"),
                            _manifest("python", "stack:python", content="different")])

    def test_rejects_unknown_disallowed_and_stale_requested_skills(self) -> None:
        manifest = load_manifests([_manifest("python", "stack:python")])["python"]
        with self.assertRaisesRegex(SkillBundleError, "unknown required skill"):
            resolve_skill_bundle(_profile(), stack="python", requested_role="executor",
                                 requested_ids=["missing"], manifests={})
        profile = _profile()
        profile.role_grants["auditor"] = ("read",)
        with self.assertRaisesRegex(SkillBundleError, "incompatible with role"):
            resolve_skill_bundle(profile, stack="python", requested_role="executor",
                                 requested_ids=["python"], manifests={"python": manifest},
                                 recipient_role="auditor")
        with self.assertRaisesRegex(SkillBundleError, "stale classification"):
            resolve_skill_bundle(_profile(), stack="python", requested_role="executor",
                                 requested_ids=["python"],
                                 manifests={"python": replace(manifest, content="changed")})

    def test_project_bundle_rejects_malformed_and_unavailable_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tools" / "feature-pipeline" / "config"
            _write_project_profile(config)
            descriptor = config / "skill-invalid.json"
            cases = [
                ("{", "invalid skill descriptor"),
                ("[]", "must be an object"),
                (json.dumps({"catalog": "unknown", "id": "python", "source": "skills/python.json"}),
                 "unknown skill descriptor catalog"),
                (json.dumps({"catalog": "feature_pipeline.catalogs.skill_bundles.v1", "id": "python", "source": "../escape"}),
                 "unsafe skill descriptor"),
                (json.dumps({"catalog": "feature_pipeline.catalogs.skill_bundles.v1", "id": "python", "source": "skills/missing.json"}),
                 "source is unavailable"),
            ]
            for document, message in cases:
                with self.subTest(message=message):
                    descriptor.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(SkillBundleError, message):
                        load_project_skill_bundle(root, task_type="python", recipient_role="executor")

    def test_project_bundle_requires_descriptors_with_matching_manifest_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tools" / "feature-pipeline" / "config"
            _write_project_profile(config)
            with self.assertRaisesRegex(SkillBundleError, "has no skill descriptors"):
                load_project_skill_bundle(root, task_type="python", recipient_role="executor")
            source = root / "skills" / "actual.json"
            source.parent.mkdir()
            source.write_text(json.dumps(_manifest("actual", "stack:python")), encoding="utf-8")
            (config / "skill-python.json").write_text(json.dumps({
                "catalog": "feature_pipeline.catalogs.skill_bundles.v1",
                "id": "declared", "source": "skills/actual.json",
            }), encoding="utf-8")
            with self.assertRaisesRegex(SkillBundleError, "IDs do not match"):
                load_project_skill_bundle(root, task_type="python", recipient_role="executor")

    def test_project_bundle_uses_canonical_route_and_recipient_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "tools" / "feature-pipeline" / "config"
            catalog = root / "feature-pipeline-skill" / "src" / "feature_pipeline" / "catalogs"
            config.mkdir(parents=True)
            catalog.mkdir(parents=True)
            profile = {
                "schema_version": 1, "project": "p",
                "anchors": {"agents_root": ".agents", "core_root": "feature-pipeline-skill"},
                "run_state_path": ".pipeline/runs",
                "roles": [
                    {"role": "executor", "min_grants": ["read", "write"]},
                    {"role": "test_verifier", "min_grants": ["read"]},
                ],
                "stacks": [{"id": "python", "role": "executor", "checks": ["py"]}],
                "task_routing": [{"task_type": "python", "working_root": ".", "stack": "python"}],
            }
            checks = {"schema_version": 1, "checks": [
                {"name": "py", "stack": "python", "argv": ["true"], "cwd": "."},
            ]}
            (config / "pipeline.profile.json").write_text(json.dumps(profile), encoding="utf-8")
            (config / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
            raw = _manifest("python", "stack:python")
            raw["permitted_roles"] = ["executor", "test_verifier"]
            source = catalog / "python.json"
            source.write_text(json.dumps(raw), encoding="utf-8")
            (config / "skill-python.json").write_text(json.dumps({
                "catalog": "feature_pipeline.catalogs.skill_bundles.v1",
                "id": "python", "source": "feature-pipeline-skill/src/feature_pipeline/catalogs/python.json",
            }), encoding="utf-8")

            bundle = load_project_skill_bundle(root, task_type="python", recipient_role="test_verifier")

        self.assertEqual(bundle.stack, "python")
        self.assertEqual(bundle.role, "test_verifier")
        self.assertIn("reviewed text", bundle.render())

    def test_shipped_rust_bundle_is_hashed_and_classified_for_rust_only(self) -> None:
        path = Path(__file__).parents[1] / "src" / "feature_pipeline" / "catalogs" / "skill_bundles" / "v1" / "rust.json"
        manifest = load_manifests([path.read_text(encoding="utf-8")])["rust-standard-library"]
        self.assertEqual(manifest.classification, "stack:rust")
        self.assertIn("neutral-pipeline-contract", manifest.required_dependencies)
