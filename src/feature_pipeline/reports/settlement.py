"""One parameterized report-protocol settlement parser (VP-02).

Before VP-02 ``pipeline_core.reports`` carried two near-identical strict parsers — one for the
executor *status* envelope (``implemented`` / ``blocked``) and one for the independent
verifier *verdict* envelope (``PASS`` / ``FAIL`` / ``BLOCKED``) — plus two prose parsers and
two settlement functions that reconciled the prose line against the authoritative JSON
envelope. The two paths never actually diverged; they only differed in the token vocabulary,
the strict key set, and a handful of nouns baked into the error strings.

This module collapses them onto **one** implementation. An :class:`EnvelopeContract` captures
every point of variation; :func:`parse_envelope`, :func:`parse_prose_token`, and
:func:`settle` are written once and take a contract. :data:`STATUS_CONTRACT` and
:data:`VERDICT_CONTRACT` are the two supported envelopes. The error ``code`` slugs and the
human-readable messages are byte-identical to the pre-VP-02 strings, so
``pipeline_core.reports`` can delegate here without changing any golden or any caller.

Standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Final


class ReportProtocolError(ValueError):
    """A report-protocol settlement failure.

    ``code`` is a stable, machine-readable reason — safe to branch on and to record in
    evidence. ``pipeline_core.reports`` re-raises this as its historical ``ReportError`` with
    the same ``code`` and message.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EnvelopeContract:
    """Every point of variation between the supported report envelopes.

    One frozen description per envelope kind; the parsers below are generic over it. The
    ``*_noun`` / ``*_code`` fields exist only to reproduce the pre-VP-02 error strings
    verbatim.
    """

    name: str
    keys: frozenset[str]
    token_field: str
    token_values: tuple[str, ...]
    envelope_noun: str
    report_noun: str
    prose_field: str
    prose_hint: str
    disagreement_noun: str
    drift_noun: str
    envelope_code: str
    mismatch_code: str
    normalize: Callable[[str], str]
    prose_code: str = "unparseable-report"
    #: The optional extra envelope key that carries a human-readable reason for a token in
    #: :attr:`reason_eligible_tokens` (e.g. ``"blocked"``). ``None`` when this envelope kind
    #: never carries a reason (the verifier verdict envelope). The key is additive. A caller
    #: may require it for an eligible token when settling a protocol that owns that guarantee;
    #: otherwise an eligible envelope may omit it. If present it must be a non-empty string,
    #: and it must be *absent* for every other token — a reason next to ``"implemented"`` is
    #: contradictory, not merely unused.
    reason_field: str | None = None
    reason_eligible_tokens: frozenset[str] = frozenset()

    @property
    def prose_pattern(self) -> re.Pattern[str]:
        """A ``<Field>: <token>`` line, tolerant of a leading ``- `` and ``*``/``_`` emphasis."""
        alternation = "|".join(re.escape(value) for value in self.token_values)
        return re.compile(
            rf"(?im)^\s*-?\s*[*_]*\s*{re.escape(self.prose_field)}\s*[*_]*\s*:\s*[*_]*\s*"
            rf"({alternation})\b"
        )


STATUS_CONTRACT: Final = EnvelopeContract(
    name="status",
    keys=frozenset({"role", "status", "task_id", "attempt"}),
    token_field="status",
    token_values=("implemented", "blocked"),
    envelope_noun="status envelope",
    report_noun="executor",
    prose_field="Status",
    prose_hint="- Status: implemented | blocked",
    disagreement_noun="executor status",
    drift_noun="status",
    envelope_code="unparseable-status-envelope",
    mismatch_code="status-envelope-mismatch",
    normalize=str.lower,
    reason_field="reason",
    reason_eligible_tokens=frozenset({"blocked"}),
)

VERDICT_CONTRACT: Final = EnvelopeContract(
    name="verdict",
    keys=frozenset({"role", "verdict", "task_id", "attempt"}),
    token_field="verdict",
    token_values=("PASS", "FAIL", "BLOCKED"),
    envelope_noun="verdict envelope",
    report_noun="verifier",
    prose_field="Verdict",
    prose_hint="- Verdict: PASS | FAIL | BLOCKED",
    disagreement_noun="verifier verdict",
    drift_noun="verdict",
    envelope_code="unparseable-verdict-envelope",
    mismatch_code="verdict-envelope-mismatch",
    normalize=str.upper,
)

#: Every supported report envelope, keyed by :attr:`EnvelopeContract.name` (AC-1).
CONTRACTS: Final[dict[str, EnvelopeContract]] = {
    STATUS_CONTRACT.name: STATUS_CONTRACT,
    VERDICT_CONTRACT.name: VERDICT_CONTRACT,
}


def parse_prose_token(text: str, contract: EnvelopeContract) -> str:
    """The normalized token from the prose ``<Field>:`` line, or raise ``unparseable-report``."""
    match = contract.prose_pattern.search(text or "")
    if not match:
        raise ReportProtocolError(
            f"{contract.report_noun} report has no parseable '{contract.prose_hint}' line — a "
            f"claim without the required field is not evidence",
            contract.prose_code,
        )
    return contract.normalize(match.group(1))


