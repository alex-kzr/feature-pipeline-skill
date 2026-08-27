"""Non-escalating role composition for portable profiles."""

from __future__ import annotations

from typing import Sequence

from schemas import Profile, SchemaError, ensure_no_role_escalation


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
