"""Adversarial aftercare controls, using synthetic provider replies only.

These checks test bounded changes and artifact integrity. They do not claim
that a keyword rule or synthetic reviewer can establish financial truth.
"""

from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources
import test_thesis_quality_flow as quality_flow
from test_thesis_quality_flow import (
    CAUTIOUS_SUMMARY, QualityLLM, SUMMARY,
)
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import FinResearchOps
from finauditgate.research import ResearchThesis


class GuardLLM(QualityLLM):
    """Mutate the existing complete fixture at a single contract boundary."""

    guard_mode: str = ""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind not in ("QualityReview", "QualityRevision"):
            return result
        value = json.loads(result.generations[0].message.content)
        if kind == "QualityReview":
            if self.guard_mode == "optional":
                value["findings"][0]["impact"] = "optional"
            elif self.guard_mode == "none":
                value["findings"] = []
            elif self.guard_mode == "wrong_before":
                value["financial_changes"][0]["change"]["expected_before"] = 101.0
        elif self.guard_mode in ("not_supported", "not_supported_without_source"):
            value["edits"] = []
            value["resolutions"][0].update(outcome="not_supported",
                reason="复核提案不成立；来源和原有条件说明支持保留原句。",
                evidence_refs=["S01"] if self.guard_mode == "not_supported" else [])
        elif self.guard_mode in ("unresolved_appendix", "unresolved_body"):
            value["resolutions"][0].update(outcome="unresolved",
                reason="尚不能判定持续性；在正文保留观察条件，记录尚待核实的影响。")
            if self.guard_mode == "unresolved_appendix":
                value["edits"] = []
        elif self.guard_mode == "wording_rating":
            value["rating"]["value"] = "Sell"
        elif self.guard_mode == "outside_path":
            value["edits"][0]["path"] = "request.as_of"
        elif self.guard_mode == "unrelated_prose":
            payload = json.loads(messages[-1].content)
            path = next(p for p in payload["editable_text"] if p.startswith("financial_analysis."))
            original = payload["editable_text"][path]
            value["edits"].append({"finding_ids": ["R1"], "path": path,
                "expected_text": original, "replacement_text": original + "此处加入无关的研究说明。"})
        return self._result(json.dumps(value, ensure_ascii=False))


class QualityGuardsTest(unittest.TestCase):
    setUp = quality_flow.ThesisQualityFlowTest.setUp
    assert_original_readable = quality_flow.ThesisQualityFlowTest.assert_original_readable
    assert_quality_failed_without_losing_main = quality_flow.ThesisQualityFlowTest.assert_quality_failed_without_losing_main

    def run_case(self, model=None, *, budget=None, root=None, resume=None):
        model = model or GuardLLM()
        app = FinResearchOps(artifact_root=root or self.root, researcher=ThesisResearcher(
            live=budget is not None, budget=budget, resume_from=resume))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "完整研究与有界质量修正。",
            sources=correction_sources(), review=True)
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        return view, app, model

    def assert_no_revision_needed(self, mode):
        view, app, model = self.run_case(GuardLLM(guard_mode=mode))
        self.assert_original_readable(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual(["QualityReview"], [r["schema"] for r in model.quality_requests])
        self.assertIsNone(view.review["revision"])
        self.assertEqual(view.latest_report["final_report"], view.review["effective"]["final_report"])
        self.assertEqual(view.latest_report["effective_forward_draft"],
                         view.review["effective"]["effective_forward_draft"])
        self.assertEqual([], view.review["effective"]["unresolved_findings"])

    def test_optional_only_review_does_not_trigger_revision(self):
        self.assert_no_revision_needed("optional")

    def test_review_without_findings_does_not_trigger_revision(self):
        self.assert_no_revision_needed("none")

    def test_evidence_backed_disagreement_can_preserve_the_original_judgment(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="not_supported"))
        self.assert_original_readable(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual("not_supported", view.review["revision"]["resolutions"][0]["outcome"])
        self.assertEqual(view.latest_report["final_report"], view.review["effective"]["final_report"])
        self.assertEqual(view.latest_report["signal"], view.delivery_rating)

    def test_disagreement_without_evidence_cannot_claim_resolution(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="not_supported_without_source"))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual("THESIS_QUALITY_DISAGREEMENT_SOURCE_REQUIRED", view.review["error_code"])

    def test_unresolved_material_issue_cannot_be_closed_by_appendix_only(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="unresolved_appendix"))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual("THESIS_QUALITY_UNRESOLVED_BODY_NOT_CHANGED", view.review["error_code"])
        self.assertEqual(SUMMARY, view.latest_report["final_report"]["summary"]["text"])

    def test_unresolved_issue_with_body_qualification_remains_partial(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="unresolved_body"))
        self.assert_original_readable(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual(["R1"], view.review["effective"]["unresolved_findings"])
        self.assertEqual(CAUTIOUS_SUMMARY, view.review["effective"]["final_report"]["summary"]["text"])
        self.assertIsNone(view.delivery_rating)
        self.assertEqual("quality-report.md", Path(view.delivery_report_path).name)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_parameter_change_with_wrong_expected_before_is_rejected(self):
        view, app, model = self.run_case(GuardLLM(quality_mode="financial", guard_mode="wrong_before"))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual(["QualityReview"], [r["schema"] for r in model.quality_requests])
        self.assertEqual(100, view.latest_report["effective_forward_draft"]["scenarios"][0]["revenue"]["value"])

    def test_wording_only_findings_cannot_change_the_rating(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="wording_rating"))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual("THESIS_QUALITY_UNJUSTIFIED_RATING_CHANGE", view.review["error_code"])

    def test_edit_cannot_mutate_a_field_outside_existing_report_prose(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="outside_path"))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual("THESIS_QUALITY_EDIT_TARGET_INVALID", view.review["error_code"])

    def test_edit_cannot_use_one_finding_to_change_an_unrelated_prose_field(self):
        view, app, _ = self.run_case(GuardLLM(guard_mode="unrelated_prose"))
        self.assert_quality_failed_without_losing_main(view, app)

    def test_resume_never_redraws_successful_or_failed_quality_stages_or_resets_budget(self):
        for mode, expected_calls in (("wording", 15), ("review_timeout", 14), ("revision_error", 15)):
            with self.subTest(mode=mode):
                initial_root = self.root / mode
                budget = ModelBudget(ceiling_cny=None, max_calls=expected_calls, max_input_bytes=524288)
                original, _, _ = self.run_case(QualityLLM(quality_mode=mode), root=initial_root, budget=budget)
                self.assertEqual(expected_calls, budget.calls)
                execution = next(initial_root.glob("application/thesis-executions/*"))
                restored = ModelBudget(ceiling_cny=None, max_calls=expected_calls, max_input_bytes=524288)
                replay, app, model = self.run_case(QualityLLM(), root=self.root / (mode + "-resume"),
                                                   resume=execution, budget=restored)
                self.assert_original_readable(replay, app)
                self.assertEqual([], model.requests)
                self.assertEqual([], model.quality_requests)
                self.assertEqual(original.review["status"], replay.review["status"])
                self.assertEqual(original.review["effective"], replay.review["effective"])
                self.assertEqual(budget.receipt(), restored.receipt())
                self.assertEqual(restored.receipt(), replay.review["budget_total"])
                with self.assertRaisesRegex(ValueError, "MODEL_CALL_LIMIT"):
                    restored.reserve("no free quality attempt after resume")


if __name__ == "__main__":
    unittest.main()
