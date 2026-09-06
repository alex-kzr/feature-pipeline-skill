"""Repository-owned CI contract for the portable feature-pipeline core.

This package is **not** part of the installable runtime (`feature_pipeline` /
`pipeline_core`) and is never imported by `scripts/run_pipeline.py`. It holds the
machine-readable gate manifest (`gates.toml`) and its standard-library loader
(`contract.py`) so workflow YAML and `tests/README.md` stop duplicating command
lists. See `docs/plans/tasks/UGA-02_core-gate-manifest.md`.
"""
