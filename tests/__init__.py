"""Test package for the portable feature-pipeline core.

Importing ``feature_pipeline.application`` here, before any test module has a chance to
import ``pipeline_core.verification`` directly (as ``tests.support.fakes`` does), pins the
process-wide module import order. ``feature_pipeline/application/__init__.py`` imports
``.task_engine``, which imports ``pipeline_core.verification``; if that happens first,
``pipeline_core.verification`` finishes initializing on its own before anything re-enters it.
If instead ``pipeline_core.verification`` is the first of the two touched (e.g. directly from a
test support module), its own import of ``feature_pipeline.application.diagnostic_service``
triggers ``feature_pipeline/application/__init__.py``, which imports ``.task_engine``, which
imports ``pipeline_core.verification`` while it is still mid-initialization — a
``VerificationOutcome``-not-yet-defined circular-import failure. Forcing the safe order once,
here, for the whole test run avoids depending on unittest's directory-discovery order (QG-01).
"""

import feature_pipeline.application  # noqa: F401  (import order fix, see docstring)
