"""Typed findings and settled outcomes for the report protocol (VP-02).

A *finding* is one machine-readable statement about why a gate did or did not pass: a stable
``code``, a human ``message``, a ``severity``, and — when known — the ``location`` it applies
to, the acceptance ``criterion`` it maps to, and a ``remediation`` hint. A *settled outcome*
bundles a :class:`~feature_pipeline.reports.dispositions.Disposition` with the findings that
produced it.

These are plain value objects. The mechanical gates in
``feature_pipeline.application.report_gates`` build them; the compatibility renderers in
``feature_pipeline.reports.renderers`` turn them back into the historical Markdown.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .dispositions import Disposition


class Severity(StrEnum):
    """How much one finding matters. Ordered most to least severe by declaration."""

    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    """One machine-readable statement about a gate result."""

    code: str
    message: str
    severity: Severity = Severity.MAJOR
    location: str | None = None
    criterion: str | None = None
    remediation: str | None = None

    def as_record(self) -> dict[str, object]:
        """A JSON-safe dict; optional fields appear only when set."""
        record: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "severity": str(self.severity),
        }
        if self.location is not None:
            record["location"] = self.location
        if self.criterion is not None:
            record["criterion"] = self.criterion
        if self.remediation is not None:
            record["remediation"] = self.remediation
        return record


@dataclass(frozen=True)
class Outcome:
    """A disposition plus the findings that produced it.

    ``clean`` is true exactly when the disposition is
    :attr:`~feature_pipeline.reports.dispositions.Disposition.CLEAN`; a clean outcome carries
    no findings.
    """

    disposition: Disposition
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return self.disposition is Disposition.CLEAN

    def as_record(self) -> dict[str, object]:
        return {
            "disposition": str(self.disposition),
            "clean": self.clean,
            "findings": [finding.as_record() for finding in self.findings],
        }


def merge_findings(*groups: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """Order-preserving de-duplication of findings by ``(code, message, location)``."""
    seen: set[tuple[str, str, str | None]] = set()
    merged: list[Finding] = []
    for group in groups:
        for finding in group:
            key = (finding.code, finding.message, finding.location)
            if key in seen:
                continue
            seen.add(key)
            merged.append(finding)
    return tuple(merged)
