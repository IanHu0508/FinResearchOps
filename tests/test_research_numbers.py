"""Synthetic checks of report numeric binding, not financial efficacy."""

from copy import deepcopy
import unittest

from finauditgate.application.research_numbers import METRIC_KEYS, METRIC_NAMES, render_research_block
from finauditgate.core.forward_scenarios import calculate_forward


def assumption(value):
    return {"value": value, "basis_type": "analyst_assumption",
            "reason": "合成研究假设", "evidence_refs": ["S01"]}


def inputs():
    values = {"revenue": 100.0, "operating_margin": .2, "net_nonoperating_income": 0.0,
              "effective_tax_rate": .25, "diluted_ordinary_shares": 10.0,
              "non_working_capital_adjustments": 3.0, "operating_asset_liability_cash_effect": -2.0,
              "cash_capex": 5.0, "fx_reporting_per_price_currency": 1.0,
              "exit_pe": 10.0, "cash_dividend_per_traded_unit": .5}
    draft = {"business_model": "合成工业企业", "reporting_currency": "USD", "price_currency": "USD",
             "amount_unit": "million", "forecast_start": "2027-01-01", "forecast_end": "2027-12-31",
             "valuation_date": "2027-12-31", "market_price_date": "2026-12-31",
             "earnings_basis": "trailing_at_valuation", "market_price": assumption(10.0),
             "shares_per_traded_unit": assumption(1.0), "limitations": [],
             "scenarios": [{"scenario_id": "F1", "name": "合成基准情景", "drivers": "订单与价格假设。",
                            "valuation_reasoning": "仅为合成条件计算。",
                            "evidence_that_changes_case": "订单变化。",
                            "noncontrolling_attribution": {"nature": "profit", "amount": assumption(2.0)},
                            **{key: assumption(value) for key, value in values.items()}}]}
    return draft, calculate_forward(draft)


def block(*keys):
    return {"text": "需要结合订单与经营条件判断。", "evidence_refs": ["S01"],
            "metrics": [{"scenario_id": "F1", "metric": key} for key in keys]}


