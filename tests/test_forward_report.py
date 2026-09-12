from copy import deepcopy
import unittest

from finauditgate.application.forward_report import render_forward


def assumption(value, *, reason="经营假设，尚需跟踪", refs=None):
    return {"value": value, "basis_type": "analyst_assumption", "reason": reason,
            "evidence_refs": ["S01"] if refs is None else refs}


def inputs():
    scenario = {"scenario_id": "F1", "name": "经营稳定", "drivers": "收入来自销量与价格。\n保持原分析换行。",
                "valuation_reasoning": "以现金转化及盈利持续性解释条件倍数。",
                "evidence_that_changes_case": "订单下滑时下调销量假设。"}
    values = {"revenue": 1000, "operating_margin": .2, "net_nonoperating_income": -10,
              "effective_tax_rate": .25,
              "diluted_ordinary_shares": 100, "non_working_capital_adjustments": 20,
              "operating_asset_liability_cash_effect": -10, "cash_capex": 30,
              "fx_reporting_per_price_currency": 7, "exit_pe": 15,
              "cash_dividend_per_traded_unit": .5}
    scenario.update({key: assumption(value) for key, value in values.items()})
    scenario["noncontrolling_attribution"] = {"nature": "profit", "amount": assumption(2.5)}
    draft = {"business_model": "合成工业企业，以销售产品和收款创造利润及现金。",
             "reporting_currency": "CNY", "price_currency": "USD", "amount_unit": "million",
             "forecast_start": "2026-01-01", "forecast_end": "2026-12-31",
             "earnings_basis": "trailing_at_valuation",
             "valuation_date": "2026-12-31", "market_price_date": "2026-01-02",
             "market_price": assumption(12), "shares_per_traded_unit": assumption(5),
             "scenarios": [scenario], "limitations": ["销量仍取决于订单兑现。"]}
    calculated = {"method": "earnings_to_cash_and_exit_pe/v1", "scenario_results": [
        {"scenario_id": "F1", "name": "经营稳定", "operating_profit": 200,
         "pretax_income": 190, "consolidated_net_income": 142.5, "parent_net_income": 140,
         "noncontrolling_attribution_effect": -2.5,
         "eps_per_traded_unit": 1, "operating_cash_flow": 152.5, "cash_after_capex_proxy": 122.5,
         "exit_price_per_traded_unit": 15, "return_ex_dividend": .25,
         "return_with_dividend": 3.5 / 12, "price_only_break_even_pe": 12,
         "dividend_adjusted_break_even_pe": 11.5, "missing_inputs": [], "limitations": []}],
         "limitations": ["现金代理不反映融资变动。"]}
    assessment = {"economic_conclusion": "在销量实现条件下，当前价格有正回报空间。",
                  "confidence_and_limits": "订单假设仍不确定。",
                  "scenario_assessments": [{"scenario_id": "F1", "disposition": "conditional",
                      "reason": "订单兑现后才采用。", "what_changes_the_view": "回款恶化则下调预期。"}]}
    return draft, calculated, assessment


