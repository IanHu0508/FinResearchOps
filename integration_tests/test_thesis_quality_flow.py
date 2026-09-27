"""Bounded quality aftercare through the real graph, persistence and reader.

Only the provider is synthetic. These checks establish targeted revision and
delivery integrity, not that a model can detect every financial error.
"""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APITimeoutError
from pydantic import Field

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources, support
from test_thesis_delivery import SelectionLLM
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import FinResearchOps
from finauditgate.application.thesis_case import render, validate
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.core.forward_scenarios import calculate_forward
from finauditgate.research import ResearchThesis


SUMMARY = (
    "经营判断保持；历史资料{{source:E0001}}；"
    "有效盈利{{metric:F1:eps_per_traded_unit}}；Q4实际数据尚未披露。"
)
CAUTIOUS_SUMMARY = SUMMARY.replace("经营判断保持", "经营判断仍取决于后续观察")


class QualityLLM(SelectionLLM):
    quality_mode: str = "wording"
    quality_requests: list[dict] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind not in ("QualityReview", "QualityRevision"):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        payload = json.loads(messages[-1].content)
        item = {"node": payload["node"], "schema": kind, "payload": deepcopy(payload)}
        self.quality_requests.append(item)
        self.requests.append({**item, "reasoning_effort": self.reasoning_effort,
            "text": "\n".join(m.content for m in messages)})
        if self.quality_mode == "review_timeout" and kind == "QualityReview":
            raise APITimeoutError(request=httpx.Request("POST", "https://synthetic.invalid"))
        if self.quality_mode == "revision_error" and kind == "QualityRevision":
            raise RuntimeError("SYNTHETIC_QUALITY_REVISION_FAILURE")
        if kind == "QualityReview":
            finding = {"id": "R1", "path": "summary.text", "original_text": SUMMARY,
                "issue": "既有经营结论表述过于确定，应保留后续观察条件。",
                "impact": "wording", "evidence_refs": ["S01"],
                "next_check": "对照所给合成资料，将观察与持续性假设分开。"}
            changes = []
            if self.quality_mode == "missing_original":
                finding["original_text"] = "这一句并不存在于主稿。"
            elif self.quality_mode == "financial":
                finding.update(impact="financial",
                    issue="合成压力测试要求修正收入情景假设，并重新计算其影响。",
                    next_check="明确收入仍为分析假设，复算归母盈利与每股盈利。")
                changes = [{"finding_ids": ["R1"], "change": {
                    "scenario_id": "F1", "field": "revenue", "expected_before": 100.0,
                    "replacement": {"value": 110.0, "basis_type": "analyst_assumption",
                        "reason": "合成条件情景调整，不宣称来源披露了未来收入。",
                        "evidence_refs": ["S01"]},
                    "correction_basis": "assumption_update",
                    "reason": "仅修改本项条件假设，并由程序重新计算。",
                    "evidence_refs": ["S01"]}}]
            elif self.quality_mode == "conclusion":
                finding.update(impact="conclusion",
                    issue="原结论未回应定价不利的条件性判断。",
                    next_check="依据原有条件情景重新判断研究评级。")
            value = {"findings": [finding], "financial_changes": changes,
                "coverage_and_limits": "SYNTHETIC_ONLY：仅核查指定表述，不构成金融认证。"}
        else:
            replacement = CAUTIOUS_SUMMARY
            if self.quality_mode == "unbound_number":
                replacement = "盈利预测为999美元，结论保持。"
            value = {"edits": [{"finding_ids": ["R1"], "path": "summary.text",
                                  "expected_text": SUMMARY, "replacement_text": replacement}],
                "resolutions": [{"finding_id": "R1", "outcome": "corrected",
                    "reason": "在正文恢复条件性表述；其余研究内容保持。", "evidence_refs": ["S01"]}],
                "rating": {"value": "Sell" if self.quality_mode == "conclusion" else "REVIEW",
                    "reason": "以有效条件情景重审结论，合成结果不代表投资意见。"},
                "scenario_decisions": []}
        return self._result(json.dumps(value, ensure_ascii=False))


class ThesisQualityFlowTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model=None, *, budget=None, review=True):
        model = model or QualityLLM()
        app = FinResearchOps(artifact_root=self.root, researcher=ThesisResearcher(
            live=budget is not None, budget=budget))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "完整研究与有界质量修正。",
            sources=correction_sources(), review=review)
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        return view, app, model

    def assert_original_readable(self, view, app):
        record = view.latest_report
        validate(record)
        self.assertEqual("finresearchops.thesis-case/v16", record["schema_version"])
        self.assertEqual(13, len(record["exchanges"]))
        self.assertEqual(13, len(record["model_calls"]))
        self.assertEqual([], record["recovery"]["attempts"])
        self.assertEqual(SUMMARY, record["final_report"]["summary"]["text"])
        original = Path(view.report_path)
        self.assertEqual("report.md", original.name)
        self.assertEqual(render(record), original.read_bytes())
        raw = (original.parent / "case.json").read_bytes()
        self.assertEqual("case-" + sha256_hex(raw), view.case_ref)
        self.assertEqual(record, json.loads(raw))
        self.assertEqual(record, app.read_case(view.case_ref).latest_report)

    def assert_quality_failed_without_losing_main(self, view, app):
        self.assert_original_readable(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertFalse(view.review.get("effective"))
        # Without an effective correction, deliver the original's formal layout.
        self.assertEqual(view.research_report_path, view.delivery_report_path)
        self.assertEqual("research-report.md", Path(view.delivery_report_path).name)
        self.assertIsNone(view.delivery_rating)

    def test_quality_revision_preserves_main_and_spends_from_the_same_budget(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        view, app, model = self.run_case(budget=budget)
        self.assert_original_readable(view, app)
        self.assertEqual("finresearchops.thesis-review/v3", view.review["schema_version"])
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual(["QualityReview", "QualityRevision"],
                         [r["schema"] for r in model.quality_requests])
        self.assertTrue(all(r["node"] == "Data Review Agent" for r in model.quality_requests))
        effective = view.review["effective"]
        self.assertEqual(CAUTIOUS_SUMMARY, effective["final_report"]["summary"]["text"])
        before = deepcopy(view.latest_report["final_report"])
        after = deepcopy(effective["final_report"])
        after["summary"]["text"] = before["summary"]["text"]
        self.assertEqual(before, after)
        self.assertEqual(view.latest_report["effective_forward_draft"], effective["effective_forward_draft"])
        self.assertEqual(view.latest_report["effective_forward_calculations"], effective["effective_forward_calculations"])
        delivery = Path(view.delivery_report_path)
        self.assertEqual("quality-report.md", delivery.name)
        self.assertIn("经营判断仍取决于后续观察", delivery.read_text())
        self.assertTrue(delivery.with_name("quality-process-record.md").is_file())
        self.assertEqual(13, view.latest_report["budget"]["calls"])
        self.assertEqual(15, budget.calls)
        self.assertEqual(budget.receipt(), view.review["budget_total"])
        execution = next(self.root.glob("application/thesis-executions/*"))
        runtime = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual(15, len(runtime["model_calls"]))
        self.assertEqual([], runtime["recovery"]["attempts"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_review_cannot_target_a_sentence_that_is_absent_from_the_report(self):
        view, app, model = self.run_case(QualityLLM(quality_mode="missing_original"))
        self.assertEqual(["QualityReview"], [r["schema"] for r in model.quality_requests])
        self.assert_quality_failed_without_losing_main(view, app)

    def test_uncomputed_number_cannot_enter_the_quality_report(self):
        view, app, model = self.run_case(QualityLLM(quality_mode="unbound_number"))
        self.assertEqual(2, len(model.quality_requests))
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertFalse(Path(view.report_path).with_name("quality-report.md").exists())

    def test_review_timeout_is_retained_without_retry_or_main_recovery(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        view, app, model = self.run_case(QualityLLM(quality_mode="review_timeout"), budget=budget)
        self.assertEqual(["QualityReview"], [r["schema"] for r in model.quality_requests])
        self.assert_quality_failed_without_losing_main(view, app)
        self.assertEqual(14, budget.calls)
        execution = next(self.root.glob("application/thesis-executions/*"))
        runtime = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual([], runtime["recovery"]["attempts"])
        self.assertEqual("APITimeoutError", runtime["model_calls"][-1]["error_type"])

    def test_revision_exception_does_not_destroy_the_completed_main_report(self):
        view, app, model = self.run_case(QualityLLM(quality_mode="revision_error"))
        self.assertEqual(["QualityReview", "QualityRevision"], [r["schema"] for r in model.quality_requests])
        self.assert_quality_failed_without_losing_main(view, app)

    def test_parameter_revision_uses_effective_inputs_and_recomputes_before_writing(self):
        view, app, model = self.run_case(QualityLLM(quality_mode="financial"))
        self.assert_original_readable(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        effective = view.review["effective"]
        old = view.latest_report["effective_forward_draft"]
        revised = effective["effective_forward_draft"]
        self.assertEqual(100, old["scenarios"][0]["revenue"]["value"])
        self.assertEqual(110, revised["scenarios"][0]["revenue"]["value"])
        self.assertEqual("profit", revised["scenarios"][0]["noncontrolling_attribution"]["nature"])
        self.assertEqual(13, view.latest_report["effective_forward_calculations"]["scenario_results"][0]["parent_net_income"])
        self.assertEqual(14.5, effective["effective_forward_calculations"]["scenario_results"][0]["parent_net_income"])
        self.assertEqual(calculate_forward(revised), effective["effective_forward_calculations"])
        payload = model.quality_requests[-1]["payload"]
        self.assertEqual(revised, payload["effective_forward_draft"])
        self.assertEqual(effective["effective_forward_calculations"], payload["effective_forward_calculations"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_delivery_rating_tracks_revised_conclusion_without_rewriting_original_signal(self):
        view, app, _ = self.run_case(QualityLLM(quality_mode="conclusion"))
        self.assert_original_readable(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual("REVIEW", view.latest_report["signal"])
        self.assertEqual("Sell", view.delivery_rating)
        self.assertEqual("Sell", view.review["effective"]["final_report"]["rating"])

    def test_tampered_quality_sidecar_is_rejected_while_original_case_stays_readable(self):
        view, app, _ = self.run_case()
        self.assertEqual("COMPLETED", view.review["status"])
        review_path = Path(view.report_path).with_name("review.json")
        original_main = Path(view.report_path).read_bytes()
        forged = json.loads(review_path.read_text())
        forged["effective"]["final_report"]["summary"]["text"] = "盈利结论已经无条件获得保证。"
        review_path.write_bytes(canonical_json_bytes(forged))
        reloaded = app.read_case(view.case_ref)
        self.assert_quality_failed_without_losing_main(reloaded, app)
        self.assertEqual(original_main, Path(reloaded.report_path).read_bytes())

    def test_quality_reader_is_standard_library_only(self):
        view, _, _ = self.run_case()
        python = Path(__file__).parents[1] / ".venv/bin/python"
        code = ("import sys; from pathlib import Path; from finauditgate.application import FinResearchOps; "
                "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); "
                "assert v.review['status']=='COMPLETED'; "
                "assert Path(v.delivery_report_path).name=='quality-report.md'; "
                "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        run = subprocess.run([str(python), "-S", "-c", code, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)

    def test_explicitly_disabled_review_does_not_run_quality_calls(self):
        view, app, model = self.run_case(review=False)
        self.assert_original_readable(view, app)
        self.assertEqual([], model.quality_requests)
        self.assertEqual("DEFERRED", view.review["status"])
        self.assertEqual(view.research_report_path, view.delivery_report_path)
        self.assertEqual(view.latest_report["signal"], view.delivery_rating)

    def test_correction_receives_the_same_full_financial_text_contract(self):
        from finauditgate.application.research_delivery import INSTRUCTION
        view, _, _ = self.run_case()
        instruction = view.review["exchanges"][1]["messages"][0]["content"]
        self.assertIn(INSTRUCTION, instruction)
        self.assertIn("不要把review.issue/next_check/reason中的金额", instruction)
        self.assertIn("只返回下面QualityRevision结构", instruction)
