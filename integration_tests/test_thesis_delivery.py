"""Selected evidence through the real graph, persistence, reader and resume seams."""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import unittest

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources, support
from test_thesis_narrative import NarrativeLLM
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.research import ResearchThesis


class SelectionLLM(NarrativeLLM):
    unknown_evidence: bool = False
    unknown_source: bool = False
    plain_blocks: bool = True
    bad_value: bool = False
    explicit_source: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema and schema.__name__ == "FinalResearchReport":
            payload = json.loads(messages[-1].content)
            assert "content_blocks" in payload["source_bundle"]["sources"][0]
            assert "content" not in payload["source_bundle"]["sources"][0]
            value = json.loads(result.generations[0].message.content)
            ref = "E9999" if self.unknown_evidence else payload["source_bundle"]["sources"][0]["content_blocks"][0]["id"]
            value["summary"]["text"] = "经营判断保持；历史资料{{source:" + ref + "}}；有效盈利{{metric:F1:eps_per_traded_unit}}；Q4实际数据尚未披露。"
            if self.plain_blocks:
                def remove(v):
                    if isinstance(v, dict):
                        if "text" in v:
                            v.pop("evidence_refs", None)
                            v.pop("metrics", None)
                        for child in v.values():
                            remove(child)
                    elif isinstance(v, list):
                        for child in v:
                            remove(child)
                remove(value)
            if self.unknown_source:
                value["summary"]["evidence_refs"] = ["S999"]
            if self.explicit_source:
                value["summary"]["evidence_refs"] = [self.explicit_source]
            if self.bad_value:
                value["summary"]["metrics"] = [{"scenario_id": "F1", "metric": "revenue", "value": 999}]
            value["scenario_assessments"][0]["metrics"] = [{"scenario_id": "F1", "metric": "revenue"}]
            value["change_explanations"][0]["explanation"]["metrics"] = [{"scenario_id": "F1", "metric": "pretax_income"}]
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class ThesisDeliveryTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model=None, *, bundle=None):
        app = FinResearchOps(artifact_root=self.root, researcher=ThesisResearcher())
        command = ResearchThesis("AURORA", date(2026, 3, 2), "完整研究与反证。", sources=bundle or correction_sources(), review=False)
        with patched_native_runtime(model or SelectionLLM()):
            return app.handle(command), app

    def test_v15_derives_metadata_and_retains_full_report_and_raw_response(self):
        view, app = self.run_case()
        record = view.latest_report
        self.assertEqual("finresearchops.thesis-case/v16", record["schema_version"])
        self.assertEqual("COMPLETED", record["evidence_check"]["status"])
        self.assertEqual(["S01"], record["final_report"]["summary"]["evidence_refs"])
        self.assertEqual([{"scenario_id": "F1", "metric": "eps_per_traded_unit"}], record["final_report"]["summary"]["metrics"])
        self.assertEqual(13, len(record["exchanges"]))
        raw = json.loads(record["model_calls"][-1]["output"][0]["content"])
        self.assertNotIn("metrics", raw["summary"])
        self.assertEqual(raw["summary"]["text"], record["final_report"]["summary"]["text"])
        text = Path(view.report_path).read_text()
        for part in ("## 结论", "## 最强反证及其影响", "## 参数变化", "## 旧信念", "## 结论限制", "Q4实际数据", "F1 · 情景指标"):
            self.assertIn(part, text)
        self.assertIn("来源原文", text)
        correction = text.split("## 参数变化", 1)[1].split("## 旧信念", 1)[0]
        self.assertIn("税前利润", correction)
        self.assertIn("| 税前利润 | 20 | 百万元 USD |", correction)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_unknown_evidence_saves_full_partial_case_and_cannot_claim_binding_complete(self):
        view, app = self.run_case(SelectionLLM(unknown_evidence=True))
        self.assertEqual("PARTIAL", view.status)
        self.assertEqual("PARTIAL", view.latest_report["status"])
        findings = view.latest_report["evidence_check"]["findings"]
        self.assertTrue(any(r["field"] == "summary" and r["reference"] == "E9999" for r in findings))
        text = Path(view.report_path).read_text()
        self.assertIn("完整研究已保存", text)
        self.assertIn("经营判断保持", text)
        self.assertIn("## 待核证据项", text)
        self.assertIn("所选证据块不存在", text)
        self.assertNotIn("来源原文", text)
        self.assertEqual(view, app.read_case(view.case_ref))
        forged = deepcopy(view.latest_report)
        forged["status"] = forged["evidence_check"]["status"] = "COMPLETED"
        forged["evidence_check"]["findings"] = []
        with self.assertRaisesRegex(ValueError, "THESIS_EVIDENCE_CHECK_CHANGED"):
            validate(forged)

    def test_unknown_explicit_source_is_visible_and_partial(self):
        view, _ = self.run_case(SelectionLLM(unknown_source=True))
        self.assertEqual("PARTIAL", view.status)
        self.assertIn("S999", Path(view.report_path).read_text())

    def test_excluded_source_id_cannot_be_redirected_to_a_research_block(self):
        bundle = correction_sources()
        other = deepcopy(bundle["sources"][0])
        other.update(id="E0001", use="sensitivity")
        bundle["sources"].append(other)
        view, app = self.run_case(SelectionLLM(explicit_source="E0001"), bundle=bundle)
        self.assertEqual("PARTIAL", view.status)
        self.assertEqual(["E0001", "S01"], view.latest_report["final_report"]["summary"]["evidence_refs"])
        self.assertEqual(view, app.read_case(view.case_ref))
        execution = next(self.root.glob("application/thesis-executions/*"))
        request = json.loads((execution / "request.json").read_text())
        prefix = CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=16)
        self.assertEqual(13, len(prefix.rows))

    def test_supplied_metric_values_remain_hard_failures(self):
        with self.assertRaises(ApplicationError):
            self.run_case(SelectionLLM(bad_value=True))
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))

    def test_resume_retains_13_calls_including_a_partial_report_without_new_generation(self):
        self.run_case(SelectionLLM(unknown_evidence=True))
        execution = next(self.root.glob("application/thesis-executions/*"))
        request = json.loads((execution / "request.json").read_text())
        completed = CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=16)
        self.assertEqual(13, len(completed.rows))

    def test_reader_stays_standard_library_only_and_detects_report_changes(self):
        view, app = self.run_case()
        python = Path(__file__).parents[1] / ".venv/bin/python"
        code = ("import sys; from pathlib import Path; from finauditgate.application import FinResearchOps; "
                "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); "
                "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v16'; "
                "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        run = subprocess.run([str(python), "-c", code, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        report = Path(view.report_path)
        report.write_bytes(report.read_bytes() + b"changed")
        with self.assertRaises(ApplicationError):
            app.read_case(view.case_ref)

    def test_v13_to_v15_reuses_the_first_twelve_calls_without_redrawing_research(self):
        old_app = FinResearchOps(artifact_root=self.root, researcher=ThesisResearcher(protocol_version=13))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "完整研究与反证。", sources=correction_sources(), review=False)
        with patched_native_runtime(NarrativeLLM()):
            old = old_app.handle(command)
        execution = next(self.root.glob("application/thesis-executions/*"))
        original = Path(old.report_path).read_bytes()
        new_app = FinResearchOps(artifact_root=self.root, researcher=ThesisResearcher(resume_from=execution))
        with patched_native_runtime(SelectionLLM()):
            new = new_app.handle(command)
        self.assertEqual(12, new.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(old.latest_report["model_calls"][:12], new.latest_report["model_calls"][:12])
        self.assertEqual(old.latest_report["effective_forward_calculations"], new.latest_report["effective_forward_calculations"])
        self.assertEqual(original, Path(old.report_path).read_bytes())
        self.assertEqual(old, old_app.read_case(old.case_ref))
