"""Closed numerical references with exact source support and adversarial prose."""

from copy import deepcopy
import unittest

from test_research_numbers import inputs, block
from finauditgate.application.research_narrative import NarrativeContext


def context(quotes=None, sources=None):
    return NarrativeContext(*inputs(), sources or {"sources": [{"id": "S01", "content":
        "SYNTHETIC annual disclosure. FY2025 consolidated revenue: 100 million USD; operating margin: 20%."}]}, quotes or [],
        {"symbol": "000001.SZ", "as_of": "2026-12-31", "horizon_months": 12})


class ResearchNarrativeTest(unittest.TestCase):
    def test_effective_metrics_cannot_be_retyped_or_relabelled(self):
        ctx = context()
        text = ctx.text("有效盈利为 {{metric:F1:eps_per_traded_unit}}。")
        for value in ("EPS", "1.3", "USD", "2027-01-01", "2027-12-31"):
            self.assertIn(value, text)
        for value in ("EPS为999美元", "EPS为1.7美元", "EPS为９９９美元", "EPS为٩٩٩美元", "EPS为九百九十九美元", "EPS为零。", "EPS is nine hundred dollars", "EPS=&#57;&#57;", "EPS=Ⅸ", "利润率百分之二十"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ctx.text(value)

    def test_qualitative_prose_and_valid_identifiers_remain_usable(self):
        self.assertIn("F1", context().text("F1 与 S01 的经营口径一致；一方面关注现金，另一方面关注盈利。"))
        self.assertIn("G&amp;A", context().text("费用改善部分来自G&A下降，其可持续性尚需判断。"))
        self.assertIn("2026-12-31", context().text("截止日 {{context:as_of}}，证券 {{context:symbol}}。"))
        self.assertIn("000001.SZ", context().text("{{context:symbol}}"))

    def test_historical_numbers_keep_exact_source_and_locator(self):
        quote = "FY2025 consolidated revenue: 100 million USD; operating margin: 20%."
        ctx = context([{"id": "Q1", "source_id": "S01", "quote": quote}])
        text = ctx.text("历史参考：{{source:Q1}}。未来兑现仍需判断。")
        self.assertIn(quote, text)
        self.assertIn("S01，字符 29–", text)
        self.assertIn("非本次有效预测", text)

    def test_quotes_cannot_invent_truncate_ambiguously_or_read_sensitivity_sources(self):
        for quote in ("FY2025 revenue: 999 million USD.", "100"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": quote}])
        for source in ({"id": "S01", "content": "repeated source text; repeated source text"},
                       {"id": "S01", "content": "repeated source text", "use": "sensitivity"}):
            with self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": "repeated source text"}], {"sources": [source]})

    def test_unknown_null_and_malformed_references_never_supply_fallback_numbers(self):
        ctx = context()
        for text in ("{{metric:F9:eps_per_traded_unit}}", "{{metric:F1:invented_target}}", "{{source:Q9}}", "{{context:question}}", "{{metric:F1:eps_per_traded_unit:value=999}}", "{metric:F1:eps_per_traded_unit}"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ctx.text(text)
        ctx.calculations["scenario_results"][0]["eps_per_traded_unit"] = None
        self.assertIn("未能计算", ctx.text("{{metric:F1:eps_per_traded_unit}}"))

    def test_source_markup_is_escaped_and_never_recursively_interpreted(self):
        quote = "quoted {{metric:F1:revenue}} <script>1</script> [link](bad)"
        text = context([{"id": "Q1", "source_id": "S01", "quote": quote}], {"sources": [{"id": "S01", "content": quote}]}).text("{{source:Q1}}")
        self.assertNotIn("<script>", text)
        self.assertIn("\\{\\{metric", text)
        self.assertNotIn("100 百万元", text)

    def test_quote_reference_is_visible_even_with_empty_evidence_list(self):
        quote = "FY2025 consolidated revenue: 100 million USD; operating margin: 20%."
        ctx = context([{"id": "Q1", "source_id": "S01", "quote": quote}])
        value = {**block(), "text": "{{source:Q1}}", "evidence_refs": []}
        before = deepcopy(value)
        self.assertEqual(["S01"], ctx.block(value)["evidence_refs"])
        self.assertEqual(before, value)

    def test_line_wrapping_matches_without_changing_numbers_or_token_boundaries(self):
        original = "FY2025 revenue\n  100 million USD;\t operating margin 20%."
        quote = "FY2025 revenue 100 million USD; operating margin 20%."
        sources = {"sources": [{"id": "S01", "content": original}]}
        ctx = context([{"id": "Q1", "source_id": "S01", "quote": quote}], sources)
        self.assertEqual(original, ctx.quotes["Q1"]["quote"])
        self.assertIn("空白归一定位", ctx.text("{{source:Q1}}"))
        for changed in (quote.replace("100", "999"), quote.replace("100", "10 0"), quote.replace("20%", "20.0%")):
            with self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": changed}], sources)
