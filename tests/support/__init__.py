"""Reusable test support: shared fakes, builders, fixtures, and assertions.

QG-01 (``docs/plans/tasks/QG-01_test-suite-structure.md``) consolidates the fake
adapters/verifiers, task/run builders, filesystem fixtures, and assertion helpers that were
previously hand-duplicated across individual test modules (for example
``tests/test_repair.py`` and ``tests/test_execution.py`` each defined their own
``ScriptedExecutor``/``StubVerifier`` pair and near-identical ``TaskSpec``/``Run`` builders).

Submodules:

* :mod:`tests.support.contracts` — the structural launch-adapter contract every fake
  executor/verifier satisfies, plus a mixin that checks a fake against it.
* :mod:`tests.support.fakes` — the shared ``ScriptedExecutor``/``StubVerifier`` test doubles.
* :mod:`tests.support.builders` — ``TaskSpec``/``Run``/``TaskExecution`` construction helpers.
* :mod:`tests.support.fixtures` — filesystem fixture helpers (temp run roots, file writers).
* :mod:`tests.support.assertions` — small, repeated assertion helpers.

See ``tests/README.md`` for the full suite map and per-suite discovery commands.
"""

from __future__ import annotations
