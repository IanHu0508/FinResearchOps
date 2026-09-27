"""Formal research report: a readable layout that keeps every report contract."""

from copy import deepcopy
import json
import re
import unittest

from finauditgate.application.research_delivery import evidence_catalog, normalize_report, report_context
from finauditgate.application.research_narrative import change_view
from finauditgate.application.research_report import (
    FILES, SUPPORTED, V1, V2, VERSION, VERSIONS, html_version, markdown_version, render,
)
from finauditgate.core.artifacts import sha256_hex
from finauditgate.core.forward_revision import apply_forward_revision


FILING = "[[PAGE 1]]\n合成年度报告：营业收入与利润摘要，期间FY2025。\n" * 45
QUANT_NOTE = "SYNTHETIC_QUANT_NOTE：二十个交易日横截面排序，不是上涨概率。"
ANALYSIS_MARKER = "SYNTHETIC_ANALYST_FULL_TEXT_STAYS_IN_WORKPAPER"
BELIEF_MARKER = "SYNTHETIC_INITIAL_BELIEF_WITH_UNBOUND_100_STAYS_IN_PROCESS"


def assumption(value, basis="analyst_assumption"):
    return {"value": value, "basis_type": basis, "reason": "合成假设", "evidence_refs": ["S01"]}


def source(sid, content, use="research", origin=None):
    return {"id": sid, "origin": origin or ("SYNTHETIC_ORIGIN_" + sid), "availability_note": "截止日前可得",
            "use": use, "content": content, "sha256": sha256_hex(content.encode())}


def bundle(origin=None):
    return {"schema_version": "finresearchops.thesis-sources/v2", "symbol": "600000.SH", "as_of": "2026-03-02",
            "identity": {"company_short_name": "合成公司", "exchange": "SSE", "market": "CN_A", "currency": "CNY"},
            "sources": [source("S01", FILING, origin=origin), source("QUANT", QUANT_NOTE),
                        source("S02", "编排者条件情景", use="sensitivity")]}


def block_ids(sources):
    catalog = evidence_catalog(sources)
    filing = [e["id"] for e in catalog if e["source_id"] == "S01"]
    quant = [e["id"] for e in catalog if e["source_id"] == "QUANT"]
    return filing, quant[0]


def draft():
    values = {"revenue": 100, "operating_margin": .2, "net_nonoperating_income": 0, "effective_tax_rate": .25,
              "diluted_ordinary_shares": 10, "non_working_capital_adjustments": 3,
              "operating_asset_liability_cash_effect": -2, "cash_capex": 5,
              "fx_reporting_per_price_currency": 1, "exit_pe": None, "cash_dividend_per_traded_unit": None}
    scenario = {"drivers": "驱动", "valuation_reasoning": "估值推理", "evidence_that_changes_case": "更新证据",
                "noncontrolling_attribution": {"nature": "profit", "amount": assumption(2)},
                **{key: assumption(value) for key, value in values.items()}}
    second = deepcopy(scenario)
    second["revenue"] = assumption(90)
    return {"business_model": "合成经营", "reporting_currency": "CNY", "price_currency": "CNY", "amount_unit": "million",
            "forecast_start": "2026-03-03", "forecast_end": "2027-03-02", "valuation_date": "2027-03-02",
            "earnings_basis": "trailing_at_valuation", "market_price_date": "2026-03-02",
            "market_price": assumption(20, "reported"), "shares_per_traded_unit": assumption(1),
            "limitations": ["原提案限制"],
            "scenarios": [{"scenario_id": "F1", "name": "基准条件", **scenario},
                          {"scenario_id": "F2", "name": "情景2号", **second}]}


