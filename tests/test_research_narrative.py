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

    def test_cjk_prose_linewrap_preserves_source_offsets_and_input(self):
        for newline in ("\n", "\r\n"):
            original = f"合成披露，经营活动产生的现金{newline}流量为100.50万元，仍须核验。"
            quote = original.replace(newline, "")
            sources = {"sources": [{"id": "S01", "content": "前置说明。" + newline * 2 + original + newline * 2 + "后置说明。"}]}
            quotes = [{"id": "Q1", "source_id": "S01", "quote": quote}]
            before = deepcopy((sources, quotes))
            with self.subTest(newline=newline):
                ctx = context(quotes, sources)
                bound = ctx.quotes["Q1"]
                self.assertEqual(original, bound["quote"])
                self.assertEqual(original, sources["sources"][0]["content"][bound["start"]:bound["end"]])
                self.assertEqual("cjk_linewrap", bound["matching"])
                self.assertIn("中文断行兼容定位", ctx.text("资料：{{source:Q1}}"))
                self.assertEqual(before, (sources, quotes))

    def test_cjk_linewrap_accepts_mixed_retained_and_omitted_wraps(self):
        original = "合成披露，经营活动产生的现金\n流量保持稳定，经营回款\n情况仍需观察，不能外推。"
        quote = original.replace("现金\n流量", "现金流量").replace("回款\n情况", "回款 情况")
        ctx = context([{"id": "Q1", "source_id": "S01", "quote": quote}],
                      {"sources": [{"id": "S01", "content": original}]})
        self.assertEqual(original, ctx.quotes["Q1"]["quote"])

    def test_cjk_linewrap_cannot_join_numeric_or_latin_tokens(self):
        for broken in ("1\n000", "１\n０００", "一\n百", "-\n12", "1\n.2", "20\n%", "cash\nflow"):
            source = f"合成披露，相关数值为{broken}万元，仍需核验。"
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": source.replace("\n", "")}],
                        {"sources": [{"id": "S01", "content": source}]})

    def test_cjk_linewrap_cannot_erase_layout_boundaries(self):
        for separator in ("\n\n", "\n \n", "\n  ", "\t", "\f", "\u2028", "\u2029"):
            source = "合成披露，经营活动产生的现金" + separator + "流量稳定，仍须核验。"
            with self.subTest(separator=separator), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": source.replace(separator, "")}],
                        {"sources": [{"id": "S01", "content": source}]})
        for source, quote in (
            ("本期情况良好。\n合成披露，经营活动产生的现金\n流量稳定，仍须核验。",
             "本期情况良好。 合成披露，经营活动产生的现金流量稳定，仍须核验。"),
            ("|合成披露，经营活动产生的现金\n流量稳定，仍须核验。|",
             "合成披露，经营活动产生的现金流量稳定，仍须核验。"),
            ("营业收入\n归母净利润\n经营现金流量", "营业收入归母净利润经营现金流量"),
            ("项目：经营现金\n流量：待核验金额", "项目：经营现金流量：待核验金额"),
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": quote}],
                        {"sources": [{"id": "S01", "content": source}]})

    def test_cjk_linewrap_cannot_skip_page_furniture_or_other_text(self):
        for middle in ("[[PAGE 2]]", "第二节 财务报告", "另有需要核对的实质说明。", "1 / 20", "脚注：口径不同。"):
            source = f"合成披露，经营活动产生的现金\n{middle}\n流量稳定，仍須核验。"
            quote = "合成披露，经营活动产生的现金流量稳定，仍須核验。"
            with self.subTest(middle=middle), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": quote}],
                        {"sources": [{"id": "S01", "content": source}]})

    def test_cjk_linewrap_rejects_paragraph_padding_and_unicode_tables(self):
        paragraph = "合成披露，经营活动产生的现金\n流量稳定，仍须核验。"
        for ending in ("。", "。”", ".", "!", "："):
            for padding in (" ", "\u00a0"):
                for newline in ("\n", "\r\n"):
                    source = "上一段情况良好" + ending + padding + newline + paragraph
                    quote = source.replace(padding + newline, padding).replace("现金\n流量", "现金流量")
                    with self.subTest(ending=ending, padding=padding, newline=newline), self.assertRaises(ValueError):
                        context([{"id": "Q1", "source_id": "S01", "quote": quote}],
                                {"sources": [{"id": "S01", "content": source}]})
        for delimiter in ("│", "┃", "║", "｜", "¦", "∣", "∥"):
            source = delimiter + paragraph + delimiter
            with self.subTest(delimiter=delimiter), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": paragraph.replace("\n", "")}],
                        {"sources": [{"id": "S01", "content": source}]})

    def test_cjk_linewrap_cannot_change_nonwhitespace_or_add_spaces(self):
        original = "合成披露，经营活动产生的现金\n流量为100.50万元，仍须核验。"
        joined = original.replace("\n", "")
        for quote in (joined.replace("100.50", "10050"), joined.replace("100.50", "100.5"),
                      joined.replace("万元", "元"), joined.replace("，", "；", 1),
                      joined.replace("经营活动", "经营 活动")):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                context([{"id": "Q1", "source_id": "S01", "quote": quote}],
                        {"sources": [{"id": "S01", "content": original}]})

    def test_cjk_linewrap_requires_unique_nonwhitespace_occurrence(self):
        original = "合成披露，经营活动产生的现金\n流量稳定，仍须核验。"
        source = original + "\n\n" + original.replace("\n", "\r\n")
        with self.assertRaises(ValueError):
            context([{"id": "Q1", "source_id": "S01", "quote": original.replace("\n", "")}],
                    {"sources": [{"id": "S01", "content": source}]})
