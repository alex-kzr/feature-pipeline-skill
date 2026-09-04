"""Byte-aware bounded capture for process output.

``pipeline_core.commands`` keeps the last N bytes of a command's stream under an outcome-based
budget, with an explicit dropped-bytes header so a cut is never silent. RS-01 lifts that helper
here unchanged so the process boundary owns "bounded capture and a diagnostic tail" in one
place; ``pipeline_core.commands`` re-exports it and still composes redaction ahead of it.

Standard library only.
"""

from __future__ import annotations

#: Emitted when even the header will not fit the budget.
TRUNCATION_MARKER = "[truncated]"


def tail_truncate(text: str, budget: int) -> str:
    """Keep the last ``budget`` bytes of UTF-8 ``text`` with an explicit dropped-bytes header."""
    encoded = (text or "").encode("utf-8", errors="replace")
    if len(encoded) <= budget:
        return text or ""
    if budget <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:budget]
    tail_budget = budget
    while True:
        dropped = len(encoded) - tail_budget
        header = f"[{dropped} bytes truncated; showing the last {tail_budget}]\n"
        header_size = len(header.encode("utf-8"))
        if header_size + tail_budget <= budget:
            return header + encoded[-tail_budget:].decode("utf-8", errors="ignore")
        resolved_tail = budget - header_size
        if resolved_tail <= 0:
            return TRUNCATION_MARKER
        tail_budget = min(tail_budget - 1, resolved_tail)


__all__ = ["TRUNCATION_MARKER", "tail_truncate"]