def record(version="finresearchops.thesis-case/v17", *, summary=None, origin=None, edit=None):
    sources = bundle(origin)
    filing, quant = block_ids(sources)
    first, second = filing[0], filing[1]
    changes = [{"scenario_id": "F2", "field": "revenue", "expected_before": 90, "replacement": assumption(80),
                "correction_basis": "assumption_update", "reason": "下调收入假设", "evidence_refs": ["S01"]}]
    revision = apply_forward_revision(draft(), changes)
    candidate = {
        "rating": "REVIEW",
        "summary": {"text": summary or (
            "研究截止{{context:as_of}}，经营判断保持稳定（{{source:" + first + "}}）。"
            "价格面：价格已先行（{{source:" + second + "}}、{{source:" + quant + "}}）；"
            "F1收入{{metric:F1:revenue}}，每股收益{{metric:F1:eps_per_traded_unit}}。"
            "结论：维持研究观察，不形成定价结论。")},
        "financial_analysis": {
            "operating_performance": {"text": "经营表现有资料支持（{{source:" + first + "}}，但期间有限）。"},
            "earnings_quality": {"text": "盈利路径为{{metric:F2:parent_net_income}}，仍是研究假设。"},
            "cash_and_capital_allocation": {"text": "现金流F1经营现金流{{metric:F1:operating_cash_flow}}。"},
            "valuation_and_price_requirements": {"text": "打平市盈率{{metric:F1:price_only_break_even_pe}}与{{metric:F2:price_only_break_even_pe}}均为条件反推。"}},
        "strongest_counterevidence": {"text": "最强反证来自价格先行的组合风险。其一，价格已大幅上涨（{{source:" + second + "}}）。其二，盈利尚未兑现。"},
        "scenario_assessments": [
            {"scenario_id": "F1", "disposition": "use", "reason": "基准情景，收入{{metric:F1:revenue}}。",
             "what_changes_the_view": "下修触发：扣非转负。上修触发：订单改善。"},
            {"scenario_id": "F2", "disposition": "conditional", "reason": "下行情景，归母净利润{{metric:F2:parent_net_income}}。",
             "what_changes_the_view": "价格转跌。"}],
        "limitations": ["研究假设不是事实（{{source:" + first + "}}）。"],
        "change_explanations": [{"scenario_id": "F2", "field": "revenue",
                                 "explanation": {"text": "下调后收入为{{metric:F2:revenue}}，仍是假设。"}}],
        "belief_explanations": [{"belief_id": "D1", "explanation": {"text": "维持D1。支持面：资料有限（{{source:" + first + "}}）。"}}],
    }
    if edit:
        edit(candidate)
    final = normalize_report(candidate, sources)
    request = {"symbol": "600000.SH", "as_of": "2026-03-02", "horizon_months": 12, "data_mode": "FROZEN_SOURCES",
               "question": "合成研究问题。", "hypotheses": ["合成待检验假设"], "research_constraints": ["合成研究约束"],
               "user_view": "SYNTHETIC_DESIRED_BUY_STAYS_SEPARATE"}
    forward_revision = {"changes": changes, "claim_assessments": [], "unresolved_issues": [],
                        "belief_updates": [{"belief_id": "D1", "status": "maintain", "new_statement": None,
                                            "reason": "维持", "evidence_refs": ["S01"]}]}
    context = report_context(final, revision["effective_forward_draft"], revision["effective_forward_calculations"],
                             sources, request, changes=change_view(revision["applied_changes"]),
                             beliefs=forward_revision["belief_updates"])
    check = context.evidence_check()

    def exchange(node, kind, cites_quant):
        payload = {"node": node, "request": {}, "source_bundle": {"sources": [{"id": "S01"}, {"id": "QUANT"}]}}
        return {"node": node, "kind": kind, "response_id": node + kind,
                "messages": [{"role": "system", "content": "SYNTHETIC"}, {"role": "user", "content": json.dumps(payload)}],
                "parsed": {"evidence_refs": ["QUANT"] if cites_quant else ["S01"]}}

    value = {"schema_version": version, "status": check["status"], "review_status": "AWAITING_REVIEW",
             "request": request, "source_bundle": sources, "forward_draft": draft(),
             "forward_revision": forward_revision, "final_report": final, "evidence_check": check,
             "rating_comparison": {"before": "REVIEW", "after": "REVIEW", "changed": False},
             "independent_assessment": {"beliefs": [{"belief_id": "D1", "statement": BELIEF_MARKER}],
                                        "decision": {"rating": "REVIEW", "executive_summary": BELIEF_MARKER}},
             "exchanges": [exchange("Market Analyst", "AnalystReport", True), exchange("Portfolio Manager", "FinalResearchReport", False)],
             **revision}
    if version.endswith("v17"):
        report = {"analysis": ANALYSIS_MARKER, "observations": [{"statement": ANALYSIS_MARKER, "evidence_refs": ["S01"]}],
                  "coverage": "partial", "limits": [ANALYSIS_MARKER], "evidence_refs": ["S01", "QUANT"]}
        value["analyst_reports"] = {node: deepcopy(report) for node in (
            "Fundamentals Analyst", "Market Analyst", "News Analyst", "Sentiment Analyst")}
    return value


def body(markdown):
    return markdown.split("## 附录一", 1)[0]