class ForwardReportTest(unittest.TestCase):
    def test_report_connects_units_assumptions_calculation_and_adoption(self):
        draft, calculations, assessment = inputs()
        report = "\n".join(render_forward(draft, calculations, assessment))
        for text in ("百万元 CNY", "百万普通股", "股票／ADS", "EPS（USD／交易单位）",
                     "条件价格（USD／交易单位）", "累计回报，非年化", "+25.00%", "+29.17%",
                     "5 普通股／交易单位", "7 CNY／USD", "有条件采用", "订单兑现后才采用。",
                     "已扣现金资本购买", "不等同于 FCFE", "不自动构成公允价值", "S01",
                     "税前利润 190.00", "合并净利润 142.50", "归母净利润 140.00"):
            self.assertIn(text, report)
        self.assertLess(report.index("| 情景与采纳"), report.index("### 共同口径与输入依据"))
        self.assertIn(draft["scenarios"][0]["drivers"], report)
        for parameter in draft["scenarios"][0].values():
            if isinstance(parameter, dict):
                parameter = parameter.get("amount", parameter)
                self.assertIn(parameter["reason"], report)

    def test_noncontrolling_profit_loss_and_unknown_keep_attribution_direction_explicit(self):
        for nature, effect, parent, label in (
            ("profit", -2.5, 140, "少数股东盈利归属额（从合并净利扣除）"),
            ("loss", 2.5, 145, "少数股东亏损归属额（加回）"),
            ("unknown", None, None, "少数股东归属额（盈利或亏损性质未知）"),
        ):
            with self.subTest(nature=nature):
                draft, calculations, assessment = inputs()
                amount = assumption(None if nature == "unknown" else 2.5,
                                    reason="归属额需要单独说明", refs=["S09"])
                draft["scenarios"][0]["noncontrolling_attribution"] = {"nature": nature, "amount": amount}
                calculations["scenario_results"][0].update(noncontrolling_attribution_effect=effect,
                                                           parent_net_income=parent)
                report = "\n".join(render_forward(draft, calculations, assessment))
                parameter_row = next(line for line in report.splitlines() if line.startswith("| " + label))
                self.assertIn("归属额需要单独说明", parameter_row)
                self.assertIn("S09", parameter_row)
                self.assertIn("模型标注：研究假设", parameter_row)
                self.assertIn("未能计算" if nature == "unknown" else "2.5 百万元 CNY", parameter_row)
                expected_effect = {"profit": "-2.50", "loss": "+2.50", "unknown": "未能计算"}[nature]
                self.assertIn("少数股东归属调节 " + expected_effect, report)
                self.assertNotIn("少数股东损益扣减", report)

    def test_cash_adjustment_labels_do_not_claim_strict_working_capital_or_only_noncash(self):
        report = "\n".join(render_forward(*inputs()))
        self.assertIn("非营运资金调整（含非现金等）", report)
        self.assertIn("经营性资产负债现金影响（非严格NWC或余额变化）", report)
        self.assertNotIn("非现金调整净额", report)
        self.assertNotIn("营运资本现金影响（流入为正）", report)

    def test_missing_price_keeps_operating_forecast_without_fake_zero_return(self):
        draft, calculations, assessment = inputs()
        draft["market_price"]["value"] = None
        draft["market_price_date"] = None
        result = calculations["scenario_results"][0]
        result.update(return_ex_dividend=None, return_with_dividend=None, price_only_break_even_pe=None,
                      dividend_adjusted_break_even_pe=None, missing_inputs=["market_price"])
        report = "\n".join(render_forward(draft, calculations, assessment))
        pricing_row = next(line for line in report.splitlines() if line.startswith("| F1") and "15.00 | 15.00" in line)
        self.assertIn("| 未能计算 | 未能计算 |", pricing_row)
        self.assertNotIn("0.00%", pricing_row)
        self.assertIn("归母净利润 140.00", report)
        self.assertIn("起点行情日：未提供", report)
        self.assertIn("尚缺计算输入：起点市场价格", report)

    def test_break_even_multiples_remain_visible_when_no_exit_multiple_is_proposed(self):
        draft, calculations, assessment = inputs()
        draft["scenarios"][0]["exit_pe"]["value"] = None
        result = calculations["scenario_results"][0]
        result.update(exit_price_per_traded_unit=None, return_ex_dividend=None,
                      return_with_dividend=None, missing_inputs=["exit_pe"])
        report = "\n".join(render_forward(draft, calculations, assessment))
        self.assertIn("维持起点股价所需PE（倍）", report)
        self.assertIn("含股息打平所需PE（倍）", report)
        self.assertIn("| 未能计算 | 未能计算 | 未能计算 | 未能计算 | 12.00 | 11.50 |", report)
        self.assertIn("给定盈利与股息假设下的条件反推", report)
        self.assertIn("不是推荐或公允倍数，不能证明市场共识或股价便宜", report)

    def test_unknown_dividend_break_even_does_not_erase_price_only_break_even(self):
        draft, calculations, assessment = inputs()
        draft["scenarios"][0]["cash_dividend_per_traded_unit"]["value"] = None
        result = calculations["scenario_results"][0]
        result.update(return_with_dividend=None, dividend_adjusted_break_even_pe=None,
                      missing_inputs=["cash_dividend_per_traded_unit"])
        report = "\n".join(render_forward(draft, calculations, assessment))
        self.assertIn("| +25.00% | 未能计算 | 12.00 | 未能计算 |", report)

    def test_reported_label_is_a_model_claim_and_not_a_fact_certification(self):
        draft, calculations, assessment = inputs()
        draft["market_price"]["basis_type"] = "reported"
        scenario = draft["scenarios"][0]
        scenario["revenue"]["basis_type"] = "reported"
        scenario["operating_margin"]["basis_type"] = "company_guidance"
        scenario["exit_pe"]["basis_type"] = "reference_comparison"
        report = "\n".join(render_forward(draft, calculations, assessment))
        self.assertIn("| 模型标注的依据类型 |", report)
        for label in ("模型标注：披露来源", "模型标注：公司指引", "模型标注：参考比较", "模型标注：研究假设"):
            self.assertIn(label, report)
        self.assertNotIn("已披露事实", report)
        self.assertIn("依据类型由模型声明；引用存在不证明预测或假设成立", report)

    def test_earnings_period_basis_is_distinct_from_valuation_date(self):
        for basis, label in (("trailing_at_valuation", "估值日滚动年度盈利"),
                             ("forward_from_valuation", "估值日之后年度盈利"),
                             ("fiscal_year", "指定财年盈利")):
            with self.subTest(basis=basis):
                draft, calculations, assessment = inputs()
                draft["earnings_basis"] = basis
                calculations["earnings_basis"] = basis
                report = "\n".join(render_forward(draft, calculations, assessment))
                self.assertIn("估值采用的盈利口径：" + label, report)
                self.assertIn("预测期间是收入、利润和现金流对应的区间", report)
                self.assertIn("条件估值日是把该盈利口径用于价格推演的时点，两者不等同", report)

    def test_rejected_scenario_stays_visible_without_becoming_a_recommendation(self):
        draft, calculations, assessment = inputs()
        opinion = assessment["scenario_assessments"][0]
        opinion.update(disposition="reject", reason="15 倍缺少可比依据。")
        report = "\n".join(render_forward(draft, calculations, assessment))
        self.assertIn("经营稳定（已拒绝）", report)
        self.assertIn("### F1 · 经营稳定：已拒绝", report)
        self.assertIn("已拒绝情景的数值仅保留作过程依据", report)
        self.assertIn("15 倍缺少可比依据。", report)
        self.assertIn("+25.00%", report)

    def test_table_cells_escape_delimiters_and_newlines_without_rewriting_prose(self):
        draft, calculations, assessment = inputs()
        draft["scenarios"][0]["name"] = "顺风|逆风\r\n新的行"
        draft["scenarios"][0]["exit_pe"].update(reason="依据|未证\n下一行 <b>", evidence_refs=["S01|S02"])
        report = "\n".join(render_forward(draft, calculations, assessment))
        self.assertIn("顺风&#124;逆风<br>新的行", report)
        self.assertIn("依据&#124;未证<br>下一行 &lt;b&gt;", report)
        self.assertIn("S01&#124;S02", report)
        expected_pipes = None
        for line in report.splitlines():
            if line.startswith("| "):
                if expected_pipes is None:
                    expected_pipes = line.count("|")
                self.assertEqual(line.count("|"), expected_pipes)
            else:
                expected_pipes = None
        self.assertIn(draft["scenarios"][0]["drivers"], report)

    def test_render_does_not_mutate_inputs(self):
        original = inputs()
        before = deepcopy(original)
        render_forward(*original)
        self.assertEqual(original, before)

    def test_empty_scenarios_retain_economic_analysis_and_missing_information(self):
        draft, calculations, assessment = inputs()
        draft["scenarios"] = []
        calculations["scenario_results"] = []
        assessment["scenario_assessments"] = []
        report = "\n".join(render_forward(draft, calculations, assessment))
        self.assertIn("当前没有可列示的量化情景", report)
        self.assertIn(assessment["economic_conclusion"], report)
        self.assertIn(assessment["confidence_and_limits"], report)
        self.assertIn(draft["limitations"][0], report)
        self.assertIn(calculations["limitations"][0], report)


if __name__ == "__main__":
    unittest.main()
