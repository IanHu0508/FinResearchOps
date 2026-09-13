"""Exercise numerical prose at the model → graph → saved report boundary."""

import json
import os
from pathlib import Path
from datetime import date
from types import SimpleNamespace
import unittest
import subprocess
from unittest.mock import patch
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion

from native_support import patched_native_runtime
from test_thesis_correction import CorrectionLLM, correction_sources, support
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.research import ResearchThesis


class NarrativeLLM(CorrectionLLM):
    location: str = "summary"
    prose: str = "有效盈利须结合经营条件判断。"
    empty_metrics: bool = False
    quoted_history: bool = False
    misleading_old_labels: bool = False
    missing_explanation: bool = False
    metric_echo: bool = False
    conflicting_echo: bool = False
    novel_echo: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else None
        if kind == "UnderwritingDraft" and self.misleading_old_labels:
            value = json.loads(result.generations[0].message.content)
            value["scenarios"][0]["name"] = "旧名称预测EPS999"
            value["scenarios"][0]["revenue"]["reason"] = "旧依据声称目标价998美元"
            result.generations[0].message.content = json.dumps(value)
        if kind in ("ForwardRevision", "FinalResearchReport"):
            value = json.loads(result.generations[0].message.content)
            if "source_quotes" in schema.model_fields:
                value["source_quotes"] = []
                if self.quoted_history:
                    value["source_quotes"] = [{"id": "Q1", "source_id": "S01", "quote":
                        "Profit attributable to noncontrolling owners is 2 million USD and parent net income is 13 million USD."}]
            if kind == "FinalResearchReport":
                payload = json.loads(messages[-1].content)
                def explanation(text):
                    return {"text": text, "evidence_refs": ["S01"], "metrics": []}
                if "change_explanations" in schema.model_fields:
                    value["change_explanations"] = [{"scenario_id": r["scenario_id"], "field": r["field"],
                        "explanation": explanation("盈利归属方向修正影响归母盈利，金额仍为假设。")}
                        for r in payload["change_context"]]
                    value["belief_explanations"] = [{"belief_id": r["belief_id"],
                        "explanation": explanation("根据有效口径解释已记录的经营判断变化。")}
                        for r in payload["research_resolution"]["belief_updates"]]
                    if self.missing_explanation:
                        value["change_explanations"] = []
                    if self.location == "belief":
                        value["belief_explanations"][0]["explanation"]["text"] = self.prose
                    elif self.location == "change":
                        value["change_explanations"][0]["explanation"]["text"] = self.prose
                    elif self.location == "unresolved":
                        value["limitations"] = [self.prose]
                if self.location == "summary":
                    value["summary"]["text"] = self.prose
                    if self.empty_metrics:
                        value["summary"]["metrics"] = []
                elif self.location == "scenario":
                    value["scenario_assessments"][0]["reason"] = self.prose
                elif self.location == "trigger":
                    value["scenario_assessments"][0]["what_changes_the_view"] = self.prose
                elif self.location == "limitations":
                    value["limitations"] = [self.prose]
                elif self.location == "financial":
                    value["financial_analysis"]["earnings_quality"]["text"] = self.prose
                elif self.location == "counterevidence":
                    value["strongest_counterevidence"]["text"] = self.prose
                if self.metric_echo or self.conflicting_echo or self.novel_echo:
                    value["scenario_assessments"][0]["reason"] += " {{metric:F1:eps_per_traded_unit}}"
                    value["scenario_assessments"][0]["metrics"] = [{"scenario_id": "F1", "metric": "eps_per_traded_unit"}]
                    if self.conflicting_echo:
                        value["scenario_assessments"][0]["metrics"][0]["value"] = 999
                    if self.novel_echo:
                        value["scenario_assessments"][0]["metrics"][0]["metric"] = "revenue"
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class OverrunNarrativeLLM(NarrativeLLM):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except LengthFinishReasonError as exc:
            raw = exc.completion.model_dump()
            raw["usage"]["completion_tokens"] += 14
            raw["usage"]["total_tokens"] += 14
            raise LengthFinishReasonError(completion=ChatCompletion.model_validate(raw)) from exc


class ThesisNarrativeTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model, *, budget=None, live=False):
        app = FinResearchOps(artifact_root=self.root, researcher=ThesisResearcher(budget=budget, live=live))
        request = ResearchThesis("AURORA", date(2026, 3, 2), "经营与价格是否支持投资？", sources=correction_sources(), review=False)
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            return app.handle(request), app

    def assert_unbound(self, model):
        with self.assertRaises(ApplicationError) as failure:
            self.run_case(model)
        self.assertIsInstance(failure.exception.__cause__, ValueError)
        self.assertIn("UNBOUND_RESEARCH_NUMBER", str(failure.exception.__cause__))
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))

    def test_wrong_forecast_prose_cannot_be_saved_even_with_correct_metrics(self):
        self.assert_unbound(NarrativeLLM(prose="F1 的预测 EPS 为 999 USD，可支撑 9990 USD 的目标价。"))

    def test_empty_metrics_do_not_bypass_the_same_guard(self):
        self.assert_unbound(NarrativeLLM(prose="F1 的预测 EPS 为 999 USD。", empty_metrics=True))

    def test_stale_pre_correction_value_cannot_reenter_the_final_report(self):
        self.assert_unbound(NarrativeLLM(prose="F1 的预测 EPS 为 1.7 USD。"))

    def test_scenario_reasons_triggers_limits_and_belief_updates_are_also_checked(self):
        for location in ("scenario", "trigger", "limitations", "belief", "financial", "counterevidence", "change", "unresolved"):
            with self.subTest(location=location):
                self.assert_unbound(NarrativeLLM(location=location, prose="F1 的预测 EPS 为 999 USD。"))

    def test_bound_metric_uses_effective_value_through_save_and_reopen(self):
        model = NarrativeLLM(prose="归属修正后的条件盈利为 {{metric:F1:eps_per_traded_unit}}。")
        view, app = self.run_case(model)
        self.assertEqual(view, app.read_case(view.case_ref))
        report = Path(view.report_path).read_text()
        self.assertIn("1.3", report)
        self.assertNotIn("{{metric:", report)
        self.assertIn("EPS", report)
        self.assertIn("USD", report)
        self.assertEqual("max", next(r["reasoning_effort"] for r in model.requests if r["schema"] == "ForwardRevision"))
        self.assertEqual("max", next(r["reasoning_effort"] for r in model.requests if r["schema"] == "FinalResearchReport"))

    def test_exact_history_and_context_work_in_the_current_main_report(self):
        view, app = self.run_case(NarrativeLLM(quoted_history=True, prose=
            "历史参考为 {{source:Q1}}；当前条件为 {{metric:F1:eps_per_traded_unit}}；日期为 {{context:as_of}}。"))
        text = Path(view.report_path).read_text()
        self.assertIn("2 million USD", text)
        self.assertIn("非本次有效预测", text)
        self.assertIn("2026-03-02", text)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_standard_library_only_reader_reopens_the_new_case(self):
        view, _ = self.run_case(NarrativeLLM())
        python = Path(__file__).parents[1] / ".venv/bin/python"
        code = ("import sys; from pathlib import Path; from finauditgate.application import FinResearchOps; "
                "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); "
                "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v13'; "
                "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents')); print(v.status)")
        run = subprocess.run([str(python), "-c", code, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)

    def test_old_numeric_names_and_parameter_reasons_remain_only_in_the_appendix(self):
        view, _ = self.run_case(NarrativeLLM(misleading_old_labels=True))
        report = Path(view.report_path)
        main, appendix = report.read_text(), (report.parent / "process-record.md").read_text()
        for marker in ("旧名称预测EPS999", "旧依据声称目标价998美元"):
            self.assertNotIn(marker, main)
            self.assertIn(marker, appendix)

    def test_final_cannot_omit_explanations_for_applied_parameter_changes(self):
        with self.assertRaises(ApplicationError) as failure:
            self.run_case(NarrativeLLM(missing_explanation=True))
        self.assertIn("RESEARCH_EXPLANATION_COVERAGE_INVALID", str(failure.exception.__cause__))

    def test_reopen_rejects_changed_narrative_or_appendix(self):
        view, app = self.run_case(NarrativeLLM())
        report = Path(view.report_path)
        original = report.read_bytes()
        report.write_bytes(original + b"\nEPS=999")
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)
        report.write_bytes(original)
        appendix = report.parent / "process-record.md"
        appendix.write_bytes(appendix.read_bytes() + b"\nchanged")
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)

    def test_resume_does_not_reuse_a_schema_valid_unbound_final(self):
        from finauditgate.adapters.thesis_responses import CompletedCalls
        self.run_case(NarrativeLLM())
        execution = next(self.root.glob("application/thesis-executions/*"))
        request = json.loads((execution / "request.json").read_text())
        receipt = json.loads((execution / "runtime-receipt.json").read_text())
        clean = CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=13)
        self.assertEqual(13, len(clean.rows))
        value = json.loads(receipt["model_calls"][12]["output"][0]["content"])
        value["summary"]["text"] = "EPS为999美元。"
        receipt["model_calls"][12]["output"][0]["content"] = json.dumps(value)
        read_bytes = Path.read_bytes
        with patch.object(Path, "read_bytes", lambda p: json.dumps(receipt).encode() if p.name == "runtime-receipt.json" else read_bytes(p)):
            altered = CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=13)
        self.assertEqual(12, len(altered.rows))
        self.assertEqual("UNBOUND_RESEARCH_NUMBER", altered.receipt["prefix_stop"]["error_code"])

    def test_confirmed_sdk_length_overcount_can_use_only_the_existing_final_retry(self):
        budget = ModelBudget(ceiling_cny=None, max_calls=14, max_output_tokens=64)
        view, app = self.run_case(OverrunNarrativeLLM(truncations=1), budget=budget, live=True)
        self.assertEqual(view, app.read_case(view.case_ref))
        attempts = view.latest_report["final_generation"]
        self.assertEqual(["TRUNCATED", "COMPLETED"], [r["status"] for r in attempts])
        self.assertEqual(["max", "high"], [r["reasoning_effort"] for r in attempts])
        self.assertEqual(14, budget.receipt()["calls"])
        self.assertIn(78, [r["output_tokens"] for r in budget.receipt()["usage"]])
        self.assertEqual(64, budget.max_output_tokens)

    def test_duplicate_selector_echo_is_recorded_without_losing_raw_output(self):
        view, app = self.run_case(NarrativeLLM(metric_echo=True))
        self.assertEqual(view, app.read_case(view.case_ref))
        r = view.latest_report
        raw = json.loads(r["model_calls"][-1]["output"][0]["content"])
        self.assertIn("metrics", raw["scenario_assessments"][0])
        self.assertNotIn("metrics", r["final_report"]["scenario_assessments"][0])
        self.assertEqual(raw["scenario_assessments"][0]["reason"], r["final_report"]["scenario_assessments"][0]["reason"])
        self.assertIn("机械格式归一记录", (Path(view.report_path).parent / "process-record.md").read_text())

    def test_echo_with_a_supplied_value_is_never_silently_removed(self):
        for model in (NarrativeLLM(conflicting_echo=True), NarrativeLLM(novel_echo=True)):
            with self.assertRaises(ApplicationError) as failure:
                self.run_case(model)
            self.assertIn("THESIS_SCENARIO_METRIC_ECHO_CONFLICT", str(failure.exception.__cause__))
