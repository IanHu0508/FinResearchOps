"""Evidence delivery cannot turn unresolved citations into verified facts."""

from copy import deepcopy
import unittest

from test_research_numbers import inputs
from finauditgate.application.research_delivery import evidence_catalog, final_source_view, normalize_report, report_context
from finauditgate.core.artifacts import sha256_hex


def sources(body="SYNTHETIC annual filing. Revenue: 100 million USD. Scope is consolidated; period FY2025."):
    return {"schema_version": "finresearchops.thesis-sources/v2", "sources": [{"id": "S01", "content": body,
        "sha256": sha256_hex(body.encode()), "use": "research"}]}


def report(text="经营情况仍需观察。"):
    return {"rating": "REVIEW", "summary": {"text": text},
        "financial_analysis": {key: {"text": "保留完整研究内容。"} for key in (
            "operating_performance", "earnings_quality", "cash_and_capital_allocation", "valuation_and_price_requirements")},
        "strongest_counterevidence": {"text": "不利观察需要改变判断。"},
        "scenario_assessments": [{"scenario_id": "F1", "disposition": "conditional", "reason": "经营条件未证实。",
            "what_changes_the_view": "经营变化。"}], "limitations": ["研究假设不是事实。"],
        "change_explanations": [], "belief_explanations": []}


def pending_numbers(ctx):
    return [r["reference"] for r in ctx.evidence_check()["findings"] if r["reason"] == "UNBOUND_RESEARCH_NUMBER_PENDING"]


def context(value, bundle=None, *, legacy=False):
    bundle = bundle or sources()
    normalized = normalize_report(value, bundle, legacy=legacy)
    ctx = report_context(normalized, *inputs(), bundle,
        {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12}, changes=[], beliefs=[], legacy=legacy)
    return normalized, ctx


