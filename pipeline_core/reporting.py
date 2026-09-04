"""Minimal Markdown report assembly shared by the execution-engine artifact writers.

Since VP-02 the implementation lives in :mod:`feature_pipeline.reports.renderers` (the one
home for every compatibility renderer). This module re-exports it unchanged so existing
importers — ``pipeline_core.diagnostics`` and the diagnostic golden — are untouched: a stable
``##`` heading shape, a guaranteed-non-empty fenced body, and no trailing whitespace
(``git diff --check`` stays clean).
"""

from __future__ import annotations

from feature_pipeline.reports.renderers import Section, fenced, render_report

__all__ = ["Section", "fenced", "render_report"]
