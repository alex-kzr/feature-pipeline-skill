"""Non-escalating role composition for portable profiles."""

from __future__ import annotations

from typing import Sequence

from feature_pipeline.contracts import Profile, SchemaError, ensure_no_role_escalation
from feature_pipeline.inputs.profile import CompiledProfile


VERIFIER_ROLES = frozenset({"task_verifier", "test_verifier"})
WRITE_CAPABILITIES = frozenset({"write", "create", "delete", "modify", "filesystem_write"})


def compose_role(profile: Profile, role: str, extension: Sequence[str], adapter_grant: Sequence[str]) -> tuple[str, ...]:
    """Compose base role, extension, and adapter grants without widening privilege."""
    try:
        base = profile.role_grants[role]
    except KeyError:
        raise SchemaError(f"unknown role: {role}") from None
    extension_grant = ensure_no_role_escalation(base, extension)
    adapter = ensure_no_role_escalation(base, adapter_grant)
    ensure_no_role_escalation(base, set(extension_grant) | set(adapter))
    composed = tuple(sorted(base))
    if role in VERIFIER_ROLES and WRITE_CAPABILITIES & set(composed):
        raise SchemaError(f"verifier role '{role}' must be read-only")
    return composed


def canonical_stack_role(profile: CompiledProfile, stack: str, requested_role: str) -> tuple[str, tuple[str, ...]]:
    """Return the one role/grant pair owned by ``stacks[]``, never an inferred role.

    This intentionally compares the requested semantic role before any generated agent name or
    task type reaches an adapter, preserving capability independence from role naming.
    """
    binding = profile.stack_for(stack)
    if binding.role != requested_role:
        raise SchemaError(
            f"stack {stack!r} is canonically bound to role {binding.role!r}, not {requested_role!r}"
        )
    try:
        return binding.role, tuple(sorted(profile.role_grants[binding.role]))
    except KeyError:
        raise SchemaError(f"canonical stack role is unknown: {binding.role!r}") from None