class ResearchDeliveryTest(unittest.TestCase):
    def test_catalog_covers_every_character_without_cleanup_or_joining(self):
        body = "[[PAGE 1]]\n单位：百万元\n" + ("合成表格\t营业收入100.50\n\n" * 280) + "[[PAGE 2]]\n脚注：归属口径不同。"
        bundle = sources(body)
        before = deepcopy(bundle)
        entries = evidence_catalog(bundle)
        self.assertEqual(body, "".join(e["text"] for e in entries))
        self.assertEqual(len(entries), len({e["id"] for e in entries}))
        for e in entries:
            self.assertEqual(body[e["start"]:e["end"]], e["text"])
            self.assertEqual(sha256_hex(body.encode()), e["source_sha256"])
        view = final_source_view(bundle)
        self.assertNotIn("content", view["sources"][0])
        self.assertEqual(body, "".join(e["text"] for e in view["sources"][0]["content_blocks"]))
        self.assertEqual(before, bundle)

    def test_findings_do_not_depend_on_financial_section_key_order(self):
        value = report()
        value["financial_analysis"]["operating_performance"]["text"] = "需跟踪DDR5进展。"
        value["financial_analysis"]["cash_and_capital_allocation"]["text"] = "需核对前20大客户。"
        _, ctx = context(value)
        reordered = deepcopy(value)
        reordered["financial_analysis"] = dict(reversed(list(value["financial_analysis"].items())))
        _, other = context(reordered)
        self.assertEqual(ctx.evidence_check(), other.evidence_check())
        self.assertEqual(["financial_analysis.cash_and_capital_allocation", "financial_analysis.operating_performance"],
                         [r["field"] for r in ctx.evidence_check()["findings"]])

    def test_source_binding_uses_catalog_offsets_even_for_repeated_text(self):
        bundle = sources("重复披露，需核对上下文。\n" * 200)
        value, ctx = context(report("历史：{{source:E0002}}；条件EPS：{{metric:F1:eps_per_traded_unit}}。"), bundle)
        self.assertEqual(["S01"], value["summary"]["evidence_refs"])
        self.assertEqual([{"scenario_id": "F1", "metric": "eps_per_traded_unit"}], value["summary"]["metrics"])
        self.assertEqual("COMPLETED", ctx.evidence_check()["status"])
        binding = ctx.evidence_check()["bindings"][0]
        self.assertGreater(binding["start"], 0)
        self.assertIn("原文", ctx.block(value["summary"])["text"])

    def test_unknown_or_sensitivity_evidence_is_partial_not_fabricated(self):
        bundle = sources()
        bundle["sources"].append({"id": "S02", "content": "UNTRUSTED_SENSITIVITY", "use": "sensitivity"})
        value, ctx = context(report("已有内容。{{source:E9999}} 保留反证。"), bundle)
        self.assertEqual("PARTIAL", ctx.evidence_check()["status"])
        self.assertFalse(ctx.evidence_check()["bindings"])
        text = ctx.block(value["summary"])["text"]
        self.assertIn("保留反证", text)
        self.assertIn("证据待核", text)
        self.assertNotIn("来源原文", text)
        self.assertNotIn("UNTRUSTED", str(final_source_view(bundle)))

    def test_valid_extra_selectors_are_retained_without_duplicate_echo_requirement(self):
        raw = report("{{metric:F1:eps_per_traded_unit}}")
        raw["scenario_assessments"][0]["metrics"] = [{"scenario_id": "F1", "metric": "revenue"}]
        before = deepcopy(raw)
        value, _ = context(raw)
        self.assertEqual(raw["scenario_assessments"][0]["metrics"], value["scenario_assessments"][0]["metrics"])
        self.assertEqual(before, raw)
        for bad in ({"scenario_id": "F1", "metric": "revenue", "value": 999},
                    {"scenario_id": "F1", "metric": "invented_target"}):
            raw["summary"]["metrics"] = [bad]
            with self.assertRaises(ValueError):
                context(raw)

    def test_non_value_numerals_are_kept_visible_as_pending_not_halted(self):
        for text, tokens in (("H3仍需核查。", ["H3"]), ("DDR5与LPDDR5X需求回升。", ["DDR5", "LPDDR5X"]),
                             ("5G与B2B业务。", ["5G", "B2B"]), ("连续增长3年。", ["3"]), ("有两点值得注意。", ["两点"]),
                             ("前20大客户。", ["20"]), ("PCIe5.0产品。", ["PCIe5.0"]), ("2026-09-27发布。", ["2026-09-27"])):
            with self.subTest(text=text):
                value, ctx = context(report(text))
                self.assertEqual(tokens, pending_numbers(ctx))
                self.assertEqual("PARTIAL", ctx.evidence_check()["status"])
                self.assertEqual(text, ctx.block(value["summary"])["text"])

    def test_value_positions_still_refuse_the_whole_final_answer(self):
        for text in ("收入26亿元增长。", "同比增长20。", "毛利率从18下降到15。", "利润率6.8。", "市值1,163。", "估值20x。",
                     "收入12345。", "规模3bn。", "PE为20。", "百分之十。", "EPS3.44。", "三成。", "两倍。", "收益三点。",
                     "价格2030元。", "EPS为2025年。", "股息（2025年6月30日）美元。", "EPS=20个交易日。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                context(report(text))

    def test_every_numeral_the_strict_contract_refused_is_refused_or_pending(self):
        from finauditgate.application.research_narrative import NarrativeContext
        samples = ("H3仍需核查。", "DDR5需求。", "连续增长3年。", "有两点值得注意。", "未来6个月。", "Q40数据。", "UFS9.9已量产。",
                   "收入26亿元。", "同比增长20。", "三成。", "2026-09-27发布。", "one-off影响。", "约〇点五。")
        for text in samples:
            with self.subTest(text=text):
                _, ctx = context(report("保留判断。"))
                with self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                    NarrativeContext._literal(ctx, ctx._checked(text))  # the pre-change contract
                try:
                    self.assertTrue(ctx.unbound_numbers(text))
                except ValueError as exc:
                    self.assertEqual("UNBOUND_RESEARCH_NUMBER", str(exc))

    def test_published_v13_narrative_contract_is_unchanged(self):
        from finauditgate.application.research_narrative import NarrativeContext
        draft, calculations = inputs()
        ctx = NarrativeContext(draft, calculations, sources(), [], {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12})
        for text in ("H3仍需核查。", "DDR5需求。", "有两点值得注意。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                ctx.text(text)

    def test_quarter_marker_is_not_a_financial_quantity_escape(self):
        value, ctx = context(report("Q4实际数据尚未披露，Q1业绩仍需核查。"))
        self.assertIn("Q4实际", ctx.block(value["summary"])["text"])
        for text in ("EPS=Q4美元", "EPS为Q4美元", "利润率Q4%", "EPS为999美元", "EPS为九百美元", "Q4实际数据为999元"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                context(report(text))
        _, ctx = context(report("Q40数据"))
        self.assertEqual(["Q40"], pending_numbers(ctx))

    def test_legacy_projection_maps_quote_ids_without_claiming_the_quote_valid(self):
        raw = report("保留研究判断。{{source:Q1}}")
        raw["summary"]["evidence_refs"] = ["Q1"]
        raw["source_quotes"] = [{"id": "Q1", "source_id": "S01", "quote": "Revenue: 999 million USD; fabricated."}]
        value, ctx = context(raw, legacy=True)
        self.assertEqual(["S01"], value["summary"]["evidence_refs"])
        self.assertEqual("PARTIAL", ctx.evidence_check()["status"])
        self.assertIn("保留研究判断", ctx.block(value["summary"])["text"])
        self.assertNotIn("999", ctx.block(value["summary"])["text"])
        self.assertEqual(raw["source_quotes"], value["source_quotes"])
        with self.assertRaises(ValueError):
            context(raw)

    def test_unused_explicit_legacy_quotation_failure_stays_visible(self):
        raw = report("已保存完整研究，但引用清单仍须核对。")
        raw["source_quotes"] = [{"id": "Q1", "source_id": "S01", "quote": "Invented source text with 999 million USD."}]
        _, ctx = context(raw, legacy=True)
        self.assertEqual("PARTIAL", ctx.evidence_check()["status"])
        self.assertEqual("source_quotes[0]", ctx.evidence_check()["findings"][0]["field"])

    def test_known_source_does_not_certify_unbound_amounts_or_invented_metrics(self):
        for text in ("{{source:E0001}} 所以目标价999美元。", "{{metric:F9:eps_per_traded_unit}}", "{{metric:F1:made_up}}"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                context(report(text))

    def test_source_content_hash_conflict_is_still_a_hard_error(self):
        bundle = sources()
        bundle["sources"][0]["content"] += " changed"
        with self.assertRaisesRegex(ValueError, "THESIS_SOURCE_RECORD_INVALID"):
            context(report("{{source:E0001}}"), bundle)

    def test_explicit_source_ids_win_over_evidence_aliases_in_the_source_field(self):
        bundle = sources()
        other = sources("Second source contains distinct SYNTHETIC evidence.")["sources"][0]
        other["id"] = "E0001"
        bundle["sources"].append(other)
        raw = report("保留研究。")
        raw["summary"]["evidence_refs"] = ["E0001"]
        value, ctx = context(raw, bundle)
        self.assertEqual(["E0001"], value["summary"]["evidence_refs"])
        self.assertEqual("COMPLETED", ctx.evidence_check()["status"])
        raw["summary"]["text"] += "{{source:E0001}}"
        value, _ = context(raw, bundle)
        self.assertEqual(["E0001", "S01"], value["summary"]["evidence_refs"])
        other["use"] = "sensitivity"
        value, ctx = context(raw, bundle)
        self.assertEqual(["E0001", "S01"], value["summary"]["evidence_refs"])
        self.assertEqual("PARTIAL", ctx.evidence_check()["status"])
        self.assertTrue(any(f["reference"] == "E0001" and f["reason"] == "THESIS_UNKNOWN_SOURCE_REFERENCE"
                            for f in ctx.evidence_check()["findings"]))
        bundle["sources"].pop()
        value, ctx = context(raw, bundle)
        self.assertEqual(["E0001", "S01"], value["summary"]["evidence_refs"])
        self.assertEqual("PARTIAL", ctx.evidence_check()["status"])

    def test_evidence_check_is_stable_during_rendering(self):
        value, ctx = context(report("{{source:E0001}}；{{source:E9999}}"))
        check = ctx.evidence_check()
        for _ in range(3):
            ctx.block(value["summary"])
        self.assertEqual(check, ctx.evidence_check())
