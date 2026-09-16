"""REC-29: fail-closed narrowing for ``canonical_amendment_fields`` malformed input.

``canonical_amendment_fields`` accepts either a mapping or an object exposing the
amendable-contract attributes. Every field it reads is iterated (``allowed_scope``,
``out_of_scope``, ``verification_commands``, ``documentation_impact``); a non-iterable
value, or a verification-command entry that is neither a mapping, an object with
``cwd``/``argv``, nor a ``(cwd, argv)`` pair, must be rejected with a stable
:class:`AmendmentError` rather than an unnarrowed ``TypeError``/``ValueError`` surfacing
from the mypy-narrowed parsing helpers.
"""

from __future__ import annotations

import unittest

from pipeline_core.plan import AmendmentError, canonical_amendment_fields


class CanonicalAmendmentFieldsMalformedInputTests(unittest.TestCase):
    def test_non_iterable_scope_field_is_rejected_with_a_stable_code(self) -> None:
        contract = {
            "allowed_scope": 5,
            "out_of_scope": (),
            "verification_commands": (),
            "documentation_impact": (),
        }

        with self.assertRaises(AmendmentError) as denied:
            canonical_amendment_fields(contract)

        self.assertEqual(denied.exception.code, "amendment-malformed-field")

    def test_malformed_verification_command_entry_is_rejected_with_a_stable_code(self) -> None:
        contract = {
            "allowed_scope": (),
            "out_of_scope": (),
            "verification_commands": (5,),
            "documentation_impact": (),
        }

        with self.assertRaises(AmendmentError) as denied:
            canonical_amendment_fields(contract)

        self.assertEqual(denied.exception.code, "amendment-malformed-command")

    def test_well_formed_contract_still_normalizes_every_field(self) -> None:
        contract = {
            "allowed_scope": ("src/**",),
            "out_of_scope": (".pipeline/**",),
            "verification_commands": (
                {"cwd": ".", "argv": ["true"]},
                ("feature-pipeline-skill", ["uv", "run", "test"]),
            ),
            "max_repair_attempts": 2,
            "documentation_impact": ("docs/**",),
        }

        fields = canonical_amendment_fields(contract)

        self.assertEqual(fields["allowed_scope"], ["src/**"])
        self.assertEqual(fields["max_repair_attempts"], 2)
        self.assertEqual(
            fields["verification_commands"],
            [[".", ["true"]], ["feature-pipeline-skill", ["uv", "run", "test"]]],
        )


if __name__ == "__main__":
    unittest.main()
