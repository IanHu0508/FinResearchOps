"""Protocol 24 helpers: hiding the numerals the contract refuses in a final report.

Everything here is synthetic and runs with the standard library only. These checks
decide which characters are hidden and when hiding cannot make a report pass; they
say nothing about the quality of any answer.
"""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from test_number_contract_v2 import bundle
from test_research_delivery import report
from test_research_numbers import inputs
from finauditgate.adapters.thesis_analysts import main_stages
from finauditgate.adapters.thesis_degrade import case_status
from finauditgate.adapters.thesis_masking import MARK, SENTENCE_MARK, mask_numbers
from finauditgate.adapters.tradingagents_thesis import select_protocol
from finauditgate.application.research_delivery import contract_for, normalize_report, report_context


REQUEST = {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12}
VALUE = "收入为12.5亿元。"
SCHEMAS = Path(__file__).parents[1] / "schemas"


def masked(value):
    return mask_numbers(normalize_report(value, bundle()), *inputs(), bundle(), REQUEST, changes=[], beliefs=[])


def check(value):
    return report_context(value, *inputs(), bundle(), REQUEST, changes=[], beliefs=[], contract=2)


class MaskNumbersTest(unittest.TestCase):
    def test_only_refused_sentences_change_and_the_masked_report_passes(self):
        value = report("经营改善。" + VALUE + "现金仍需观察。")
        value["limitations"].append("毛利率为35%。")
        result, rows = masked(value)
        self.assertEqual("经营改善。收入为" + MARK + "元。现金仍需观察。", result["summary"]["text"])
        self.assertEqual([{"field": "summary", "original": VALUE, "masked": "收入为" + MARK + "元。", "mode": "numbers"},
                          {"field": "limitations[1]", "original": "毛利率为35%。", "masked": "毛利率为" + MARK + "。",
                           "mode": "numbers"}], rows)
        check(result)
        with self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
            check(normalize_report(value, bundle()))
        restored = deepcopy(result)
        restored["summary"]["text"], restored["limitations"][1] = value["summary"]["text"], "毛利率为35%。"
        self.assertEqual(normalize_report(value, bundle()), restored)

    def test_numerals_the_contract_accepts_stay_and_citations_are_kept(self):
        cases = (("2025年6月末现金为3亿元，仍需核对。", "2025年6月末现金为" + MARK + "元，仍需核对。"),
                 ("截至2025年12月31日，负债为12亿元。", "截至2025年12月31日，负债为" + MARK + "元。"),
                 ("2025年1-9月收入3亿元。", "2025年1-9月收入" + MARK + "元。"),
                 ("2025-06-30现金3亿元。", "2025-06-30现金" + MARK + "元。"),
                 ("Q3收入为3亿元，S01显示毛利率为35%。", "Q3收入为" + MARK + "元，S01显示毛利率为" + MARK + "。"),
                 ("价格低于10EMA，收入为5亿元{{source:E0001}}。", "价格低于10EMA，收入为" + MARK + "元{{source:E0001}}。"),
                 ("增长率为+12.5%，而2024年为−3%。", "增长率为" + MARK + "，而2024年为" + MARK + "。"),
                 ("收入为1,234.5万元，另有两家工厂。", "收入为" + MARK + "元，另有两家工厂。"),
                 ("财务费用降幅约一成半，收入超过十亿。", "财务费用降幅约" + MARK + "，收入超过" + MARK + "。"),
                 ("①收入为三亿元，②利润改善。", MARK + "收入为" + MARK + "元，" + MARK + "利润改善。"))
        for text, expected in cases:
            with self.subTest(text=text):
                result, [row] = masked(report(text))
                self.assertEqual((expected, "numbers"), (result["summary"]["text"], row["mode"]))
                check(result)

    def test_each_sentence_is_judged_with_the_prose_before_it(self):
        # On its own the count is only pending; after "收入！" the contract reads it as an amount.
        self.assertIsNone(masked(report("为12家工厂。")))
        result, rows = masked(report("关于收入！为12家工厂。经营改善。"))
        self.assertEqual("关于收入！为" + MARK + "家工厂。经营改善。", result["summary"]["text"])
        self.assertEqual([{"field": "summary", "original": "为12家工厂。", "masked": "为" + MARK + "家工厂。", "mode": "numbers"}], rows)
        check(result)

    def test_a_numeral_refused_by_the_text_after_it_is_the_one_hidden(self):
        # The contract reads a line break as space: "5\n元" is an amount, and the next line is not to blame.
        for text, expected in (("主要工厂有5\n元的在建投入待核。", "主要工厂有" + MARK + "\n元的在建投入待核。"),
                               ("关键客户共3\n利润改善。", "关键客户共" + MARK + "\n利润改善。")):
            with self.subTest(text=text):
                result, [row] = masked(report(text))
                self.assertEqual((expected, text.split("\n")[0] + "\n", "numbers"),
                                 (result["summary"]["text"], row["original"], row["mode"]))
                check(result)

    def test_a_citation_never_splits_a_sentence(self):
        result, [row] = masked(report("收入为12.5亿元{{source:E0001！}}。"))
        self.assertEqual(("收入为" + MARK + "元{{source:E0001！}}。", "收入为12.5亿元{{source:E0001！}}。"),
                         (result["summary"]["text"], row["original"]))

    def test_numerals_attached_to_letters_are_hidden_only_when_refused(self):
        cases = (("价格低于12.5EMA，收入为5亿元{{source:E0001}}。", "价格低于" + MARK + "，收入为" + MARK + "元{{source:E0001}}。"),
                 ("公司重点布局5G\n收入结构仍以传统业务为主。", "公司重点布局" + MARK + "\n收入结构仍以传统业务为主。"),
                 ("FY2025收入为5亿元，28nm产能提升。", "FY2025收入为" + MARK + "元，28nm产能提升。"),
                 ("毛利率为35%，３EPS5。", "毛利率为" + MARK + "，３EPS5。"))
        for text, expected in cases:
            with self.subTest(text=text):
                result, rows = masked(report(text))
                self.assertEqual((expected, {"numbers"}), (result["summary"]["text"], {r["mode"] for r in rows}))
                check(result)

    def test_hiding_a_numeral_never_exposes_a_number_word(self):
        for tail in ("用户约²million。", "用户约三million。"):
            with self.subTest(tail=tail):
                result, rows = masked(report("毛利率为35%。\n" + tail))
                self.assertEqual(("毛利率为" + MARK + "。\n" + tail, ["毛利率为35%。"]),
                                 (result["summary"]["text"], [r["original"] for r in rows]))

    def test_text_no_span_covers_is_hidden_whole_and_labelled_so(self):
        from unittest.mock import patch
        import finauditgate.adapters.thesis_masking as masking
        spots = masking._spots
        # Simulate a refusal that no numeral-like span covers: the text is hidden whole, its citation kept.
        with patch.object(masking, "_spots", side_effect=lambda text: [] if "EMA" in text else spots(text)):
            result, rows = masked(report("经营改善。价格低于12.5EMA，收入为5亿元{{source:E0001}}。2025年6月末现金为3亿元。"))
        self.assertEqual("经营改善。" + SENTENCE_MARK + "{{source:E0001}}。2025年6月末现金为" + MARK + "元。", result["summary"]["text"])
        self.assertEqual(["sentence", "numbers"], [r["mode"] for r in rows])
        check(result)
        # The label follows what was replaced, not marks the model wrote itself.
        result, [row] = masked(report("价格低于12.5EMA" + SENTENCE_MARK + "。"))
        self.assertEqual(("价格低于" + MARK + SENTENCE_MARK + "。", "numbers"), (result["summary"]["text"], row["mode"]))

    def test_clean_reports_and_other_refusals_are_not_masked(self):
        self.assertIsNone(masked(report("经营改善，仍需观察。")))
        self.assertIsNone(masked(report("2025年上半年的收入增长仍需核对。")))
        other_error = report(VALUE)
        other_error["limitations"].append("另见{{bogus:x}}。")
        self.assertIsNone(masked(other_error))
        uncovered = report(VALUE)
        uncovered["belief_explanations"] = [{"belief_id": "D1", "explanation": {"text": "信念。"}}]
        self.assertIsNone(masked(uncovered))

    def test_masking_is_deterministic_and_idempotent(self):
        value = report("经营改善。" + VALUE + "2025年前三季度利润为3亿元，同比增长20%。")
        first, rows = masked(value)
        self.assertEqual((first, rows), masked(deepcopy(value)))
        self.assertIsNone(masked(first))

