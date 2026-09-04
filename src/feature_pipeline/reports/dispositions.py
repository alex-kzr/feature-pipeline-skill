"""Standardized dispositions for a settled report (VP-02).

Every failed gate in the pipeline ultimately resolves to one of a small, closed set of
*dispositions*. Before VP-02 the vocabulary was implicit and scattered — ``pipeline_core``
spoke of ``BLOCKED`` for an absent toolchain, ``FAIL`` for a timeout or a non-zero exit, and
carried ``status-envelope-mismatch`` / ``unparseable-*`` protocol reasons as free strings.
This module names the four failure dispositions the task requires — **timeout**, **protocol**,
**infrastructure-blocker**, **product-failure** — plus **clean**, and gives one deterministic
classifier from a command exit code.

* ``infrastructure-blocker`` is *external*: the change under test cannot fix it, so the repair
  loop is never charged for it and the task goes to ``blocked``.
* ``timeout``, ``protocol``, and ``product-failure`` are the task's own: they drive
  ``verification_failed`` and a bounded repair.

Standard library only.
"""

from __future__ import annotations

from enum import StrEnum


class Disposition(StrEnum):
    """The closed set of settled report dispositions."""

    CLEAN = "clean"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    INFRASTRUCTURE_BLOCKER = "infrastructure-blocker"
    PRODUCT_FAILURE = "product-failure"


#: Dispositions the change under test cannot be blamed for.
_EXTERNAL: frozenset[Disposition] = frozenset({Disposition.INFRASTRUCTURE_BLOCKER})

#: The historical ``pipeline_core.commands`` disposition tokens, for the compatibility bridge.
_VERDICT_FOR: dict[Disposition, str] = {
    Disposition.CLEAN: "PASS",
    Disposition.TIMEOUT: "FAIL",
    Disposition.PROTOCOL: "FAIL",
    Disposition.PRODUCT_FAILURE: "FAIL",
    Disposition.INFRASTRUCTURE_BLOCKER: "BLOCKED",
}


def is_external_blocker(disposition: Disposition) -> bool:
    """True when the disposition is outside the task's control (-> ``blocked``, no repair)."""
    return disposition in _EXTERNAL


def to_verdict(disposition: Disposition) -> str:
    """Map a disposition to the historical ``PASS`` / ``FAIL`` / ``BLOCKED`` token."""
    return _VERDICT_FOR[disposition]


def classify_command(
    exit_code: int | str,
    *,
    timed_out: bool = False,
    unavailable: bool = False,
    launch_failed: bool = False,
) -> Disposition:
    """Deterministically classify one launched command.

    Mirrors ``pipeline_core.commands.classify_outcome`` byte-for-byte on the disposition:
    a clean exit is :attr:`Disposition.CLEAN`; an absent program or a failed launch is
    :attr:`Disposition.INFRASTRUCTURE_BLOCKER`; a timeout is :attr:`Disposition.TIMEOUT`;
    any other non-zero exit is :attr:`Disposition.PRODUCT_FAILURE`.
    """
    if exit_code == 0 and not (timed_out or unavailable or launch_failed):
        return Disposition.CLEAN
    if unavailable or launch_failed:
        return Disposition.INFRASTRUCTURE_BLOCKER
    if timed_out:
        return Disposition.TIMEOUT
    return Disposition.PRODUCT_FAILURE
