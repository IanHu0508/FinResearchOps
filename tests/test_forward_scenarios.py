"""Synthetic economic bridge and failure-boundary tests."""

import copy
import json
import unittest
from decimal import Inexact, ROUND_DOWN, localcontext

from finauditgate.core.forward_scenarios import calculate_forward, valuation_date_for


def assumption(value):
    return {
        "value": value, "basis_type": "analyst_assumption",
        "reason": "Synthetic proposed input", "evidence_refs": [],
    }


def draft():
    values = {
        "revenue": 100, "operating_margin": 0.20,
        "net_nonoperating_income": 0, "effective_tax_rate": 0.25,
        "diluted_ordinary_shares": 10,
        "non_working_capital_adjustments": 0, "operating_asset_liability_cash_effect": 0,
        "cash_capex": 2, "fx_reporting_per_price_currency": 1,
        "exit_pe": 10, "cash_dividend_per_traded_unit": 0.5,
    }
    return {
        "reporting_currency": "USD", "price_currency": "USD",
        "amount_unit": "million", "forecast_start": "2027-01-01",
        "forecast_end": "2027-12-31", "valuation_date": "2027-12-31",
        "earnings_basis": "trailing_at_valuation",
        "market_price_date": "2026-12-31", "market_price": assumption(10),
        "shares_per_traded_unit": assumption(1), "limitations": [],
        "scenarios": [{
            "scenario_id": "F1", "name": "Illustrative case",
            "drivers": "Flat sales; proposed operating margin.",
            "noncontrolling_attribution": {"nature": "profit", "amount": assumption(0)},
            **{key: assumption(value) for key, value in values.items()},
            "valuation_reasoning": "Uncalibrated illustration only.",
            "evidence_that_changes_case": "A supported change in sales or margin.",
        }],
    }