class SourceFactsTest(unittest.TestCase):
    """The contract reads what it needs from the sources once per context; every check keeps its result."""

    def test_source_labels_are_read_once_and_still_decide(self):
        from unittest.mock import patch
        from finauditgate.application import research_delivery
        from finauditgate.core.artifacts import sha256_hex
        from test_number_contract_v2 import pending
        sources = bundle()
        body = "SYNTHETIC 新闻索引，抓取时间：2026-01-02。UFS 4.1。"
        sources["sources"].append({"id": "S10", "content": body, "sha256": sha256_hex(body.encode()), "use": "research"})
        self.assertEqual([], pending("资料抓取于2026-01-02。", 2, sources))
        self.assertEqual(["2026-01-03"], pending("资料抓取于2026-01-03。", 2, sources))
        self.assertEqual(["2026-01-02"], pending("资料抓取于2026-01-02。", 2))
        context = research_delivery.DeliveryContext(*inputs(), sources, report("x"), REQUEST, contract=2)
        with patch.object(research_delivery.unicodedata, "normalize", wraps=research_delivery.unicodedata.normalize) as normalize:
            for _ in range(3):
                context.unbound_numbers("资料抓取于2026-01-02，UFS 4.1已在资料中出现，价格低于10EMA。")
        contents = [source["content"] for source in context.sources.values()]
        self.assertEqual(sorted(contents), sorted(c.args[1] for c in normalize.call_args_list if c.args[1] in contents))


