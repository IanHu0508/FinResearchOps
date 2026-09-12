"""Synthetic checks for current report and separately preserved process output."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from finauditgate.application.thesis_report_v11 import render, render_process
from finauditgate.core.forward_revision import apply_forward_revision
from finauditgate.core.forward_scenarios import calculate_forward


def assumption(value, reason="合成预测假设"):
    return {"value": value, "basis_type": "analyst_assumption", "reason": reason, "evidence_refs": ["S01"]}


def block(text="保留原样的经营解释。", metrics=()):
    return {"text": text, "evidence_refs": ["S01"],
            "metrics": [{"scenario_id": "F1", "metric": metric} for metric in metrics]}


def record():
    values = {"revenue": 100, "operating_margin": .2, "net_nonoperating_income": 0,
              "effective_tax_rate": .25, "diluted_ordinary_shares": 10,
              "non_working_capital_adjustments": 3, "operating_asset_liability_cash_effect": -2,
              "cash_capex": 5, "fx_reporting_per_price_currency": 1,
              "exit_pe": None, "cash_dividend_per_traded_unit": None}
    draft = {"business_model": "合成经营公司", "reporting_currency": "USD", "price_currency": "USD", "amount_unit": "million",
             "forecast_start": "2027-01-01", "forecast_end": "2027-12-31", "valuation_date": "2027-12-31",
             "earnings_basis": "trailing_at_valuation", "market_price_date": "2026-12-31", "market_price": assumption(10),
             "shares_per_traded_unit": assumption(1), "limitations": ["原提案完整限制标记"],
             "scenarios": [{"scenario_id": "F1", "name": "基准条件", "drivers": "原提案驱动文字标记",
                            "noncontrolling_attribution": {"nature": "loss", "amount": assumption(2, "原误读说明标记")},
                            **{key: assumption(value) for key, value in values.items()},
                            "valuation_reasoning": "原估值推理标记", "evidence_that_changes_case": "原待更新证据标记"}]}
    changes = [{"scenario_id": "F1", "field": "noncontrolling_attribution",
                "expected_before": {"nature": "loss", "amount": 2},
                "replacement": {"nature": "profit", "amount": assumption(2, "重新核对盈利归属")},
                "correction_basis": "source_misread", "reason": "原表的括号表示盈利扣减", "evidence_refs": ["S01"]}]
    revision = apply_forward_revision(draft, changes)
    return {"schema_version": "finresearchops.thesis-case/v11",
            "request": {"symbol": "SYNTH", "as_of": "2026-12-31", "horizon_months": 12, "data_mode": "FROZEN_SOURCES",
                        "question": "检验经营与价格。\n保留原问题", "hypotheses": ["销量可能改善"],
                        "research_constraints": ["关注现金风险"], "user_view": "我希望看多\n原文保持"},
            "source_bundle": {"sources": [{"id": "S01", "origin": "合成财报", "availability_note": "截止日前披露", "use": "research", "content": "合成来源"},
                                          {"id": "S02", "origin": "合成情景", "availability_note": "编排者假设", "use": "sensitivity", "content": "独立附录情景标记"}]},
            "forward_draft": draft, "forward_calculations": calculate_forward(draft), **revision,
            "forward_revision": {"changes": changes, "claim_assessments": [], "belief_updates": [
                {"belief_id": "D1", "status": "revise", "new_statement": "盈利归属需要扣减",
                 "update_basis": "reasoning_correction", "reason": "修正历史口径误读", "financial_implication": "每股盈利下调",
                 "evidence_refs": ["S01"]}], "unresolved_issues": ["缺少合理退出倍数"]},
            "final_report": {"rating": "REVIEW", "summary": block("经营判断与价格判断分别说明。", ["parent_net_income", "eps_per_traded_unit"]),
                             "financial_analysis": {"operating_performance": block(), "earnings_quality": block(metrics=["consolidated_net_income", "parent_net_income"]),
                                                    "cash_and_capital_allocation": block(metrics=["cash_after_capex_proxy"]),
                                                    "valuation_and_price_requirements": block(metrics=["price_only_break_even_pe"])},
                             "strongest_counterevidence": block("归属误读改变每股盈利。"),
                             "scenario_assessments": [{"scenario_id": "F1", "disposition": "conditional", "reason": "需要经营假设兑现", "what_changes_the_view": "回款与盈利归属变化"}],
                             "limitations": ["当前模型解释未经人类金融验收"]},
            "rating_comparison": {"before": "Hold", "after": "REVIEW", "changed": True},
            "independent_assessment": {"decision": {"rating": "Hold", "executive_summary": "初判摘要只进附录"},
                                       "beliefs": [{"belief_id": "D1", "statement": "初始信念原文", "would_change_mind": "出现明确归属反证"}]},
            "reports": {"fundamentals_report": "基本面过程原文", "market_report": "市场过程原文", "investment_plan": "研究经理旧长文标记",
                        "trader_investment_plan": "交易员旧长文标记", "final_trade_decision": "不应继承的旧终判标记"},
            "research_evaluation": {"plan": "经理完整原记录"}, "execution_review": {"proposal": "交易完整原记录"},
            "initial": {"Bull Researcher": "初始多头原记录"}, "revisions": {"Bull Researcher": "修改多头原记录"}}


class ThesisReportV11Tests(unittest.TestCase):
    def test_main_table_uses_effective_parent_earnings_and_cash(self):
        value = record()
        text = render(value).decode()
        main_table = text.split("## 当前有效情景与计算", 1)[1].split("## 经营表现与持续性", 1)[0]
        self.assertIn("| F1 · 基准条件（有条件采用） | 100.00 | 20.00% | 15.00 | 13.00 | 1.30 | 16.00 | 11.00 |", main_table)
        self.assertNotIn("17.00", main_table)
        self.assertNotIn("1.70", main_table)
        self.assertIn("归属于母公司股东的净利润 | 13", text)
        self.assertIn("每交易单位摊薄盈利（EPS） | 1.3", text)

    def test_original_numbers_and_verbose_proposals_are_preserved_in_process_only(self):
        value = record()
        main, process = render(value).decode(), render_process(value).decode()
        self.assertNotIn("17.00", main)
        self.assertIn("归母净利润 17.00", process)
        for marker in ("原提案驱动文字标记", "原估值推理标记", "原提案完整限制标记",
                       "研究经理旧长文标记", "交易员旧长文标记", "初判摘要只进附录"):
            self.assertNotIn(marker, main)
            self.assertIn(marker, process)
        self.assertNotIn("不应继承的旧终判标记", main)
        self.assertIn('"parent_net_income": 17.0', process)
        self.assertIn('"parent_net_income": 13.0', process)
        self.assertIn("不代表当前采纳", process)

    def test_old_assumption_reason_appears_only_in_correction_difference(self):
        text = render(record()).decode()
        before, rest = text.split("## 参数修正与尚未解决的问题", 1)
        diff, after = rest.split("## 旧信念为何维持或改变", 1)
        self.assertNotIn("原误读说明标记", before + after)
        self.assertIn("原误读说明标记", diff)
        self.assertIn("原输入（已替换）", diff)
        self.assertIn("当前有效输入", diff)
        self.assertIn("少数股东亏损归属额（加回）", diff)
        self.assertIn("少数股东盈利归属额（从合并净利扣除）", diff)
        self.assertIn("修正引用：S01", diff)

    def test_every_report_block_delegates_to_effective_metric_binder(self):
        value = record()
        with patch("finauditgate.application.thesis_report_v11.render_research_block", return_value=["BOUND_BY_RENDERER", ""]) as binder:
            text = render(value).decode()
        self.assertEqual(binder.call_count, 6)
        self.assertEqual(text.count("BOUND_BY_RENDERER"), 6)
        for args in binder.call_args_list:
            self.assertEqual(args.args[1], value["effective_forward_draft"])
            self.assertEqual(args.args[2], value["effective_forward_calculations"])

    def test_rejected_scenario_visible_in_main_table_and_each_bound_reference_status(self):
        value = record()
        value["final_report"]["scenario_assessments"][0]["disposition"] = "reject"
        text = render(value).decode()
        self.assertIn("F1 · 基准条件（已拒绝）", text)
        self.assertIn("本段量化引用的当前状态：F1 · 已拒绝", text)
        self.assertIn("已拒绝路径只用于说明分歧，不代表采用", text)
        self.assertIn("### F1 · 已拒绝", text)
        self.assertIn("13.00", text)

    def test_original_request_expectation_sources_and_process_links_are_locatable(self):
        value = record()
        main, process = render(value).decode(), render_process(value).decode()
        for text in (main, process):
            for expected in ("> 我希望看多\n> 原文保持", "> 检验经营与价格。\n> 保留原问题", "仅记录；未进入主研究请求",
                             "销量可能改善", "关注现金风险", "合成财报", "截止日前披露", "S01", "S02"):
                self.assertIn(expected, text)
        self.assertIn("[过程记录附录](process-record.md)", main)
        self.assertIn("[主报告](report.md)", process)
        self.assertNotIn("独立附录情景标记", main)
        self.assertIn("独立附录情景标记", process)

    def test_belief_updates_and_changed_mind_conditions_remain_in_main_report(self):
        text = render(record()).decode()
        for expected in ("独立初判：Hold；当前终判：REVIEW", "### D1 · 修改", "初始信念原文", "修正历史口径误读",
                         "本次修正表述：盈利归属需要扣减", "什么会改变判断：回款与盈利归属变化",
                         "出现明确归属反证", "不是终判硬门槛"):
            self.assertIn(expected, text)

    def test_missing_values_are_not_displayed_as_zero_and_no_changes_are_explicit(self):
        value = record()
        value["applied_changes"] = []
        text = render(value).decode()
        self.assertIn("本轮没有应用参数修改", text)
        price_table = text.split("| 情景与采纳 | 条件PE", 1)[1].split("已拒绝情景仍显示", 1)[0]
        self.assertIn("未能计算", price_table)
        self.assertNotIn("0.00%", price_table)

    def test_both_renderers_leave_inputs_unchanged_and_return_bytes(self):
        value = record()
        before = deepcopy(value)
        self.assertIsInstance(render(value), bytes)
        self.assertIsInstance(render_process(value), bytes)
        self.assertEqual(value, before)

    def test_empty_scenarios_preserve_qualitative_report(self):
        value = record()
        for key in ("forward_draft", "effective_forward_draft"):
            value[key]["scenarios"] = []
        for key in ("forward_calculations", "effective_forward_calculations"):
            value[key]["scenario_results"] = []
        value["final_report"]["scenario_assessments"] = []
        value["applied_changes"] = []
        value["final_report"]["summary"]["metrics"] = []
        for chapter in value["final_report"]["financial_analysis"].values():
            chapter["metrics"] = []
        text = render(value).decode()
        self.assertIn("未形成适用的量化情景", text)
        self.assertIn("经营判断与价格判断分别说明", text)

    def test_corrected_common_parameter_has_units_without_scenario_claim(self):
        value = record()
        value["applied_changes"] = [{"scenario_id": None, "field": "market_price", "before": assumption(10), "after": assumption(11),
                                    "correction_basis": "source_misread", "reason": "修正同日价格读数", "evidence_refs": ["S01"]}]
        text = render(value).decode()
        self.assertIn("### 共同参数 · 起点市场价格", text)
        self.assertIn("10 USD／交易单位", text)
        self.assertIn("11 USD／交易单位", text)

    def test_table_content_is_escaped_without_rewriting_model_prose(self):
        value = record()
        value["effective_forward_draft"]["scenarios"][0]["name"] = "情景|分支\n<b>"
        value["final_report"]["summary"]["text"] = "解释|原文\n第二行"
        text = render(value).decode()
        self.assertIn("情景&#124;分支<br>&lt;b&gt;", text)
        self.assertIn("解释|原文\n第二行", text)

    def test_main_report_exposes_effective_denominator_dividend_and_cash_inputs(self):
        value = record()
        effective = value["effective_forward_draft"]
        scenario = effective["scenarios"][0]
        assumptions = {"net_nonoperating_income": -1.5, "effective_tax_rate": .21,
                       "diluted_ordinary_shares": 8.5, "fx_reporting_per_price_currency": 1.25,
                       "cash_dividend_per_traded_unit": .75, "non_working_capital_adjustments": 7,
                       "operating_asset_liability_cash_effect": -4, "cash_capex": 6, "exit_pe": 12}
        for field, number in assumptions.items():
            scenario[field] = assumption(number, "完整预测依据只在附录展示")
        effective["market_price"] = assumption(11)
        effective["shares_per_traded_unit"] = assumption(5)
        value["effective_forward_calculations"] = calculate_forward(effective)
        main = render(value).decode()
        inputs = main.split("### 当前计算实际采用的输入", 1)[1].split("已拒绝情景仍显示", 1)[0]
        for expected in ("起点市场价格 | 11 USD／交易单位", "每交易单位对应普通股数 | 5 普通股／交易单位",
                         "非经营净收益（损失为负） | -1.5 百万元 USD", "有效税率 | 21.00%",
                         "少数股东盈利归属额（从合并净利扣除） | 2 百万元 USD",
                         "摊薄普通股数 | 8.5 百万普通股", "汇率 | 1.25 USD／USD",
                         "期间现金股息／交易单位 | 0.75 USD／交易单位",
                         "非营运资金调整（含非现金等） | 7 百万元 USD",
                         "经营性资产负债现金影响（非严格NWC或余额变化） | -4 百万元 USD",
                         "现金资本购买 | 6 百万元 USD", "期末条件市盈率 | 12 倍", "S01"):
            self.assertIn(expected, inputs)
        self.assertNotIn("摊薄普通股数 | 10 百万普通股", inputs)
        self.assertNotIn("汇率 | 1 USD／USD", inputs)
        self.assertNotIn("完整预测依据只在附录展示", main)
        self.assertIn("完整预测依据只在附录展示", render_process(value).decode())
        self.assertIn("未来情景的假设", inputs)
        self.assertIn("其他归属调整或摊薄项目未单列时须另行核对", inputs)
        self.assertIn("不构成法定摊薄EPS认证", inputs)

    def test_effective_input_table_preserves_unknowns_and_rejected_scenario_status(self):
        value = record()
        value["final_report"]["scenario_assessments"][0]["disposition"] = "reject"
        text = render(value).decode()
        inputs = text.split("### 当前计算实际采用的输入", 1)[1].split("已拒绝情景仍显示", 1)[0]
        self.assertIn("#### F1 · 已拒绝：有效参数", inputs)
        self.assertIn("期间现金股息／交易单位 | 未能计算", inputs)
        self.assertIn("期末条件市盈率 | 未能计算", inputs)
        self.assertNotIn("期间现金股息／交易单位 | 0", inputs)


if __name__ == "__main__":
    unittest.main()
