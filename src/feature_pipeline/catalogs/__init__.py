"""Packaged, versioned catalog resources for the feature-pipeline core.

The JSON under ``task_kinds/<version>/catalog.json`` is the editable source of truth for
the task-kind contract; :mod:`feature_pipeline.domain.task_kinds` loads and validates it.
Resources only — this package imports nothing and is safe to ship in a wheel or sdist.
"""
