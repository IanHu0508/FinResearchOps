"""Bounded forecast correction tests using synthetic financial inputs only."""

from copy import deepcopy
import unittest

from finauditgate.core.forward_revision import apply_forward_revision
from finauditgate.core.forward_scenarios import calculate_forward


def assumption(value, reason="Synthetic research assumption"):
    return {"value": value, "basis_type": "analyst_assumption",
            "reason": reason, "evidence_refs": ["S01"]}


def draft():
    values = {
        "revenue": 100, "operating_margin": 0.2, "net_nonoperating_income": 0,
        "effective_tax_rate": 0.25, "diluted_ordinary_shares": 10,
        "non_working_capital_adjustments": 3, "operating_asset_liability_cash_effect": -2,
        "cash_capex": 5, "fx_reporting_per_price_currency": 1,
        "exit_pe": None, "cash_dividend_per_traded_unit": None,
    }
    return {
        "business_model": "Synthetic operating company",
        "reporting_currency": "USD", "price_currency": "USD", "amount_unit": "million",
        "forecast_start": "2027-01-01", "forecast_end": "2027-12-31",
        "valuation_date": "2027-12-31", "earnings_basis": "trailing_at_valuation",
        "market_price_date": "2026-12-31", "market_price": assumption(10),
        "shares_per_traded_unit": assumption(1), "limitations": ["Synthetic hypotheses only"],
        "scenarios": [{
            "scenario_id": "F1", "name": "Synthetic case", "drivers": "No sales change",
            "noncontrolling_attribution": {"nature": "loss", "amount": assumption(2)},
            **{key: assumption(value) for key, value in values.items()},
            "valuation_reasoning": "No calibrated multiple", "evidence_that_changes_case": "New guidance",
        }],
    }


def change(field, expected, replacement, *, scenario_id="F1"):
    return {"scenario_id": scenario_id, "field": field, "expected_before": expected,
            "replacement": replacement, "correction_basis": "source_misread",
            "reason": "Correct the supplied synthetic input", "evidence_refs": ["S01"]}


