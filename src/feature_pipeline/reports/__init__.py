"""Report protocol for the portable feature-pipeline core (VP-02).

One parameterized settlement parser (:mod:`.settlement`), typed findings and outcomes
(:mod:`.findings`), the closed disposition vocabulary (:mod:`.dispositions`), and the
compatibility Markdown renderers (:mod:`.renderers`). ``pipeline_core.reports`` and
``pipeline_core.reporting`` delegate here; the deterministic mechanical gates that consume
these types live in :mod:`feature_pipeline.application.report_gates`.
"""

from __future__ import annotations

from .dispositions import (
    Disposition,
    classify_command,
    is_external_blocker,
    to_verdict,
)
from .findings import Finding, Outcome, Severity, merge_findings
from .renderers import (
    Section,
    fenced,
    render_findings_block,
    render_report,
)
from .settlement import (
    CONTRACTS,
    STATUS_CONTRACT,
    VERDICT_CONTRACT,
    EnvelopeContract,
    ReportProtocolError,
    Settlement,
    parse_envelope,
    parse_prose_token,
    settle,
)

__all__ = [
    "CONTRACTS",
    "STATUS_CONTRACT",
    "VERDICT_CONTRACT",
    "EnvelopeContract",
    "ReportProtocolError",
    "Settlement",
    "parse_envelope",
    "parse_prose_token",
    "settle",
    "Disposition",
    "classify_command",
    "is_external_blocker",
    "to_verdict",
    "Finding",
    "Outcome",
    "Severity",
    "merge_findings",
    "Section",
    "fenced",
    "render_findings_block",
    "render_report",
]
