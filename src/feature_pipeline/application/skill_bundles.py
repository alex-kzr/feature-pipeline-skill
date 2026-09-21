"""Deterministic, hash-bound skill bundle resolution.

The profile's ``stacks[].role`` entry is the sole authority for selecting a
semantic role.  Manifests only describe skills; they never infer a role from a
task type or generated agent name.  Content is carried in the resolved bundle
so a tool-free verifier can receive the exact reviewed text, not a promise to
read it later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, cast

from feature_pipeline.inputs.profile import CompiledProfile, UnknownStack


class SkillBundleError(ValueError):
    """A skill manifest or requested composition is unsafe or incompatible."""


def _safe_source(source: str) -> bool:
    path = PurePosixPath(source)
    return bool(source and source == source.strip() and not path.is_absolute()
                and "\\" not in source and ".." not in path.parts and "." not in path.parts)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillManifest:
    """One reviewed skill, including its immutable content hash and dependencies."""

    id: str
    classification: str
    permitted_roles: tuple[str, ...]
    source: str
    sha256: str
    content: str
    required_dependencies: tuple[str, ...] = ()
    optional_references: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "SkillManifest":
        def strings(name: str) -> tuple[str, ...]:
            value = raw.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise SkillBundleError(f"skill manifest {name} must be a list of non-empty strings")
            return tuple(value)

        skill_id = raw.get("id")
        classification = raw.get("classification")
        source = raw.get("source")
        sha256 = raw.get("sha256")
        content = raw.get("content")
        if not all(isinstance(value, str) and value for value in
                   (skill_id, classification, source, sha256, content)):
            raise SkillBundleError("skill manifest needs non-empty id, classification, source, sha256 and content")
        skill_id = cast(str, skill_id)
        classification = cast(str, classification)
        source = cast(str, source)
        sha256 = cast(str, sha256)
        content = cast(str, content)
        if classification != "neutral" and not classification.startswith("stack:"):
            raise SkillBundleError(f"unknown skill classification: {classification!r}")
        if not _safe_source(source):
            raise SkillBundleError(f"skill manifest source escapes its anchor: {source!r}")
        if sha256 != _digest(content):
            raise SkillBundleError(f"skill manifest {skill_id!r} content does not match sha256")
        return cls(skill_id, classification, strings("permitted_roles"), source, sha256, content,
                   strings("required_dependencies"), strings("optional_references"))


def load_manifests(documents: Iterable[Mapping[str, object] | str]) -> Mapping[str, SkillManifest]:
    """Load manifests, rejecting conflicting duplicate IDs and stale classifications."""
    loaded: dict[str, SkillManifest] = {}
    for document in documents:
        raw = json.loads(document) if isinstance(document, str) else document
        if not isinstance(raw, Mapping):
            raise SkillBundleError("skill manifest must be an object")
        manifest = SkillManifest.from_mapping(raw)
        prior = loaded.get(manifest.id)
        if prior is not None and prior != manifest:
            raise SkillBundleError(f"duplicate skill id with conflicting source or content: {manifest.id!r}")
        loaded[manifest.id] = manifest
    return dict(sorted(loaded.items()))


@dataclass(frozen=True)
class ResolvedSkillBundle:
    """The ordered, immutable skill text a role is permitted to receive."""

    stack: str
    role: str
    manifests: tuple[SkillManifest, ...]

    @property
    def digest(self) -> str:
        """The lowercase sha256 hex digest binding this exact ordered set of skill contents.

        Deterministic over ``(id, sha256)`` pairs in the bundle's own stable (sorted-id)
        order, so two resolutions of the same manifests always agree and any change to
        which skills or content were selected changes the digest (TC-11 AC-1).
        """
        return _digest("|".join(f"{manifest.id}:{manifest.sha256}" for manifest in self.manifests))

    def render(self) -> str:
        lines = ["Reviewed permitted skills and references (immutable):"]
        for manifest in self.manifests:
            lines += [
                f"--- skill: {manifest.id} ({manifest.source}, sha256:{manifest.sha256}) ---",
                manifest.content.rstrip("\n"),
            ]
        return "\n".join(lines) + "\n"


def resolve_skill_bundle(
    profile: CompiledProfile,
    *,
    stack: str,
    requested_role: str,
    requested_ids: Iterable[str],
    manifests: Mapping[str, SkillManifest],
    recipient_role: str | None = None,
) -> ResolvedSkillBundle:
    """Resolve required transitive skills for the profile-owned stack/role binding."""
    try:
        binding = profile.stack_for(stack)
    except UnknownStack as exc:
        raise SkillBundleError(f"task route has no canonical stack binding: {stack!r}") from exc
    if requested_role != binding.role:
        raise SkillBundleError(
            f"requested role {requested_role!r} is incompatible with stack {stack!r}; "
            f"canonical role is {binding.role!r}"
        )
    if requested_role not in profile.role_grants:
        raise SkillBundleError(f"canonical stack role is unknown: {requested_role!r}")
    recipient = recipient_role or requested_role
    if recipient not in profile.role_grants:
        raise SkillBundleError(f"bundle recipient role is unknown: {recipient!r}")

    selected: dict[str, SkillManifest] = {}
    visiting: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in selected:
            return
        if skill_id in visiting:
            raise SkillBundleError(f"required skill dependency cycle at {skill_id!r}")
        try:
            manifest = manifests[skill_id]
        except KeyError:
            raise SkillBundleError(f"unknown required skill: {skill_id!r}") from None
        if manifest.classification not in ("neutral", f"stack:{stack}"):
            raise SkillBundleError(
                f"required skill {skill_id!r} is incompatible with stack {stack!r}")
        if recipient not in manifest.permitted_roles:
            raise SkillBundleError(
                f"required skill {skill_id!r} is incompatible with role {recipient!r}")
        if manifest.sha256 != _digest(manifest.content):
            raise SkillBundleError(f"skill {skill_id!r} changed content with stale classification")
        visiting.add(skill_id)
        for dependency in manifest.required_dependencies:
            visit(dependency)
        visiting.remove(skill_id)
        selected[skill_id] = manifest

    for skill_id in requested_ids:
        visit(skill_id)
    return ResolvedSkillBundle(stack, recipient, tuple(selected[key] for key in sorted(selected)))


def load_project_skill_bundle(
    project_root: Path | str, *, task_type: str, recipient_role: str,
) -> ResolvedSkillBundle | None:
    """Resolve a configured project's bundle for one routed task and recipient role.

    A repository without the optional project profile remains compatible with the portable
    core fixtures.  Once the profile exists, however, every descriptor and anchored manifest
    is validated before a prompt is composed; a malformed bundle is never silently omitted.
    """
    root = Path(project_root).resolve()
    config = root / "tools" / "feature-pipeline" / "config"
    profile_path = config / "pipeline.profile.json"
    if not profile_path.is_file():
        return None
    profile = CompiledProfile.from_path(profile_path)
    route = profile.route_for(task_type)
    binding = profile.stack_for(route.stack)
    documents: list[str] = []
    ids: list[str] = []
    for descriptor_path in sorted(config.glob("skill*.json")):
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillBundleError(f"invalid skill descriptor {descriptor_path.name}: {exc}") from None
        if not isinstance(descriptor, Mapping):
            raise SkillBundleError(f"skill descriptor {descriptor_path.name} must be an object")
        skill_id, source = descriptor.get("id"), descriptor.get("source")
        if descriptor.get("catalog") != "feature_pipeline.catalogs.skill_bundles.v1":
            raise SkillBundleError(f"unknown skill descriptor catalog: {descriptor_path.name}")
        if not isinstance(skill_id, str) or not skill_id or not isinstance(source, str) or not _safe_source(source):
            raise SkillBundleError(f"unsafe skill descriptor: {descriptor_path.name}")
        source_path = (root / source).resolve()
        try:
            source_path.relative_to(root)
        except ValueError:
            raise SkillBundleError(f"skill descriptor source escapes project: {source!r}") from None
        try:
            document = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillBundleError(f"skill manifest source is unavailable: {source!r}") from exc
        documents.append(document)
        ids.append(skill_id)
    if not documents:
        raise SkillBundleError("configured project has no skill descriptors")
    manifests = load_manifests(documents)
    if set(ids) != set(manifests):
        raise SkillBundleError("skill descriptor IDs do not match their anchored manifests")
    requested_ids = sorted(
        skill_id for skill_id, manifest in manifests.items()
        if manifest.classification in ("neutral", f"stack:{route.stack}")
    )
    return resolve_skill_bundle(
        profile, stack=route.stack, requested_role=binding.role,
        requested_ids=requested_ids, manifests=manifests, recipient_role=recipient_role,
    )