class ForwardRevisionTests(unittest.TestCase):
    def test_minority_loss_to_profit_recomputes_parent_earnings_not_consolidated_cash(self):
        original = draft()
        changes = [change("noncontrolling_attribution", {"nature": "loss", "amount": 2},
                          {"nature": "profit", "amount": assumption(2, "Outside owners earned a profit")})]
        old, edits = deepcopy(original), deepcopy(changes)
        before = calculate_forward(original)["scenario_results"][0]
        result = apply_forward_revision(original, changes)
        after = result["effective_forward_calculations"]["scenario_results"][0]
        self.assertEqual((before["parent_net_income"], after["parent_net_income"]), (17, 13))
        self.assertEqual((before["eps_per_traded_unit"], after["eps_per_traded_unit"]), (1.7, 1.3))
        self.assertEqual(after["consolidated_net_income"], before["consolidated_net_income"])
        self.assertEqual(after["operating_cash_flow"], before["operating_cash_flow"])
        self.assertEqual(after["cash_after_capex_proxy"], 11)
        self.assertEqual(original, old)
        self.assertEqual(changes, edits)
        preserved = deepcopy(result["effective_forward_draft"])
        preserved["scenarios"][0]["noncontrolling_attribution"] = old["scenarios"][0]["noncontrolling_attribution"]
        self.assertEqual(preserved, old)
        applied = result["applied_changes"][0]
        self.assertEqual(applied["before"], old["scenarios"][0]["noncontrolling_attribution"])
        self.assertEqual(applied["after"], changes[0]["replacement"])
        self.assertEqual(applied["reason"], changes[0]["reason"])
        self.assertEqual(applied["evidence_refs"], ["S01"])

    def test_cash_adjustment_changes_cash_without_changing_equity_earnings(self):
        original = draft()
        before = calculate_forward(original)["scenario_results"][0]
        result = apply_forward_revision(original, [change("operating_asset_liability_cash_effect", -2, assumption(-7))])
        after = result["effective_forward_calculations"]["scenario_results"][0]
        self.assertEqual(after["operating_cash_flow"], before["operating_cash_flow"] - 5)
        self.assertEqual(after["cash_after_capex_proxy"], before["cash_after_capex_proxy"] - 5)
        for key in ("parent_net_income", "eps_per_traded_unit", "price_only_break_even_pe"):
            self.assertEqual(after[key], before[key])

    def test_no_changes_preserve_values_without_sharing_mutable_references(self):
        original = draft()
        result = apply_forward_revision(original, [])
        self.assertEqual(result["effective_forward_draft"], original)
        self.assertEqual(result["effective_forward_calculations"], calculate_forward(original))
        self.assertEqual(result["applied_changes"], [])
        result["effective_forward_draft"]["scenarios"][0]["revenue"]["value"] = 99
        self.assertEqual(original["scenarios"][0]["revenue"]["value"], 100)

    def test_noop_does_not_claim_a_correction_but_reason_only_change_is_retained(self):
        original = draft()
        current = original["scenarios"][0]["revenue"]
        self.assertEqual(apply_forward_revision(original, [change("revenue", 100, deepcopy(current))])["applied_changes"], [])
        replacement = assumption(100, "Same forecast, corrected historical interpretation")
        result = apply_forward_revision(original, [change("revenue", 100, replacement)])
        self.assertEqual(len(result["applied_changes"]), 1)
        self.assertEqual(result["effective_forward_calculations"], calculate_forward(original))

    def test_common_price_and_conversion_corrections_apply_to_each_existing_scenario(self):
        original = draft()
        second = deepcopy(original["scenarios"][0])
        second["scenario_id"] = "F2"
        original["scenarios"].append(second)
        result = apply_forward_revision(original, [
            change("market_price", 10, assumption(20), scenario_id=None),
            change("shares_per_traded_unit", 1, assumption(5), scenario_id=None),
        ])
        for row in result["effective_forward_calculations"]["scenario_results"]:
            self.assertEqual(row["eps_per_traded_unit"], 8.5)
            self.assertAlmostEqual(row["price_only_break_even_pe"], 20 / 8.5)
        self.assertEqual(result["effective_forward_draft"]["scenarios"], original["scenarios"])

    def test_unknown_amount_propagates_to_dependent_outputs_without_zero_filling(self):
        original = draft()
        correction = change("noncontrolling_attribution", {"nature": "loss", "amount": 2},
                            {"nature": "unknown", "amount": assumption(None, "No supported attribution")})
        result = apply_forward_revision(original, [correction])
        row = result["effective_forward_calculations"]["scenario_results"][0]
        self.assertIsNone(row["parent_net_income"])
        self.assertIsNone(row["eps_per_traded_unit"])
        self.assertEqual(row["operating_cash_flow"], 16)
        self.assertIsNone(result["effective_forward_draft"]["scenarios"][0]["noncontrolling_attribution"]["amount"]["value"])

    def test_unknown_scalar_and_expected_null_are_supported(self):
        original = draft()
        result = apply_forward_revision(original, [change("exit_pe", None, assumption(12))])
        self.assertEqual(result["effective_forward_calculations"]["scenario_results"][0]["exit_price_per_traded_unit"], 20.4)
        result = apply_forward_revision(original, [change("revenue", 100, assumption(None))])
        row = result["effective_forward_calculations"]["scenario_results"][0]
        self.assertIsNone(row["operating_profit"])
        self.assertIsNone(row["operating_cash_flow"])

    def test_duplicate_target_rejected_even_for_noop_or_sequential_expected_values(self):
        first = change("revenue", 100, assumption(90))
        for second in (deepcopy(first), change("revenue", 90, assumption(80))):
            with self.subTest(second=second):
                with self.assertRaisesRegex(ValueError, "duplicate correction target"):
                    apply_forward_revision(draft(), [first, second])
        noop = change("revenue", 100, assumption(100))
        with self.assertRaisesRegex(ValueError, "duplicate correction target"):
            apply_forward_revision(draft(), [noop, first])

    def test_stale_old_value_or_attribution_direction_rejected(self):
        invalid = [change("revenue", 99, assumption(90)),
                   change("noncontrolling_attribution", {"nature": "profit", "amount": 2},
                          {"nature": "profit", "amount": assumption(2)})]
        for correction in invalid:
            with self.subTest(field=correction["field"]):
                with self.assertRaisesRegex(ValueError, "does not match the original input"):
                    apply_forward_revision(draft(), [correction])

    def test_target_prices_dates_id_and_source_fields_are_not_patchable(self):
        for sid, field in (("F1", "scenario_id"), ("F1", "drivers"), ("F1", "target_price"),
                           (None, "valuation_date"), (None, "market_price_date"), (None, "source_bundle"),
                           (None, "rating"), (None, "revenue"), ("F1", "market_price")):
            with self.subTest(sid=sid, field=field):
                with self.assertRaisesRegex(ValueError, "target does not exist or is not allowed"):
                    apply_forward_revision(draft(), [change(field, None, assumption(1), scenario_id=sid)])

    def test_missing_or_invalid_scenario_target_is_rejected(self):
        for sid in ("F2", "F4", "", [], 1):
            with self.subTest(sid=sid):
                with self.assertRaisesRegex(ValueError, "scenario_id: target does not exist or is not allowed"):
                    apply_forward_revision(draft(), [change("revenue", 100, assumption(90), scenario_id=sid)])

    def test_missing_existing_input_cannot_be_created_by_a_patch(self):
        original = draft()
        del original["scenarios"][0]["revenue"]
        with self.assertRaisesRegex(ValueError, "field: target does not exist"):
            apply_forward_revision(original, [change("revenue", None, assumption(90))])

    def test_missing_replacement_value_is_rejected_instead_of_inventing_null(self):
        replacement = assumption(None)
        del replacement["value"]
        with self.assertRaisesRegex(ValueError, "replacement: expected exactly"):
            apply_forward_revision(draft(), [change("revenue", 100, replacement)])

    def test_complete_replacement_and_exact_change_shape_are_required(self):
        for field in ("scenario_id", "field", "expected_before", "replacement", "correction_basis", "reason", "evidence_refs"):
            correction = change("revenue", 100, assumption(90))
            del correction[field]
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, "expected exactly"):
                apply_forward_revision(draft(), [correction])
        correction = change("revenue", 100, assumption(90))
        correction["desired_rating"] = "Buy"
        with self.assertRaisesRegex(ValueError, "expected exactly"):
            apply_forward_revision(draft(), [correction])

    def test_boolean_nonfinite_and_string_old_values_cannot_match_numeric_inputs(self):
        for expected in (True, "0.2", float("nan"), float("inf")):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "finite JSON number"):
                apply_forward_revision(draft(), [change("operating_margin", expected, assumption(0.1))])

    def test_new_inputs_are_rechecked_by_the_calculator(self):
        for field, expected, value in (("diluted_ordinary_shares", 10, 0),
                                       ("cash_capex", 5, -1),
                                       ("fx_reporting_per_price_currency", 1, 0),
                                       ("revenue", 100, float("inf"))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                apply_forward_revision(draft(), [change(field, expected, assumption(value))])
        with self.assertRaises(ValueError):
            apply_forward_revision(draft(), [change("noncontrolling_attribution", {"nature": "loss", "amount": 2},
                                                    {"nature": "profit", "amount": assumption(-2)})])

    def test_failed_late_change_does_not_partially_mutate_caller_inputs(self):
        original = draft()
        before = deepcopy(original)
        changes = [change("revenue", 100, assumption(90)), change("cash_capex", 999, assumption(4))]
        saved = deepcopy(changes)
        with self.assertRaises(ValueError):
            apply_forward_revision(original, changes)
        self.assertEqual(original, before)
        self.assertEqual(changes, saved)

    def test_result_and_change_record_do_not_share_mutable_replacement_objects(self):
        correction = change("revenue", 100, assumption(90))
        result = apply_forward_revision(draft(), [correction])
        result["effective_forward_draft"]["scenarios"][0]["revenue"]["value"] = 80
        self.assertEqual(result["applied_changes"][0]["after"]["value"], 90)
        result["applied_changes"][0]["after"]["evidence_refs"].append("S02")
        self.assertEqual(correction["replacement"]["evidence_refs"], ["S01"])

    def test_invalid_revision_container_duplicate_scenario_and_empty_metadata_are_rejected(self):
        for changes in (None, {}, "correction"):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "expected a list"):
                apply_forward_revision(draft(), changes)
        original = draft()
        original["scenarios"].append(deepcopy(original["scenarios"][0]))
        with self.assertRaisesRegex(ValueError, "unique strings"):
            apply_forward_revision(original, [])
        for key, value in (("reason", " "), ("correction_basis", ""), ("evidence_refs", [""])):
            correction = change("revenue", 100, assumption(90))
            correction[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_forward_revision(draft(), [correction])


if __name__ == "__main__":
    unittest.main()