def _parse_envelope_data(
    text: str, contract: EnvelopeContract, *, role: str, task_id: str, attempt: int,
    require_reason: bool = False,
) -> dict:
    """Strictly parse and validate one JSON report envelope, returning the raw mapping.

    :func:`parse_envelope` exposes only the settled token, unchanged for every historical
    caller; :func:`settle` needs the raw mapping too, so it can also recover an optional
    blocked reason (RLC-01 AC-2) without a second, looser parse of the same text.
    """
    stripped = (text or "").strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        raise ReportProtocolError(
            f"{contract.envelope_noun} is not valid JSON: {stripped[:200]!r}",
            contract.envelope_code,
        ) from None
    if not isinstance(data, dict):
        raise ReportProtocolError(
            f"{contract.envelope_noun} is not a JSON object", contract.envelope_code
        )
    present = set(data.keys())
    base_keys = set(contract.keys)
    # A caller may require the reason key for an eligible token. A reason key next to any
    # other token is always contradictory and rejected.
    token_peek = data.get(contract.token_field)
    reason_eligible = bool(contract.reason_field) and token_peek in contract.reason_eligible_tokens
    expected_keys = (
        base_keys | {contract.reason_field}
        if reason_eligible and require_reason
        else base_keys
    )
    allowed_keys = base_keys | ({contract.reason_field} if reason_eligible else set())
    if (not present <= allowed_keys) or (require_reason and present != expected_keys):
        raise ReportProtocolError(
            f"{contract.envelope_noun} has keys {sorted(present)}, expected exactly "
            f"{sorted(expected_keys)} — none missing, none extra",
            contract.envelope_code,
        )
    token = data.get(contract.token_field)
    if token not in contract.token_values:
        raise ReportProtocolError(
            f"{contract.envelope_noun} '{contract.token_field}' is {token!r}, expected one of "
            f"{list(contract.token_values)}",
            contract.envelope_code,
        )
    if data.get("role") != role:
        raise ReportProtocolError(
            f"{contract.envelope_noun} declares role {data.get('role')!r}, expected {role!r}",
            contract.envelope_code,
        )
    if data.get("task_id") != task_id:
        raise ReportProtocolError(
            f"{contract.envelope_noun} declares task_id {data.get('task_id')!r}, expected "
            f"{task_id!r}",
            contract.envelope_code,
        )
    if data.get("attempt") != attempt or isinstance(data.get("attempt"), bool):
        raise ReportProtocolError(
            f"{contract.envelope_noun} declares attempt {data.get('attempt')!r}, expected "
            f"{attempt!r}",
            contract.envelope_code,
        )
    if contract.reason_field and contract.reason_field in present:
        reason_value = data.get(contract.reason_field)
        if not isinstance(reason_value, str) or not reason_value.strip():
            raise ReportProtocolError(
                f"{contract.envelope_noun} '{contract.reason_field}' is present but empty or "
                f"not a string — a claim without a real reason is not evidence",
                contract.envelope_code,
            )
    return data


def parse_envelope(
    text: str, contract: EnvelopeContract, *, role: str, task_id: str, attempt: int,
    require_reason: bool = False,
) -> str:
    """Strictly parse one JSON report envelope. Never infers, never falls back to the prose."""
    data = _parse_envelope_data(
        text, contract, role=role, task_id=task_id, attempt=attempt,
        require_reason=require_reason,
    )
    return str(data[contract.token_field])


@dataclass(frozen=True)
class Settlement:
    """The settled token plus how it was reached.

    ``drift`` is set only when the prose line was absent or malformed and the envelope token
    stood on its own.
    """

    token: str
    prose_token: str | None
    envelope_token: str
    drift: str | None
    #: The envelope's optional reason (RLC-01 AC-2) — set only when the envelope carried a
    #: validated non-empty ``reason`` string next to an eligible token; ``None`` otherwise,
    #: never a fabricated substitute.
    reason: str | None = None


def settle(
    contract: EnvelopeContract,
    *,
    prose_text: str,
    envelope_text: str,
    role: str,
    task_id: str,
    attempt: int,
    require_reason: bool = False,
) -> Settlement:
    """Reconcile the strict prose parser and the authoritative JSON envelope.

    * envelope invalid -> ``contract.envelope_code`` (whatever the prose says);
    * envelope valid, prose valid, they agree -> that token;
    * envelope valid, prose valid, they disagree -> ``contract.mismatch_code`` (fail closed);
    * envelope valid, prose absent/malformed -> the envelope token, with ``drift`` set.
    """
    envelope_data = _parse_envelope_data(
        envelope_text, contract, role=role, task_id=task_id, attempt=attempt,
        require_reason=require_reason,
    )
    envelope_token = str(envelope_data[contract.token_field])
    reason = envelope_data.get(contract.reason_field) if contract.reason_field else None

    prose_token: str | None = None
    prose_error: str | None = None
    try:
        prose_token = parse_prose_token(prose_text, contract)
    except ReportProtocolError as exc:
        prose_error = str(exc)

    if prose_token is not None and prose_token != envelope_token:
        raise ReportProtocolError(
            f"{contract.disagreement_noun} disagreement for {task_id} attempt {attempt}: prose "
            f"reported {prose_token!r}, envelope reported {envelope_token!r} — not resolved "
            f"automatically",
            contract.mismatch_code,
        )

    drift = None
    if prose_token is None:
        drift = (
            f"prose {contract.drift_noun} line unusable ({prose_error}); envelope "
            f"{contract.drift_noun} {envelope_token!r} stands"
        )
    return Settlement(envelope_token, prose_token, envelope_token, drift, reason)
