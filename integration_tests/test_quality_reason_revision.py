"""Assumption reasons may be corrected only with the related research prose."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import unittest

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources, support
from test_thesis_quality_flow import QualityLLM, SUMMARY, CAUTIOUS_SUMMARY
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.core.forward_scenarios import calculate_forward
from finauditgate.research import ResearchThesis
from native_support import payload_of


NEW_REASON = (
    "该项仍是需要验证的条件假设，仅参照给定资料说明适用范围；后续经营变化可能偏离，"
    "不把情景参数视为已经披露的事实{{source:E0001}}。"
)
NEW_BODY = "该情景继续作为有条件的经营推演，假设依据只提供参考，须由后续观察验证而非财务事实认证。"
NEW_VALUATION = "共同参数的依据已经重新限定为所给资料的适用口径；条件价格推演仍依赖经营假设兑现，不构成公允价值认证。"


def get_path(value, path):
    for part in re.sub(r"\[([0-9]+)\]", r".\1", path).split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class ReasonRevisionLLM(QualityLLM):
    reason_mode: str = "valid"
    reason_path: str = "effective_forward_draft.scenarios[0].operating_margin.reason"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind not in ("QualityReview", "QualityRevision"):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        payload = payload_of(messages[-1].content)
        item = {"node": payload["node"], "schema": kind, "payload": deepcopy(payload)}
        self.quality_requests.append(item)
        self.requests.append({**item, "reasoning_effort": self.reasoning_effort,
            "text": "\n".join(m.content for m in messages)})
        if kind == "QualityReview":
            path = self.reason_path
            if self.reason_mode == "value_finding":
                path = path.removesuffix("reason") + "value"
            findings = [self.finding("R1", path, str(get_path(payload, path)))]
            if self.reason_mode == "merged":
                other = "effective_forward_draft.scenarios[0].revenue.reason"
                findings.append(self.finding("R2", other, get_path(payload, other)))
                findings.append(self.finding("R3", "scenario_assessments[0].reason",
                    payload["report"]["scenario_assessments"][0]["reason"]))
            elif self.reason_mode == "body_noop_direct":
                findings.append(self.finding("R2", "scenario_assessments[0].reason",
                    payload["report"]["scenario_assessments"][0]["reason"]))
            elif self.reason_mode == "optional_rating":
                findings[0]["impact"] = "optional"
                findings.append(self.finding("R2", "summary.text", payload["report"]["summary"]["text"]))
            value = {"findings": findings, "financial_changes": [],
                "coverage_and_limits": "SYNTHETIC_ONLY：只纠正假设理由及同情景判断，不认证预测。"}
        else:
            findings = payload["assessment"]["findings"]
            edits = []
            for finding in findings:
                if not finding["path"].startswith("effective_forward_draft."):
                    continue
                if self.reason_mode == "optional_rating":
                    continue
                edits.append({"finding_ids": [finding["id"]], "path": finding["path"],
                    "expected_text": finding["original_text"],
                    "replacement_text": "EPS=999美元；其余解释不变，仍声称情景与正文完整一致且足以支撑现有研究观点。"
                        if self.reason_mode == "bare_number" else NEW_REASON})
            if self.reason_mode == "value_edit":
                edits[0].update(path=self.reason_path.removesuffix("reason") + "value", expected_text="0.2", replacement_text="0.3")
            related = payload["related_text"]["R1"]
            related_path = next(iter(related))
            if self.reason_mode == "optional_rating":
                edits.append({"finding_ids": ["R2"], "path": "summary.text",
                    "expected_text": SUMMARY, "replacement_text": CAUTIOUS_SUMMARY})
            elif self.reason_mode != "no_body":
                if self.reason_mode == "other_scenario":
                    related_path = "scenario_assessments.1.reason"
                elif self.reason_mode == "unrelated":
                    related_path = "financial_analysis.cash_and_capital_allocation.text"
                edits.append({"finding_ids": [f["id"] for f in findings], "path": related_path,
                    "expected_text": get_path(payload["report"], related_path),
                    "replacement_text": get_path(payload["report"], related_path)
                        if self.reason_mode in ("body_confirm", "body_noop_direct") else
                        NEW_VALUATION if related_path.startswith("financial_analysis.valuation") else NEW_BODY})
            value = {"edits": edits,
                "resolutions": [{"finding_id": f["id"], "outcome": "unresolved"
                    if self.reason_mode == "optional_rating" and f["id"] == "R1" else "corrected",
                    "reason": "仅限定假设依据的支持范围，并在相关正文同步保留条件性判断。",
                    "evidence_refs": ["S01"]} for f in findings],
                "rating": {"value": "Sell" if self.reason_mode == "optional_rating" else "REVIEW",
                    "reason": "依据与情景判断均已重审，仍不足以给出可辩护的价格评级。"},
                "scenario_decisions": []}
        return self._result(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def finding(identity, path, original):
        return {"id": identity, "path": path, "original_text": original,
            "issue": "该假设依据的适用范围需要写明，不应被视为披露认证。", "impact": "wording",
            "evidence_refs": ["S01"], "next_check": "保留原数值和口径，只更正理由并重审相关情景正文。"}


class QualityReasonRevisionTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model, *, root=None):
        root = root or self.root
        app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher())
        command = ResearchThesis("AURORA", date(2026, 3, 2), "合成理由校订：参数和研究内容完整保留。",
            sources=correction_sources(), review=True)
        with patched_native_runtime(model):
            view = app.handle(command)
        return view, app

    def assert_main_retained(self, view, app):
        validate(view.latest_report)
        self.assertEqual(13, len(view.latest_report["exchanges"]))
        self.assertEqual(13, len(view.latest_report["model_calls"]))
        self.assertEqual(SUMMARY, view.latest_report["final_report"]["summary"]["text"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_reason_and_related_prose_change_together_without_changing_values_or_calculations(self):
        targets = ["effective_forward_draft.scenarios.0.operating_margin.reason",
                   "effective_forward_draft.scenarios[0].operating_margin.reason",
                   "effective_forward_draft.scenarios[0].noncontrolling_attribution.amount.reason",
                   "effective_forward_draft.market_price.reason"]
        for index, target in enumerate(targets):
            with self.subTest(target=target):
                model = ReasonRevisionLLM(reason_path=target)
                view, app = self.run_case(model, root=self.root / f"target-{index}")
                self.assert_main_retained(view, app)
                self.assertEqual("COMPLETED", view.review["status"])
                self.assertEqual(["QualityReview", "QualityRevision"], [r["schema"] for r in model.quality_requests])
                effective = view.review["effective"]
                before = view.latest_report["effective_forward_draft"]
                after = deepcopy(effective["effective_forward_draft"])
                local_path = re.sub(r"\[([0-9]+)\]", r".\1", target).removeprefix("effective_forward_draft.")
                self.assertEqual(NEW_REASON, get_path(after, local_path))
                owner_path = local_path.removesuffix(".reason")
                get_path(after, owner_path)["reason"] = get_path(before, local_path)
                self.assertEqual(before, after)
                self.assertEqual(view.latest_report["effective_forward_calculations"], effective["effective_forward_calculations"])
                self.assertEqual(calculate_forward(effective["effective_forward_draft"]), effective["effective_forward_calculations"])
                changed = effective["applied_changes"]
                self.assertEqual(1, len(changed))
                self.assertEqual(changed[0]["before"]["value"] if "value" in changed[0]["before"] else changed[0]["before"]["amount"]["value"],
                                 changed[0]["after"]["value"] if "value" in changed[0]["after"] else changed[0]["after"]["amount"]["value"])
                related = model.quality_requests[-1]["payload"]["related_text"]["R1"]
                self.assertEqual(["financial_analysis.valuation_and_price_requirements.text"] if "scenarios" not in target else ["scenario_assessments.0.reason"], list(related))
                report_text = Path(view.delivery_report_path).read_text()
                self.assertIn("本次更正的假设依据", report_text)
                self.assertIn("不把情景参数视为已经披露的事实", report_text)
                self.assertIn(NEW_VALUATION if "scenarios" not in target else NEW_BODY, report_text)

    def test_reason_only_change_without_related_body_is_not_delivery(self):
        view, app = self.run_case(ReasonRevisionLLM(reason_mode="no_body"))
        self.assert_main_retained(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual("THESIS_QUALITY_REASON_BODY_NOT_REVIEWED", view.review["error_code"])
        self.assertIsNone(view.review["effective"])
        self.assertEqual(view.research_report_path, view.delivery_report_path)

    def test_value_fields_are_neither_findings_nor_text_edit_targets(self):
        for mode in ("value_finding", "value_edit"):
            with self.subTest(mode=mode):
                model = ReasonRevisionLLM(reason_mode=mode)
                view, app = self.run_case(model, root=self.root / mode)
                self.assert_main_retained(view, app)
                self.assertEqual("PARTIAL", view.review["status"])
                self.assertIsNone(view.review["effective"])
                self.assertEqual(1 if mode == "value_finding" else 2, len(model.quality_requests))
                self.assertEqual(0.2, view.latest_report["effective_forward_draft"]["scenarios"][0]["operating_margin"]["value"])

    def test_other_scenario_and_unrelated_section_cannot_be_rewritten(self):
        for mode in ("other_scenario", "unrelated"):
            with self.subTest(mode=mode):
                view, app = self.run_case(ReasonRevisionLLM(reason_mode=mode), root=self.root / mode)
                self.assert_main_retained(view, app)
                self.assertEqual("PARTIAL", view.review["status"])
                self.assertEqual("THESIS_QUALITY_EDIT_OUTSIDE_FINDING", view.review["error_code"])
                self.assertIsNone(view.review["effective"])

    def test_multiple_findings_share_one_nonoverlapping_related_body_edit(self):
        model = ReasonRevisionLLM(reason_mode="merged")
        view, app = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual(3, len(view.review["assessment"]["findings"]))
        edits = view.review["revision"]["edits"]
        body = [e for e in edits if e["path"] == "scenario_assessments.0.reason"]
        self.assertEqual(1, len(body))
        self.assertEqual(["R1", "R2", "R3"], body[0]["finding_ids"])
        self.assertEqual(2, len(view.review["effective"]["applied_changes"]))
        self.assertEqual(NEW_BODY, view.review["effective"]["final_report"]["scenario_assessments"][0]["reason"])
        self.assertEqual(view.latest_report["effective_forward_calculations"], view.review["effective"]["effective_forward_calculations"])

    def test_unbound_financial_number_in_new_reason_remains_a_hard_failure(self):
        view, app = self.run_case(ReasonRevisionLLM(reason_mode="bare_number"))
        self.assert_main_retained(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual("UNBOUND_RESEARCH_NUMBER", view.review["error_code"])
        self.assertIsNone(view.review["effective"])
        self.assertEqual(view.research_report_path, view.delivery_report_path)

    def test_correct_reason_may_explicitly_confirm_an_already_correct_related_body(self):
        view, app = self.run_case(ReasonRevisionLLM(reason_mode="body_confirm"))
        self.assert_main_retained(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        effective = view.review["effective"]
        self.assertEqual(NEW_REASON, effective["effective_forward_draft"]["scenarios"][0]["operating_margin"]["reason"])
        self.assertEqual(view.latest_report["final_report"]["scenario_assessments"], effective["final_report"]["scenario_assessments"])
        self.assertEqual(view.latest_report["effective_forward_calculations"], effective["effective_forward_calculations"])
        body = view.review["revision"]["edits"][-1]
        self.assertEqual(body["expected_text"], body["replacement_text"])

    def test_related_noop_cannot_claim_a_direct_body_finding_was_corrected(self):
        view, app = self.run_case(ReasonRevisionLLM(reason_mode="body_noop_direct"))
        self.assert_main_retained(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual("THESIS_QUALITY_CORRECTION_NOT_APPLIED", view.review["error_code"])
        self.assertIsNone(view.review["effective"])
        self.assertEqual(view.research_report_path, view.delivery_report_path)

    def test_unedited_optional_reason_does_not_authorize_a_rating_change(self):
        view, app = self.run_case(ReasonRevisionLLM(reason_mode="optional_rating"))
        self.assert_main_retained(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual("THESIS_QUALITY_UNJUSTIFIED_RATING_CHANGE", view.review["error_code"])
        self.assertIsNone(view.review["effective"])
        self.assertEqual("REVIEW", view.latest_report["signal"])
        self.assertIsNone(view.delivery_rating)