class ForwardScenarioTests(unittest.TestCase):
    def calculate(self, value=None):
        return calculate_forward(value or draft())["scenario_results"][0]

    def test_annual_profit_bridge_and_cumulative_return(self):
        result = self.calculate()
        self.assertEqual(result["operating_profit"], 20)
        self.assertEqual(result["consolidated_net_income"], 15)
        self.assertEqual(result["parent_net_income"], 15)
        self.assertEqual(result["eps_per_traded_unit"], 1.5)
        self.assertEqual(result["exit_price_per_traded_unit"], 15)
        self.assertEqual(result["return_ex_dividend"], 0.5)
        self.assertEqual(result["return_with_dividend"], 0.55)
        self.assertEqual(result["missing_inputs"], [])

    def test_margin_change_has_transparent_profit_and_cash_effect(self):
        value = draft()
        value["scenarios"][0]["operating_margin"] = assumption(0.25)
        result = self.calculate(value)
        self.assertEqual(result["eps_per_traded_unit"], 1.875)
        self.assertEqual(result["operating_cash_flow"], 18.75)
        self.assertEqual(result["cash_after_capex_proxy"], 16.75)

    def test_noncash_loss_is_added_back_once_and_capex_subtracted_once(self):
        value = draft()
        row = value["scenarios"][0]
        row["effective_tax_rate"] = assumption(0)
        row["net_nonoperating_income"] = assumption(-6)
        row["non_working_capital_adjustments"] = assumption(6)
        row["operating_asset_liability_cash_effect"] = assumption(-3)
        result = self.calculate(value)
        self.assertEqual(result["consolidated_net_income"], 14)
        self.assertEqual(result["operating_cash_flow"], 17)
        self.assertEqual(result["cash_after_capex_proxy"], 15)

    def test_minority_attribution_does_not_replace_consolidated_cash_start(self):
        value = draft()
        value["scenarios"][0]["noncontrolling_attribution"] = {"nature": "profit", "amount": assumption(3)}
        result = self.calculate(value)
        self.assertEqual(result["parent_net_income"], 12)
        self.assertEqual(result["eps_per_traded_unit"], 1.2)
        self.assertEqual(result["operating_cash_flow"], 15)
        value["scenarios"][0]["noncontrolling_attribution"] = {"nature": "loss", "amount": assumption(3)}
        self.assertEqual(self.calculate(value)["parent_net_income"], 18)

    def test_minority_profit_400_subtracts_and_loss_400_adds_without_signed_amount(self):
        for nature, effect, parent in (("profit", -400, 1100), ("loss", 400, 1900)):
            with self.subTest(nature=nature):
                value = draft()
                row = value["scenarios"][0]
                row["revenue"] = assumption(10000)
                row["noncontrolling_attribution"] = {"nature": nature, "amount": assumption(400)}
                result = self.calculate(value)
                self.assertEqual(result["consolidated_net_income"], 1500)
                self.assertEqual(result["noncontrolling_attribution_effect"], effect)
                self.assertEqual(result["parent_net_income"], parent)
                self.assertEqual(result["eps_per_traded_unit"], parent / 10)
                self.assertEqual(result["operating_cash_flow"], 1500)
                self.assertEqual(result["cash_after_capex_proxy"], 1498)

    def test_unknown_minority_direction_preserves_cash_without_inventing_equity_earnings(self):
        for amount in (None, 0, 400):
            with self.subTest(amount=amount):
                value = draft()
                value["scenarios"][0]["noncontrolling_attribution"] = {"nature": "unknown", "amount": assumption(amount)}
                result = self.calculate(value)
                for key in ("noncontrolling_attribution_effect", "parent_net_income", "eps_per_traded_unit",
                            "exit_price_per_traded_unit", "price_only_break_even_pe",
                            "dividend_adjusted_break_even_pe", "return_ex_dividend", "return_with_dividend"):
                    self.assertIsNone(result[key])
                self.assertEqual(result["consolidated_net_income"], 15)
                self.assertEqual(result["operating_cash_flow"], 15)
                self.assertEqual(result["cash_after_capex_proxy"], 13)
                self.assertIn("noncontrolling_attribution.nature", result["missing_inputs"])

    def test_minority_missing_amount_does_not_imply_zero(self):
        for nature in ("profit", "loss"):
            with self.subTest(nature=nature):
                value = draft()
                value["scenarios"][0]["noncontrolling_attribution"] = {"nature": nature, "amount": assumption(None)}
                result = self.calculate(value)
                self.assertIsNone(result["noncontrolling_attribution_effect"])
                self.assertIsNone(result["parent_net_income"])
                self.assertEqual(result["operating_cash_flow"], 15)
                self.assertIn("noncontrolling_attribution.amount", result["missing_inputs"])

    def test_negative_minority_amount_is_not_an_alias_for_a_different_nature(self):
        for nature in ("profit", "loss", "unknown"):
            with self.subTest(nature=nature):
                value = draft()
                value["scenarios"][0]["noncontrolling_attribution"] = {"nature": nature, "amount": assumption(-400)}
                with self.assertRaisesRegex(ValueError, r"noncontrolling_attribution\.amount.value: must be nonnegative"):
                    calculate_forward(value)

    def test_minority_attribution_requires_explicit_nature_and_amount(self):
        invalid = (None, {}, {"nature": "profit"}, {"nature": "gain", "amount": assumption(400)},
                   {"nature": None, "amount": assumption(400)})
        for attribution in invalid:
            with self.subTest(attribution=attribution):
                value = draft()
                value["scenarios"][0]["noncontrolling_attribution"] = attribution
                with self.assertRaisesRegex(ValueError, "noncontrolling_attribution"):
                    calculate_forward(value)

    def test_legacy_signed_and_cash_input_names_are_not_accepted_as_replacements(self):
        replacements = (("noncontrolling_attribution", "noncontrolling_deduction"),
                        ("non_working_capital_adjustments", "noncash_adjustments"),
                        ("operating_asset_liability_cash_effect", "working_capital_cash_effect"))
        for current, legacy in replacements:
            with self.subTest(legacy=legacy):
                value = draft()
                row = value["scenarios"][0]
                del row[current]
                row[legacy] = assumption(0)
                with self.assertRaisesRegex(ValueError, current):
                    calculate_forward(value)

    def test_ads_conversion_and_fx_direction(self):
        value = draft()
        value["reporting_currency"] = "CNY"
        value["shares_per_traded_unit"] = assumption(5)
        value["scenarios"][0]["fx_reporting_per_price_currency"] = assumption(7.5)
        result = self.calculate(value)
        self.assertEqual(result["eps_per_traded_unit"], 1)
        self.assertEqual(result["exit_price_per_traded_unit"], 10)
        self.assertEqual(result["return_with_dividend"], 0.05)

    def test_market_price_changes_return_without_changing_forecast(self):
        before = self.calculate()
        value = draft()
        value["market_price"] = assumption(20)
        after = self.calculate(value)
        for key in ("parent_net_income", "operating_cash_flow", "cash_after_capex_proxy",
                    "eps_per_traded_unit", "exit_price_per_traded_unit"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["return_ex_dividend"], -0.25)

    def test_break_even_multiples_do_not_require_an_exit_pe_assumption(self):
        value = draft()
        value["scenarios"][0]["exit_pe"] = assumption(None)
        result = self.calculate(value)
        self.assertEqual(result["eps_per_traded_unit"], 1.5)
        self.assertIsNone(result["exit_price_per_traded_unit"])
        self.assertIsNone(result["return_ex_dividend"])
        self.assertAlmostEqual(result["price_only_break_even_pe"], 6.666666666666667)
        self.assertAlmostEqual(result["dividend_adjusted_break_even_pe"], 6.333333333333333)
        self.assertTrue(any("不代表公允倍数" in text for text in calculate_forward(value)["limitations"]))

    def test_missing_dividend_does_not_imply_zero_for_break_even_multiple(self):
        value = draft()
        value["scenarios"][0]["cash_dividend_per_traded_unit"] = assumption(None)
        result = self.calculate(value)
        self.assertAlmostEqual(result["price_only_break_even_pe"], 6.666666666666667)
        self.assertIsNone(result["dividend_adjusted_break_even_pe"])

    def test_break_even_multiples_require_positive_annual_eps_and_dated_holding_period(self):
        changes = (
            {"market_price_date": None},
            {"market_price_date": "2027-12-31"},
            {"market_price": assumption(None)},
            {"forecast_start": "2027-07-01"},
        )
        for change in changes:
            with self.subTest(change=change):
                value = draft()
                value.update(change)
                result = self.calculate(value)
                self.assertIsNone(result["price_only_break_even_pe"])
                self.assertIsNone(result["dividend_adjusted_break_even_pe"])
        for margin in (None, 0, -0.1):
            with self.subTest(margin=margin):
                value = draft()
                value["scenarios"][0]["operating_margin"] = assumption(margin)
                result = self.calculate(value)
                self.assertIsNone(result["price_only_break_even_pe"])
                self.assertIsNone(result["dividend_adjusted_break_even_pe"])

    def test_dividend_above_cost_retains_algebra_without_negative_pe_valuation_claim(self):
        value = draft()
        value["scenarios"][0]["cash_dividend_per_traded_unit"] = assumption(15)
        result = self.calculate(value)
        self.assertAlmostEqual(result["price_only_break_even_pe"], 6.666666666666667)
        self.assertAlmostEqual(result["dividend_adjusted_break_even_pe"], -3.333333333333333)
        self.assertTrue(any("不能理解为负市盈率估值" in text for text in result["limitations"]))

    def test_missing_pe_or_nonpositive_earnings_keep_useful_forecast(self):
        for pe in (None, 0, -10):
            with self.subTest(pe=pe):
                value = draft()
                value["scenarios"][0]["exit_pe"] = assumption(pe)
                result = self.calculate(value)
                self.assertEqual(result["parent_net_income"], 15)
                self.assertIsNone(result["exit_price_per_traded_unit"])
                self.assertIsNone(result["return_with_dividend"])
                self.assertEqual("exit_pe" in result["missing_inputs"], pe is None)
        for margin in (0, -0.10):
            with self.subTest(margin=margin):
                value = draft()
                value["scenarios"][0]["operating_margin"] = assumption(margin)
                result = self.calculate(value)
                self.assertLessEqual(result["parent_net_income"], 0)
                self.assertIsNone(result["exit_price_per_traded_unit"])

    def test_partial_period_is_not_silently_annualized(self):
        value = draft()
        value["forecast_end"] = "2027-06-30"
        value["valuation_date"] = "2027-06-30"
        result = calculate_forward(value)
        self.assertEqual(result["forecast_days"], 181)
        self.assertFalse(result["annual_earnings_period"])
        self.assertEqual(result["scenario_results"][0]["parent_net_income"], 15)
        self.assertIsNone(result["scenario_results"][0]["exit_price_per_traded_unit"])
        self.assertIn("不使用年度市盈率", result["scenario_results"][0]["limitations"][0])

    def test_missing_cash_inputs_do_not_erase_earnings_or_imply_zero(self):
        value = draft()
        value["scenarios"][0]["non_working_capital_adjustments"] = assumption(None)
        result = self.calculate(value)
        self.assertEqual(result["eps_per_traded_unit"], 1.5)
        self.assertEqual(result["exit_price_per_traded_unit"], 15)
        self.assertIsNone(result["operating_cash_flow"])
        self.assertIsNone(result["cash_after_capex_proxy"])
        self.assertEqual(result["missing_inputs"], ["non_working_capital_adjustments"])

    def test_missing_dividend_or_price_only_blocks_dependent_return(self):
        value = draft()
        value["scenarios"][0]["cash_dividend_per_traded_unit"] = assumption(None)
        result = self.calculate(value)
        self.assertEqual(result["return_ex_dividend"], 0.5)
        self.assertIsNone(result["return_with_dividend"])
        value["market_price"] = assumption(None)
        result = self.calculate(value)
        self.assertEqual(result["exit_price_per_traded_unit"], 15)
        self.assertIsNone(result["return_ex_dividend"])

    def test_input_remains_unchanged_and_outputs_are_json_finite(self):
        value = draft()
        original = copy.deepcopy(value)
        result = calculate_forward(value)
        self.assertEqual(value, original)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        result["limitations"].append("Caller change")
        self.assertEqual(value, original)

    def test_missing_quote_date_blocks_return_but_keeps_forecast(self):
        value = draft()
        value["market_price_date"] = None
        result = self.calculate(value)
        self.assertEqual(result["eps_per_traded_unit"], 1.5)
        self.assertEqual(result["exit_price_per_traded_unit"], 15)
        self.assertIsNone(result["return_ex_dividend"])
        self.assertIsNone(result["return_with_dividend"])
        self.assertIn("market_price_date", result["missing_inputs"])
        self.assertIn("缺少报价日期", result["limitations"][0])
        value["market_price"] = assumption(None)
        self.assertEqual(self.calculate(value)["parent_net_income"], 15)

    def test_quote_after_valuation_date_is_rejected(self):
        value = draft()
        value["market_price_date"] = "2028-01-01"
        with self.assertRaisesRegex(ValueError, "market_price_date"):
            calculate_forward(value)

    def test_zero_holding_period_keeps_price_but_has_no_investment_return(self):
        value = draft()
        value["market_price_date"] = value["valuation_date"]
        result = self.calculate(value)
        self.assertEqual(result["eps_per_traded_unit"], 1.5)
        self.assertEqual(result["exit_price_per_traded_unit"], 15)
        self.assertIsNone(result["return_ex_dividend"])
        self.assertIsNone(result["return_with_dividend"])
        self.assertIn("持有期为零", result["limitations"][0])

    def test_month_addition_clamps_end_of_month_and_leap_year(self):
        examples = (
            ("2027-01-31", 1, "2027-02-28"),
            ("2028-01-31", 1, "2028-02-29"),
            ("2028-02-29", 12, "2029-02-28"),
            ("2027-01-31", 2, "2027-03-31"),
            ("2027-12-31", 2, "2028-02-29"),
            ("2026-09-11", 18, "2028-03-11"),
        )
        for as_of, months, expected in examples:
            with self.subTest(as_of=as_of, months=months):
                self.assertEqual(valuation_date_for(as_of, months), expected)

    def test_month_addition_rejects_invalid_dates_horizons_and_overflow(self):
        for as_of in ("2027-02-29", "2027-1-01", None):
            with self.subTest(as_of=as_of):
                with self.assertRaisesRegex(ValueError, "as_of"):
                    valuation_date_for(as_of, 12)
        for months in (0, -1, 12.0, "12", True, None):
            with self.subTest(months=months):
                with self.assertRaisesRegex(ValueError, "horizon_months"):
                    valuation_date_for("2027-01-01", months)
        with self.assertRaisesRegex(ValueError, "horizon_months"):
            valuation_date_for("9999-12-31", 1)

    def test_trailing_denominator_must_end_at_valuation_date(self):
        value = draft()
        self.assertEqual(calculate_forward(value)["earnings_basis"], "trailing_at_valuation")
        value["valuation_date"] = "2028-01-01"
        with self.assertRaisesRegex(ValueError, "trailing_at_valuation"):
            calculate_forward(value)

    def test_forward_denominator_begins_at_valuation_or_following_day(self):
        for start, end in (("2027-12-31", "2028-12-30"), ("2028-01-01", "2028-12-31")):
            with self.subTest(start=start):
                value = draft()
                value["earnings_basis"] = "forward_from_valuation"
                value["forecast_start"] = start
                value["forecast_end"] = end
                result = calculate_forward(value)
                self.assertEqual(result["earnings_basis"], "forward_from_valuation")
                self.assertEqual(result["scenario_results"][0]["exit_price_per_traded_unit"], 15)
        for start in ("2027-12-30", "2028-01-02"):
            with self.subTest(start=start):
                value = draft()
                value["earnings_basis"] = "forward_from_valuation"
                value["forecast_start"] = start
                value["forecast_end"] = "2028-12-31"
                with self.assertRaisesRegex(ValueError, "forward_from_valuation"):
                    calculate_forward(value)

    def test_fiscal_year_denominator_is_disclosed_without_fake_date_alignment(self):
        value = draft()
        value["earnings_basis"] = "fiscal_year"
        value["valuation_date"] = "2027-09-30"
        result = calculate_forward(value)
        self.assertEqual(result["earnings_basis"], "fiscal_year")
        self.assertEqual(result["scenario_results"][0]["exit_price_per_traded_unit"], 15)
        self.assertIn("比较市盈率或价格时需核对盈利期间口径", result["limitations"][0])
        value["forecast_end"] = "2027-06-30"
        self.assertIsNone(self.calculate(value)["exit_price_per_traded_unit"])

    def test_missing_or_unknown_earnings_basis_is_rejected(self):
        value = draft()
        del value["earnings_basis"]
        with self.assertRaisesRegex(ValueError, "earnings_basis"):
            calculate_forward(value)
        for basis in ("annual", None, []):
            with self.subTest(basis=basis):
                value["earnings_basis"] = basis
                with self.assertRaisesRegex(ValueError, "earnings_basis"):
                    calculate_forward(value)

    def test_empty_scenarios_support_qualitative_research_without_invented_model(self):
        value = draft()
        value["scenarios"] = []
        result = calculate_forward(value)
        self.assertEqual(result["scenario_results"], [])
        self.assertIn("未提供适用的一般企业盈利情景", result["limitations"][0])

    def test_nonfinite_boolean_and_string_numbers_are_rejected(self):
        for number in (float("nan"), float("inf"), -float("inf"), True, "100"):
            with self.subTest(number=number):
                value = draft()
                value["scenarios"][0]["revenue"] = assumption(number)
                with self.assertRaisesRegex(ValueError, r"scenarios\[0\]\.revenue.value"):
                    calculate_forward(value)

    def test_nonpositive_price_fx_and_share_inputs_are_rejected(self):
        for field in ("market_price", "shares_per_traded_unit",
                      "diluted_ordinary_shares", "fx_reporting_per_price_currency"):
            for number in (0, -1):
                with self.subTest(field=field, number=number):
                    value = draft()
                    target = value if field in value else value["scenarios"][0]
                    target[field] = assumption(number)
                    with self.assertRaisesRegex(ValueError, field):
                        calculate_forward(value)

    def test_invalid_dates_and_reversed_forecast_are_rejected(self):
        for date_value in ("2027-02-30", "20270101", "2027-01-01T00:00:00", None):
            with self.subTest(date=date_value):
                value = draft()
                value["forecast_start"] = date_value
                with self.assertRaisesRegex(ValueError, "forecast_start"):
                    calculate_forward(value)
        value = draft()
        value["forecast_end"] = "2026-12-31"
        with self.assertRaisesRegex(ValueError, "forecast_end"):
            calculate_forward(value)

    def test_contract_rejects_bad_basis_missing_field_and_duplicate_ids(self):
        value = draft()
        value["market_price"]["basis_type"] = "validated"
        with self.assertRaisesRegex(ValueError, "basis_type"):
            calculate_forward(value)
        value = draft()
        del value["scenarios"][0]["cash_capex"]
        with self.assertRaisesRegex(ValueError, "cash_capex"):
            calculate_forward(value)
        value = draft()
        value["scenarios"].append(copy.deepcopy(value["scenarios"][0]))
        with self.assertRaisesRegex(ValueError, "scenario_id"):
            calculate_forward(value)

    def test_decimal_result_is_independent_of_callers_context(self):
        value = draft()
        value["scenarios"][0]["diluted_ordinary_shares"] = assumption(7)
        before = calculate_forward(value)
        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            self.assertEqual(calculate_forward(value), before)

    def test_result_overflow_is_not_silently_serialized_as_infinity(self):
        value = draft()
        value["scenarios"][0]["revenue"] = assumption(1e308)
        value["scenarios"][0]["operating_margin"] = assumption(100)
        with self.assertRaisesRegex(ValueError, "finite JSON number"):
            calculate_forward(value)


if __name__ == "__main__":
    unittest.main()