class ResearchNumbersTest(unittest.TestCase):
    def test_all_allowlisted_metrics_render_with_fixed_labels_units_and_period(self):
        draft, calculations = inputs()
        report = "\n".join(render_research_block(block(*METRIC_KEYS), draft, calculations))
        for name in METRIC_NAMES.values():
            self.assertIn(name, report)
        for text in ("2027-01-01 至 2027-12-31", "起点行情日：2026-12-31", "F1 · 合成基准情景",
                     "| 营业收入 | 100 | 百万元 USD |", "| 经营利润率 | 20.00% | % |",
                     "| 合并净利润 | 15 | 百万元 USD |",
                     "| 归属于母公司股东的净利润 | 13 | 百万元 USD |",
                     "| 每交易单位摊薄盈利（EPS） | 1.3 | USD／交易单位（股票／ADS） |",
                     "| 经营活动现金流 | 16 | 百万元 USD |",
                     "| 扣除现金资本购买后的现金流代理 | 11 | 百万元 USD |",
                     "S01"):
            self.assertIn(text, report)

    def test_corrected_draft_and_calculations_replace_rejected_numeric_values(self):
        draft, original_calculations = inputs()
        original = deepcopy(draft)
        draft["scenarios"][0]["operating_margin"]["value"] = .1
        effective = calculate_forward(draft)
        report = "\n".join(render_research_block(block("operating_margin", "parent_net_income", "eps_per_traded_unit",
                                                    "operating_cash_flow", "cash_after_capex_proxy"), draft, effective))
        for text in ("| 经营利润率 | 10.00% |", "| 归属于母公司股东的净利润 | 5.5 |",
                     "| 每交易单位摊薄盈利（EPS） | 0.55 |", "| 经营活动现金流 | 8.5 |",
                     "| 扣除现金资本购买后的现金流代理 | 3.5 |"):
            self.assertIn(text, report)
        self.assertNotIn("| 每交易单位摊薄盈利（EPS） | 1.3 |", report)
        self.assertEqual(original["scenarios"][0]["operating_margin"]["value"], .2)
        self.assertEqual(original_calculations["scenario_results"][0]["parent_net_income"], 13)

    def test_minority_adjustment_cannot_relabel_consolidated_as_parent(self):
        draft, calculations = inputs()
        for nature, expected_parent, expected_effect in (("profit", "13", "-2"), ("loss", "17", "+2")):
            with self.subTest(nature=nature):
                draft["scenarios"][0]["noncontrolling_attribution"]["nature"] = nature
                calculations = calculate_forward(draft)
                report = "\n".join(render_research_block(block("consolidated_net_income", "parent_net_income",
                                                             "noncontrolling_attribution_effect"), draft, calculations))
                self.assertIn("| 合并净利润 | 15 |", report)
                self.assertIn(f"| 归属于母公司股东的净利润 | {expected_parent} |", report)
                self.assertIn(f"| 少数股东损益归属调节（负数扣减、正数加回） | {expected_effect} |", report)

    def test_missing_values_are_unknown_and_do_not_become_zero(self):
        draft, _ = inputs()
        draft["scenarios"][0]["noncontrolling_attribution"]["nature"] = "unknown"
        draft["scenarios"][0]["effective_tax_rate"]["value"] = None
        report = "\n".join(render_research_block(block("parent_net_income", "effective_tax_rate"),
                                                draft, calculate_forward(draft)))
        self.assertIn("| 归属于母公司股东的净利润 | 未能计算 | 百万元 USD |", report)
        self.assertIn("| 有效所得税率 | 未能计算 | % |", report)
        self.assertNotIn("| 0 |", report)

    def test_absent_calculation_result_never_uses_an_input_or_model_fallback(self):
        draft, calculations = inputs()
        calculations["scenario_results"] = []
        draft["scenarios"][0]["parent_net_income"] = {"value": 999.0}
        report = "\n".join(render_research_block(block("revenue", "parent_net_income"), draft, calculations))
        self.assertIn("| 营业收入 | 100 |", report)
        self.assertIn("| 归属于母公司股东的净利润 | 未能计算 |", report)
        self.assertNotIn("999", report)

    def test_input_metric_is_never_taken_from_a_calculation_field(self):
        draft, calculations = inputs()
        calculations["scenario_results"][0]["revenue"] = 999.0
        report = "\n".join(render_research_block(block("revenue"), draft, calculations))
        self.assertIn("| 营业收入 | 100 |", report)
        self.assertNotIn("999", report)

    def test_invalid_numeric_types_do_not_render_as_valid_numbers(self):
        for value in (True, "999", float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                draft, calculations = inputs()
                calculations["scenario_results"][0]["parent_net_income"] = value
                report = "\n".join(render_research_block(block("parent_net_income"), draft, calculations))
                self.assertIn("| 归属于母公司股东的净利润 | 未能计算 |", report)

    def test_unknown_scenario_or_metric_is_rejected(self):
        draft, calculations = inputs()
        for invalid in ({"scenario_id": "F9", "metric": "revenue"},
                        {"scenario_id": "F1", "metric": "fair_value"},
                        {"scenario_id": "F1", "metric": "__dict__"},
                        {"scenario_id": [], "metric": "revenue"},
                        {"scenario_id": "F1", "metric": []}):
            with self.subTest(ref=invalid):
                value = block("revenue")
                value["metrics"] = [invalid]
                with self.assertRaises(ValueError):
                    render_research_block(value, draft, calculations)

    def test_model_supplied_value_label_unit_or_missing_field_is_rejected(self):
        draft, calculations = inputs()
        for key in ("value", "label", "unit"):
            with self.subTest(key=key):
                value = block("parent_net_income")
                value["metrics"][0][key] = "999"
                with self.assertRaisesRegex(ValueError, "exactly scenario_id and metric"):
                    render_research_block(value, draft, calculations)
        value = block("revenue")
        del value["metrics"][0]["scenario_id"]
        with self.assertRaises(ValueError):
            render_research_block(value, draft, calculations)

    def test_malformed_block_or_evidence_references_are_rejected(self):
        draft, calculations = inputs()
        bad_blocks = [None, [], {"text": "incomplete"}, {**block(), "value": 999},
                      {**block(), "text": None}, {**block(), "metrics": "F1"}]
        bad_blocks += [{**block(), "evidence_refs": refs} for refs in ("S01", None, [None], [1], [""], [" \n "])]
        for value in bad_blocks:
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_research_block(value, draft, calculations)

    def test_duplicate_or_orphan_result_ids_do_not_silently_override_data(self):
        for target in ("draft", "calculations", "orphan"):
            with self.subTest(target=target):
                draft, calculations = inputs()
                if target == "draft":
                    draft["scenarios"].append(deepcopy(draft["scenarios"][0]))
                elif target == "calculations":
                    calculations["scenario_results"].append(deepcopy(calculations["scenario_results"][0]))
                else:
                    calculations["scenario_results"][0]["scenario_id"] = "F9"
                with self.assertRaises(ValueError):
                    render_research_block(block("revenue"), draft, calculations)

    def test_metrics_resolve_by_scenario_id_rather_than_list_position(self):
        draft, _ = inputs()
        alternative = deepcopy(draft["scenarios"][0])
        alternative.update(scenario_id="F2", name="不同订单条件")
        alternative["operating_margin"]["value"] = .1
        draft["scenarios"].append(alternative)
        calculations = calculate_forward(draft)
        calculations["scenario_results"].reverse()
        value = block("parent_net_income")
        value["metrics"].append({"scenario_id": "F2", "metric": "parent_net_income"})
        report = "\n".join(render_research_block(value, draft, calculations))
        self.assertIn("| F1 · 合成基准情景 | 归属于母公司股东的净利润 | 13 |", report)
        self.assertIn("| F2 · 不同订单条件 | 归属于母公司股东的净利润 | 5.5 |", report)

    def test_cash_price_multiple_and_return_limits_stay_explicit(self):
        report = "\n".join(render_research_block(block("cash_after_capex_proxy", "exit_price_per_traded_unit",
                                                    "price_only_break_even_pe", "return_ex_dividend"), *inputs()))
        for text in ("不等于股权自由现金流（FCFE）或可分配给股东的现金", "不是公允倍数或市场共识",
                     "不自动构成公允价值", "累计回报，非年化", "未计投资者税费",
                     "| 不含股息累计回报率（非年化） | +30.00% |"):
            self.assertIn(text, report)

    def test_prose_is_preserved_without_claiming_its_numbers_are_verified(self):
        draft, calculations = inputs()
        value = block("parent_net_income")
        value["text"] = "原模型历史陈述：999亿元。\n**未自动修订**，与预测指标分开。"
        report = render_research_block(value, draft, calculations)
        self.assertEqual(report[0], value["text"])
        self.assertIn("不验证自由文字中的历史事实、经济依据或预测准确性", "\n".join(report))
        self.assertIn("| 归属于母公司股东的净利润 | 13 |", "\n".join(report))

    def test_text_only_block_and_non_forecast_source_reference_are_supported(self):
        value = block()
        value["evidence_refs"] = ["SOURCE-OUTSIDE-FORECAST-ASSUMPTIONS"]
        report = "\n".join(render_research_block(value, *inputs()))
        self.assertIn(value["text"], report)
        self.assertIn("SOURCE-OUTSIDE-FORECAST-ASSUMPTIONS", report)
        self.assertNotIn("| 情景 | 指标 |", report)

    def test_rendering_does_not_mutate_inputs_and_escapes_table_cells(self):
        draft, calculations = inputs()
        draft["scenarios"][0]["name"] = "订单|价格\n<script>"
        value = block("revenue")
        value["evidence_refs"] = ["<source>|A"]
        before = deepcopy((value, draft, calculations))
        report = "\n".join(render_research_block(value, draft, calculations))
        self.assertEqual((value, draft, calculations), before)
        self.assertIn("订单&#124;价格<br>&lt;script&gt;", report)
        self.assertIn("&lt;source&gt;&#124;A", report)

    def test_schema_keys_and_renderer_names_cover_the_same_closed_set(self):
        self.assertEqual(set(METRIC_KEYS), set(METRIC_NAMES))
        self.assertEqual(len(METRIC_KEYS), 16)
        with self.assertRaises(TypeError):
            METRIC_NAMES["parent_net_income"] = "合并净利润"


if __name__ == "__main__":
    unittest.main()