class ProtocolV24Test(unittest.TestCase):
    def test_new_four_analyst_runs_use_protocol_24_with_the_v23_stages_and_contract(self):
        self.assertEqual(24, select_protocol(None, True))
        self.assertEqual(16, select_protocol(None, False))
        self.assertEqual(main_stages(23), main_stages(24))
        self.assertEqual(2, contract_for({"schema_version": "finresearchops.thesis-case/v24"}))

    def test_a_masked_case_is_partial(self):
        self.assertEqual("PARTIAL", case_status([], "COMPLETED", masked=True))
        self.assertEqual("COMPLETED", case_status([], "COMPLETED"))

    def test_the_v24_schema_changes_only_what_masking_needs(self):
        v23 = json.loads((SCHEMAS / "thesis-case.v23.schema.json").read_text())
        v24 = json.loads((SCHEMAS / "thesis-case.v24.schema.json").read_text())
        expected = json.loads(json.dumps(v23).replace("SAME_V23_FLOW", "SAME_V24_FLOW"))
        expected["title"], expected["properties"]["schema_version"] = v24["title"], {"const": "finresearchops.thesis-case/v24"}
        expected["required"].insert(expected["required"].index("number_repair") + 1, "number_masking")
        expected["properties"]["number_masking"] = v24["properties"]["number_masking"]
        expected["allOf"][2]["if"]["properties"]["number_masking"] = {"type": "null"}
        expected["allOf"].append({"if": {"properties": {"number_masking": {"type": "object"}}},
                                  "then": {"properties": {"status": {"const": "PARTIAL"}, "number_repair": {"type": "null"}}}})
        attempt = expected["properties"]["recovery"]["properties"]["attempts"]["items"]
        attempt["properties"]["retry_run_id"] = v24["properties"]["recovery"]["properties"]["attempts"]["items"]["properties"]["retry_run_id"]
        attempt["allOf"] = [{"if": {"properties": {"reason": {"not": {"const": "NUMBER_REPAIR"}}}},
                             "then": {"properties": {"retry_run_id": {"type": "string"}}}}]
        # The masked text may outgrow the model answer's limits by its marks.
        for definition, key in (("ResearchBlock", "text"), ("ScenarioUse", "reason"), ("ScenarioUse", "what_changes_the_view")):
            field = expected["$defs"][definition]["properties"][key]
            del field["maxLength"]
            field["description"] = v24["$defs"][definition]["properties"][key]["description"]
        self.assertEqual(expected, v24)
        masking = v24["properties"]["number_masking"]
        rows = masking["oneOf"][1]["properties"]["sentences"]
        self.assertEqual(({"type": "null"}, 1, ["numbers", "sentence"], {"type": ["string", "null"], "minLength": 1}),
                         (masking["oneOf"][0], rows["minItems"], rows["items"]["properties"]["mode"]["enum"],
                          {k: v for k, v in attempt["properties"]["retry_run_id"].items() if k != "description"}))
        halted = json.loads((SCHEMAS / "thesis-halted-case.v1.schema.json").read_text())
        self.assertEqual({"enum": [23, 24]}, halted["properties"]["protocol_version"])

    def test_the_schema_subset_checker_refuses_what_it_cannot_read(self):
        from schema_subset import schema_errors
        self.assertEqual([], schema_errors({"a": 1}, {"type": "object", "properties": {"a": {"const": 1}}}))
        self.assertEqual([(("a",), "const")], schema_errors({"a": True}, {"properties": {"a": {"const": 1}}}))
        self.assertEqual([((), "not")], schema_errors("x", {"not": {"const": "x"}}))
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SCHEMA_KEYWORDS"):
            schema_errors({}, {"patternProperties": {}})

if __name__ == "__main__":
    unittest.main()
