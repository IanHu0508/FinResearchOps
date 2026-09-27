"""Narrow registered metadata labels never authorize financial quantities."""

import unittest

from test_research_delivery import pending_numbers, report, sources
from test_research_numbers import inputs
from finauditgate.application.research_delivery import normalize_report, report_context


def metadata_context(text, bundle=None, *, hypotheses=()):
    bundle = sources() if bundle is None else bundle
    value = normalize_report(report(text), bundle)
    request = {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12,
               "hypotheses": list(hypotheses)}
    context = report_context(value, *inputs(), bundle, request, changes=[], beliefs=[])
    return value, context


def quant_sources(content="SYNTHETIC_ONLY：预测期限为20个交易日。", *, identity="QUANT", use="research"):
    base = sources()
    row = sources(content)["sources"][0]
    row.update(id=identity, use=use)
    base["sources"].append(row)
    return base


class DeliveryMetadataContextTest(unittest.TestCase):
    def test_registered_hypothesis_aliases_preserve_original_prose(self):
        text = "H1仍待核验；H2需保留反证。"
        value, context = metadata_context(text, hypotheses=("SYNTHETIC_ASSUMPTION_A", "SYNTHETIC_ASSUMPTION_B"))
        self.assertEqual(text, context.block(value["summary"])["text"])

    def test_unknown_hypothesis_aliases_and_financial_uses_remain_unbound(self):
        hypotheses = ("SYNTHETIC_ASSUMPTION_A", "SYNTHETIC_ASSUMPTION_B")
        for text in ("EPS=H1。", "H1元。", "利润率H2%。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                metadata_context(text, hypotheses=hypotheses)
        # An unregistered label is not a value: it stays visible and pending.
        for text, token in (("H3仍需核查。", "H3"), ("H0尚未验证。", "H0")):
            with self.subTest(text=text):
                self.assertEqual([token], pending_numbers(metadata_context(text, hypotheses=hypotheses)[1]))
        self.assertEqual(["H1"], pending_numbers(metadata_context("H1尚未登记。")[1]))

    def test_sentence_initial_enumeration_is_not_a_financial_quantity(self):
        for text in ("三点提醒：来源、期间、口径。", "结论仍待核实。三点提醒：来源、期间、口径。"):
            with self.subTest(text=text):
                value, context = metadata_context(text)
                self.assertEqual(text, context.block(value["summary"])["text"])
        for text in ("收益三点。", "三点利润。", "EPS=三点提醒：仍待核实。", "收益三点提醒：仍需复核。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                metadata_context(text)

    def test_exact_research_quant_prediction_duration_is_a_registered_label(self):
        text = "20个交易日的排序不能替代年度基本面判断。"
        value, context = metadata_context(text, quant_sources())
        self.assertEqual(text, context.block(value["summary"])["text"])
        self.assertEqual([], context.evidence_check()["bindings"])
        for bundle in (sources(), quant_sources(identity="NOT_QUANT"), quant_sources(use="sensitivity"),
                       quant_sources("SYNTHETIC_ONLY：过去20个交易日的历史回报。")):
            with self.subTest(bundle=bundle):
                self.assertEqual(["20"], pending_numbers(metadata_context(text, bundle)[1]))

    def test_quant_duration_does_not_authorize_other_periods_or_returns(self):
        for text in ("真实收益20%。", "EPS=20个交易日。", "20个交易日元。", "20个交易日收益999元。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                metadata_context(text, quant_sources())
        for text in ("21天的预测。", "21个交易日的预测。"):
            with self.subTest(text=text):
                self.assertEqual(["21"], pending_numbers(metadata_context(text, quant_sources())[1]))

    def test_actual_recorded_retrieval_date_is_allowed_only_as_retrieval_metadata(self):
        content_registered = sources("SYNTHETIC_ONLY\n抓取时间：2026-09-26T09:08:07+08:00\n历史版本未认证。")
        availability_registered = sources()
        availability_registered["sources"][0]["availability_note"] = "SYNTHETIC_ONLY：抓取于2026-09-26，历史版本未认证。"
        text = "本资料抓取于2026-09-26，不能据此证明研究日可得。"
        for bundle in (content_registered, availability_registered):
            with self.subTest(bundle=bundle):
                value, context = metadata_context(text, bundle)
                self.assertEqual(text, context.block(value["summary"])["text"])
                self.assertEqual([], context.evidence_check()["bindings"])

    def test_unknown_or_financially_wrapped_retrieval_dates_are_not_authorized(self):
        registered = sources("SYNTHETIC_ONLY\n抓取时间：2026-09-26T09:08:07+08:00\n历史版本未认证。")
        for text in ("EPS=抓取于2026-09-26。", "金额为抓取于2026-09-26。", "抓取于2026-09-26元。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
                metadata_context(text, registered)
        for text, token in (("抓取于2026-09-27。", "2026-09-27"), ("发表于2026-09-26。", "2026-09-26")):
            with self.subTest(text=text):
                self.assertEqual([token], pending_numbers(metadata_context(text, registered)[1]))
        for bundle in (sources(), sources("SYNTHETIC_ONLY：该日期2026-09-26是发布日，没有登记抓取日期。")):
            with self.subTest(bundle=bundle):
                self.assertEqual(["2026-09-26"], pending_numbers(metadata_context("本资料抓取于2026-09-26。", bundle)[1]))


if __name__ == "__main__":
    unittest.main()
