"""Separate direction-only corrections from changes in assumed magnitudes."""

from copy import deepcopy
import unittest

from test_thesis_report_v11 import record
from finauditgate.core.revision_effects import revision_effects


class RevisionEffectsTest(unittest.TestCase):
    def test_direction_and_amount_changes_are_not_all_called_accounting_gain(self):
        value = record()
        changes = value["forward_revision"]["changes"]
        changes[0]["replacement"]["amount"]["value"] = 3
        original = deepcopy(value["forward_draft"])
        effects = revision_effects(original, changes)
        self.assertEqual(["direction_change", "amount_change"], [r["component"] for r in effects])
        for effect, delta in zip(effects, (-4, -1)):
            row = effect["results"][0]["metrics"]
            self.assertEqual(delta, row["parent_net_income"]["delta"])
            self.assertEqual(0, row["operating_cash_flow"]["delta"])
            self.assertEqual("analyst_assumption", effect["replacement_basis_type"])
        self.assertEqual(original, value["forward_draft"])

    def test_rationale_only_changes_have_zero_numeric_effect(self):
        value = record()
        edit = value["forward_revision"]["changes"][0]
        edit["replacement"]["nature"] = "loss"
        effects = revision_effects(value["forward_draft"], [edit])
        self.assertEqual("rationale_only", effects[0]["component"])
        self.assertEqual(0, effects[0]["results"][0]["metrics"]["eps_per_traded_unit"]["delta"])

    def test_unknowns_are_not_zero_and_no_change_is_empty(self):
        value = record()
        edit = value["forward_revision"]["changes"][0]
        edit["replacement"]["amount"]["value"] = None
        effect = revision_effects(value["forward_draft"], [edit])[-1]
        self.assertIsNone(effect["results"][0]["metrics"]["parent_net_income"]["delta"])
        self.assertEqual([], revision_effects(value["forward_draft"], []))
