"""Context labels preserve prose without permitting unbound financial values."""

import unittest

from test_research_delivery import context, pending_numbers, report, sources
from test_research_numbers import inputs
from finauditgate.application.research_delivery import normalize_report, report_context


def hypothesis_context(text, hypotheses):
    bundle = sources()
    value = normalize_report(report(text), bundle)
    request = {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12,
               "hypotheses": list(hypotheses)}
    checked = report_context(value, *inputs(), bundle, request, changes=[], beliefs=[])
    return value, checked


class ReportContextLabelsTest(unittest.TestCase):
    def test_fiscal_and_half_year_labels_preserve_the_exact_original_prose(self):
        for text in (
            "FY2024的经营表现仍需核对来源。",
            "2024年NAND/DRAM的产品变化仍需核对来源。",
            "2024H2与2025H1的业务口径不能直接混用。",
            "2025H1的变化不证明此后持续增长。",
        ):
            with self.subTest(text=text):
                value, checked = context(report(text))
                self.assertEqual(text, checked.block(value["summary"])["text"])
                self.assertEqual([], checked.evidence_check()["bindings"])

    def test_report_year_and_coordinated_year_are_time_labels_only(self):
        for text in (
            "2025半年报只能说明对应披露期间。",
            "比较2024和2025H1的披露，需要分清全年和半年口径。",
            "2024和2025H1的观察仍不构成未来保证。",
        ):
            with self.subTest(text=text):
                value, checked = context(report(text))
                self.assertEqual(text, checked.block(value["summary"])["text"])
                self.assertEqual([], checked.evidence_check()["bindings"])

    def test_registered_hypothesis_indices_preserve_text_without_certifying_the_hypothesis(self):
        cases = ((["SYNTHETIC_ASSUMPTION_A"], "假设1仍是待检验判断。"),
                 (["SYNTHETIC_ASSUMPTION_A", "SYNTHETIC_ASSUMPTION_B"],
                  "假设1需观察经营；假设2需检查反证。"),
                 (["SYNTHETIC_ASSUMPTION_A", "SYNTHETIC_ASSUMPTION_B", "SYNTHETIC_ASSUMPTION_C"],
                  "假设3尚未获得充分支持。"))
        for hypotheses, text in cases:
            with self.subTest(count=len(hypotheses), text=text):
                value, checked = hypothesis_context(text, hypotheses)
                self.assertEqual(text, checked.block(value["summary"])["text"])
                self.assertEqual([], checked.evidence_check()["bindings"])

    def test_label_syntax_cannot_hide_financial_values_or_invalid_periods(self):
        for text in (
            "EPS=FY2024。", "EPS为FY2024。", "EPS=（FY2024）。",
            "EPS为2024年NAND。", "2024年USD。",
            "FY2024美元。", "利润率2025H1%。", "收益为2024H2元。",
            "2025半年报的利润率为999%。", "实际收益为999元。",
            "现金2024和2025H1元。",
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                context(report(text))
        # Invalid period labels are not values; they stay visible and pending.
        for text, tokens in (("2025H3的披露需要核对。", ["2025H3"]), ("2025H10的经营情况。", ["2025H10"]),
                             ("FY20245的经营情况。", ["FY20245"])):
            with self.subTest(text=text):
                self.assertEqual(tokens, pending_numbers(context(report(text))[1]))

    def test_hypothesis_index_requires_registration_and_cannot_be_a_quantity(self):
        cases = (([], "假设1仍需核查。"),
                 (["SYNTHETIC_ASSUMPTION_A"], "假设2仍需核查。"),
                 (["SYNTHETIC_ASSUMPTION_A", "SYNTHETIC_ASSUMPTION_B"], "假设3仍需核查。"))
        for hypotheses, text in cases:
            with self.subTest(count=len(hypotheses), text=text):
                self.assertEqual(1, len(pending_numbers(hypothesis_context(text, hypotheses)[1])))
        registered = ["SYNTHETIC_ASSUMPTION_A", "SYNTHETIC_ASSUMPTION_B"]
        for text, token in (("假设99仍需核查。", "99"), ("假设0尚未验证。", "0")):
            with self.subTest(text=text):
                self.assertEqual([token], pending_numbers(hypothesis_context(text, registered)[1]))
        for text in ("假设1元。", "EPS=假设1。", "利润率假设1%。", "假设1下的收入为999元。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                hypothesis_context(text, registered)


if __name__ == "__main__":
    unittest.main()
