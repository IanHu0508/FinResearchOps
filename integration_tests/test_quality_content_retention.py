"""Protect substantive prose when a bounded correction replaces one span.

Provider replies are synthetic. The length guard detects gross content loss,
not semantic correctness or the truth of a model's research assumptions.
"""

import json
from pathlib import Path
import unittest

import test_thesis_quality_flow as flow


class RetentionLLM(flow.QualityLLM):
    retention_mode: str = "summary_collapse"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind not in ("QualityReview", "QualityRevision"):
            return result
        payload = json.loads(messages[-1].content)
        value = json.loads(result.generations[0].message.content)
        if kind == "QualityReview":
            finding = value["findings"][0]
            if self.retention_mode == "earnings_collapse":
                target = "financial_analysis.earnings_quality.text"
                finding.update(path=target, original_text=payload["editable_text"][target],
                    issue="合成盈利质量段落须保留观察条件，而非直接认可持续性。")
            elif self.retention_mode == "narrow_assumption":
                finding.update(original_text="经营判断保持",
                    issue="应明确该经营判断属于待后续事实验证的条件假设。")
            elif self.retention_mode == "withdraw_unsupported_reference":
                finding.update(original_text="历史资料{{source:E0001}}",
                    issue="该历史片段不能单独支持情景假设，应撤回这种支持关系。")
        else:
            finding = payload["assessment"]["findings"][0]
            replacement = "待核。"
            if self.retention_mode == "selector_padding":
                replacement += "{{source:E0001}}" * 20 + "\n " * 20
            elif self.retention_mode == "narrow_assumption":
                replacement = "经营判断属于条件假设，仍须用后续经营事实验证"
            elif self.retention_mode == "withdraw_unsupported_reference":
                replacement = "该历史资料不足以单独验证情景假设，需保留后续检验条件"
            value["edits"][0].update(path=finding["path"],
                expected_text=finding["original_text"], replacement_text=replacement)
        return self._result(json.dumps(value, ensure_ascii=False))


class QualityContentRetentionTest(unittest.TestCase):
    setUp = flow.ThesisQualityFlowTest.setUp
    run_case = flow.ThesisQualityFlowTest.run_case
    assert_original_readable = flow.ThesisQualityFlowTest.assert_original_readable
    assert_quality_failed_without_losing_main = flow.ThesisQualityFlowTest.assert_quality_failed_without_losing_main

    def assert_collapse_is_nonblocking(self, mode):
        view, app, model = self.run_case(RetentionLLM(retention_mode=mode))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual("THESIS_QUALITY_RESEARCH_CONTENT_COLLAPSED", view.review["error_code"])
        self.assertEqual(["QualityReview", "QualityRevision"],
            [request["schema"] for request in model.quality_requests])
        self.assertEqual(15, len(model.requests))
        self.assertFalse(Path(view.report_path).with_name("quality-report.md").exists())
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_whole_summary_cannot_be_replaced_by_a_placeholder(self):
        self.assert_collapse_is_nonblocking("summary_collapse")

    def test_whole_earnings_section_cannot_be_replaced_by_a_placeholder(self):
        self.assert_collapse_is_nonblocking("earnings_collapse")

    def test_repeated_selectors_and_whitespace_cannot_disguise_content_loss(self):
        self.assert_collapse_is_nonblocking("selector_padding")

    def test_narrow_assumption_qualification_preserves_other_analysis(self):
        view, app, model = self.run_case(RetentionLLM(retention_mode="narrow_assumption"))
        self.assert_original_readable(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        effective = view.review["effective"]
        expected = flow.SUMMARY.replace("经营判断保持", "经营判断属于条件假设，仍须用后续经营事实验证")
        self.assertEqual(expected, effective["final_report"]["summary"]["text"])
        self.assertEqual(view.latest_report["final_report"]["financial_analysis"],
            effective["final_report"]["financial_analysis"])
        self.assertEqual(view.latest_report["effective_forward_draft"], effective["effective_forward_draft"])
        self.assertEqual(view.latest_report["effective_forward_calculations"], effective["effective_forward_calculations"])
        self.assertEqual(2, len(model.quality_requests))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_unsupported_reference_can_be_withdrawn_without_erasing_correct_analysis(self):
        view, app, _ = self.run_case(RetentionLLM(retention_mode="withdraw_unsupported_reference"))
        self.assert_original_readable(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        expected = flow.SUMMARY.replace("历史资料{{source:E0001}}",
            "该历史资料不足以单独验证情景假设，需保留后续检验条件")
        corrected = view.review["effective"]["final_report"]["summary"]["text"]
        self.assertEqual(expected, corrected)
        self.assertIn("{{metric:F1:eps_per_traded_unit}}", corrected)
        self.assertEqual(view, app.read_case(view.case_ref))


if __name__ == "__main__":
    unittest.main()