class ResearchReportTest(unittest.TestCase):
    def test_conventional_sections_in_order_and_process_material_excluded(self):
        markdown, page = (part.decode() for part in render(record()))
        self.assertTrue(markdown.startswith("<!-- " + VERSION + " -->\n# 合成公司（600000.SH）公司研究报告"))
        headings = re.findall(r"^## (.+)$", markdown, re.M)
        self.assertEqual(["投资要点", "盈利预测与估值（情景）", "一、经营表现与持续性", "二、盈利质量与归母勾稽",
                          "三、现金创造与资本配置", "四、估值与当前价格要求", "五、情景分析", "六、主要风险与反证",
                          "七、研究局限与待验证事项", "附录一　研究流程与观点更新", "附录二　引用与资料目录", "重要说明"], headings)
        for marker in (ANALYSIS_MARKER, BELIEF_MARKER, "```", "四类分析原始输出", "各阶段引用记录", "编排者条件情景"):
            self.assertNotIn(marker, markdown)
            self.assertNotIn(marker, page)
        self.assertNotIn(FILING[:40], markdown)  # excerpts stay in the workpaper
        self.assertEqual(("research-report.md", "research-report.html"), FILES)
        self.assertIn("finresearchops.thesis-case/v16", SUPPORTED)

    def test_citations_are_numbered_by_first_use_and_listed_with_exact_locations(self):
        value = record()
        markdown = render(value)[0].decode()
        filing, quant = block_ids(value["source_bundle"])
        self.assertIn("经营判断保持稳定［1］。", markdown)
        self.assertIn("**价格面：**价格已先行［2］［3］；", markdown)
        self.assertNotIn("（［1］）", markdown)
        # A parenthesis that also holds prose keeps the model's characters.
        self.assertIn("经营表现有资料支持（［1］，但期间有限）。", markdown)
        rows = re.findall(r"^\| ［(\d+)］ \| (\S+) \| (\S+) \| ([\d,]+)–([\d,]+) \|$", markdown, re.M)
        catalog = {e["id"]: e for e in evidence_catalog(value["source_bundle"])}
        self.assertEqual([("1", "S01", filing[0]), ("2", "S01", filing[1]), ("3", "QUANT", quant)], [r[:3] for r in rows])
        for number, sid, qid, start, end in rows:
            self.assertEqual((catalog[qid]["start"], catalog[qid]["end"]), (int(start.replace(",", "")), int(end.replace(",", ""))))
        page = render(value)[1].decode()
        self.assertIn('<li id="ref-1">', page)
        self.assertIn(catalog[filing[0]]["text"][:30], page)  # one click away in HTML, collapsed

    def test_figures_come_from_effective_calculations_with_program_labels(self):
        value = record()
        markdown = render(value)[0].decode()
        results = {r["scenario_id"]: r for r in value["effective_forward_calculations"]["scenario_results"]}
        eps = f"{results['F1']['eps_per_traded_unit']:,.2f}元"
        self.assertIn("F1收入100.00百万元，每股收益" + eps + "。", markdown)
        # The clause names neither the scenario nor the metric here.
        self.assertIn(f"盈利路径为{results['F2']['parent_net_income']:,.2f}百万元（F2 归母净利润）", markdown)
        self.assertIn(f"打平市盈率{results['F1']['price_only_break_even_pe']:,.2f}倍（F1）与"
                      f"{results['F2']['price_only_break_even_pe']:,.2f}倍（F2）", markdown)
        # Inside a scenario section its own ID is implied by the heading.
        self.assertIn("### F1 基准条件（采用）\n\n基准情景，收入100.00百万元。", markdown)
        self.assertIn("下调后收入为80.00百万元", markdown)
        table = markdown.split("## 盈利预测与估值（情景）", 1)[1].split("## 一、", 1)[0]
        self.assertIn(f"| F1 | 采用 | 100.00 | 20.00% | {results['F1']['parent_net_income']:,.2f} |", table)
        self.assertIn("| F2 | 有条件采用 | 80.00 |", table)
        self.assertIn("未设定期末市盈率、期间现金股息", table)

    def test_numeric_and_markup_contracts_are_not_relaxed(self):
        for text, code in (("EPS为999元。", "UNBOUND_RESEARCH_NUMBER"), ("收入<b>高</b>。", "RESEARCH_NARRATIVE_MARKUP_INVALID")):
            with self.subTest(text=text):
                value = record()
                value["final_report"]["summary"]["text"] += text
                with self.assertRaisesRegex(ValueError, code):
                    render(value)

    def test_unresolved_citation_is_marked_pending_never_numbered(self):
        value = record(summary="经营判断待核（{{source:E9999}}）。结论：维持研究观察。")
        self.assertEqual("PARTIAL", value["evidence_check"]["status"])
        markdown = render(value)[0].decode()
        self.assertIn("经营判断待核〔证据待核：E9999〕。", markdown)
        self.assertIn("本报告2项引用未能定位到所给资料原文，模型评级不能视为获准结论", markdown)
        self.assertIn("| 引用定位 | 2个证据块定位到原文，2项引用待核，见附录二 |", markdown)
        self.assertIn("| 未绑定数字 | 无 |", markdown)
        self.assertIn("### 引用待核", markdown)
        self.assertNotIn("### 数字待核", markdown)
        legacy = render(value, V1)[0].decode()
        self.assertIn("部分引用或数字未能由程序绑定到原文或计算", legacy)
        self.assertIn("### 待核项", legacy)
        forged = deepcopy(value)
        forged["evidence_check"]["findings"] = []
        forged["evidence_check"]["status"] = forged["status"] = "COMPLETED"
        with self.assertRaisesRegex(ValueError, "THESIS_EVIDENCE_CHECK_CHANGED"):
            render(forged)

    def test_unbound_numerals_are_flagged_where_they_stand(self):
        value = record(summary="DDR5与LPDDR5X需求仍需观察（{{source:E0001}}）。结论：维持研究观察。")
        self.assertEqual("PARTIAL", value["evidence_check"]["status"])
        markdown, page = (part.decode() for part in render(value))
        self.assertIn("DDR5〔待核〕与LPDDR5X〔待核〕需求仍需观察［1］。", markdown)
        self.assertIn('title="未经程序计算或来源绑定的数字，需核对">DDR5</span>', page)
        self.assertIn("| 引用定位 | 2个证据块均定位到原文，不等于事实核验 |", markdown)
        self.assertIn("| 未绑定数字 | 2项待核，见附录二 |", markdown)
        self.assertIn("本报告2个数字未经程序计算或来源绑定，模型评级不能视为获准结论", markdown)
        self.assertIn("### 数字待核", markdown)
        self.assertIn("| summary | DDR5 | 未经程序计算或来源绑定，需核对是否为金融数值 |", markdown)
        legacy = render(value, V1)[0].decode()
        self.assertIn("| 引用定位 | 2项待核，见附录二 |", legacy)
        self.assertIn("| summary | DDR5 | 未绑定数字：未经程序计算或来源绑定，需核对是否为金融数值 |", legacy)
        self.assertIn("部分引用或数字未能由程序绑定到原文或计算", legacy)

    def test_v1_output_is_byte_identical_for_saved_reports(self):
        # sha256 of Markdown and HTML rendered by the released v1 code, before v2 existed.
        expected = {
            "completed": ("51dacda284a8c9038a578026e4706fec340e626d4d710854028b8d30aa30acc7",
                          "57b40bd2bcc30c4e27d8a99cd328fe129b9356886feb2b5a408070f33100eb40"),
            "number_pending": ("27baa393227e241f87edab8ea41896e8ea37c8d4a3e449c76c4f12bdc18729d6",
                               "be197cf3eecfbbdb466b6acde751d44bb82593c2ff9526f188216cfe4e18dbf7"),
            "citation_pending": ("54f7866b7d13766b5d07a0b53d9051c88b6a3ee6d1d1f42ed76ea2c6b9dbda5d",
                                 "1cc51136e59d51ed72a2985bbaa5dfae58a9398c854ce503d4d4a6c68c71d051"),
            "v16": ("b5b92df8decb73c4780a4b098696bba18da9bb2da2a79dc7d4691b4bb78a3c2e",
                    "2e50016ed90dc282dc0a141b7e00747c0b70d6cb0f5046470b1fd995d65a6736"),
        }
        records = {"completed": record(),
                   "number_pending": record(summary="DDR5与LPDDR5X需求仍需观察（{{source:E0001}}）。结论：维持研究观察。"),
                   "citation_pending": record(summary="经营判断待核（{{source:E9999}}）。结论：维持研究观察。"),
                   "v16": record("finresearchops.thesis-case/v16")}
        for name, value in records.items():
            with self.subTest(name=name):
                self.assertEqual(expected[name], tuple(sha256_hex(part) for part in render(value, V1)))

    def test_v2_cover_counts_citations_and_unbound_numbers_apart(self):
        markdown = render(record())[0].decode()
        self.assertIn("| 引用定位 | 3个证据块均定位到原文，不等于事实核验 |", markdown)
        self.assertIn("| 未绑定数字 | 无 |", markdown)
        self.assertNotIn("模型评级不能视为获准结论", body(markdown))

        def uncited(candidate):
            candidate["financial_analysis"]["operating_performance"]["text"] = "经营表现仍需观察。"
            candidate["strongest_counterevidence"]["text"] = "最强反证：价格先行。"
            candidate["limitations"] = ["研究假设不是事实。"]
            candidate["belief_explanations"][0]["explanation"]["text"] = "维持D1。"
        value = record(summary="经营判断保持稳定。结论：维持研究观察。", edit=uncited)
        self.assertEqual([], value["evidence_check"]["bindings"])
        self.assertIn("| 引用定位 | 未使用证据块引用 |", render(value)[0].decode())
        self.assertIn("| 引用定位 | 0个证据块均定位到原文，不等于事实核验 |", render(value, V1)[0].decode())

    def test_format_version_is_recorded_in_both_files_and_unknown_versions_are_refused(self):
        self.assertEqual((V1, V2), VERSIONS)
        self.assertEqual(V2, VERSION)
        for version in VERSIONS:
            with self.subTest(version=version):
                markdown, page = render(record(), version)
                self.assertEqual(version, markdown_version(markdown))
                self.assertEqual(version, html_version(page))
                self.assertIn("报告格式 " + version, markdown.decode())
        self.assertIsNone(markdown_version(b"# no marker\n"))
        self.assertIsNone(html_version(b"<html></html>"))
        with self.assertRaisesRegex(ValueError, "RESEARCH_REPORT_VERSION_UNSUPPORTED"):
            render(record(), "finresearchops.research-report/v9")

    def test_program_context_values_are_not_reclassified_as_prose_numbers(self):
        value = record(summary="标的{{context:symbol}}研究截止{{context:as_of}}，期限{{context:horizon_months}}个月。结论：观察。")
        self.assertEqual("COMPLETED", value["evidence_check"]["status"])
        markdown = render(value)[0].decode()
        self.assertIn("标的600000.SH研究截止2026-03-02，期限12个月。", markdown)
        self.assertNotIn("〔待核〕", markdown)

    def test_scenario_names_are_shown_only_when_they_pass_the_same_contract(self):
        markdown = render(record())[0].decode()
        self.assertIn("### F1 基准条件（采用）", markdown)
        self.assertIn("### F2（有条件采用）", markdown)
        self.assertNotIn("情景2号", markdown)

    def test_appendix_keeps_inputs_quant_use_and_updates_compact(self):
        markdown = render(record())[0].decode()
        appendix = markdown.split("## 附录一", 1)[1]
        self.assertIn("本报告由2个模型阶段依次产生：基本面、市场、新闻与情绪四类分析", appendix)
        self.assertIn("| 基本面分析 | 资料不完整 | 1 | 2 |", appendix)
        self.assertIn("**D1（模型标记：维持）**", appendix)
        self.assertIn("| F2 | 营业收入（百万元） | 90.00 | 80.00 | 数值已改 |", appendix)
        self.assertIn("进入2/2个阶段，其中1个阶段的返回显式引用；本报告引用编号：［3］。", appendix)
        self.assertIn("合成待检验假设", appendix)
        self.assertIn("SYNTHETIC\\_DESIRED\\_BUY\\_STAYS\\_SEPARATE", appendix)
        self.assertNotIn("SYNTHETIC_DESIRED", body(markdown))
        self.assertIn("| S02 | SYNTHETIC_ORIGIN_S02（情景附录资料，未进入评级请求） | 截止日前可得 |", appendix)
        v16 = render(record("finresearchops.thesis-case/v16"))[0].decode()
        self.assertNotIn("四类分析", v16)
        self.assertIn("本报告由2个模型阶段依次产生：多空研究员独立初稿", v16)

    def test_html_is_escaped_deterministic_and_print_ready(self):
        value = record(origin="<script>alert(1)</script>")
        first, second = render(value), render(deepcopy(value))
        self.assertEqual(first, second)
        page = first[1].decode()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn(f'<meta name="generator" content="{VERSION}">', page)
        self.assertIn("@page{size:A4", page)
        self.assertEqual(1, page.count("<main"))
        self.assertEqual(1, page.count("</main>"))
        self.assertEqual(page.count("<table>"), page.count("</table>"))

    def test_unsupported_case_versions_are_refused(self):
        value = record()
        value["schema_version"] = "finresearchops.thesis-case/v13"
        with self.assertRaisesRegex(ValueError, "RESEARCH_REPORT_CASE_VERSION_UNSUPPORTED"):
            render(value)


if __name__ == "__main__":
    unittest.main()
