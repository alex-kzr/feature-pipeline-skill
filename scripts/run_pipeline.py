#!/usr/bin/env python3
"""Portable feature-pipeline core runner entry point.

This is a thin shim: all behavior lives in :mod:`pipeline_core.runner_cli`. It accepts
explicit ``--project-root`` / ``--agents-root`` / ``--core-root`` anchors plus anchor-relative
logical paths, and never infers a project layout. Run ``run_pipeline.py --help`` for the flag
surface, or ``--dry-run`` for a deterministic, redacted plan.

Standard library only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Thin compatibility launcher (docs/adr/002): make the core importable whether or not the
# `feature-pipeline` package is installed. `parents[1]` carries the flat `pipeline_core` /
# `schemas` packages; `parents[1]/src` carries the migration-target `feature_pipeline`
# namespace the `schemas` shim now re-exports from. Both entries are dropped once the
# migration finishes and every caller runs from the installed distribution.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipeline_core.runner_cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
