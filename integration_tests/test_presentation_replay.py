"""Explicitly replay captured presentation failures without drawing a new answer."""

from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import Field

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources, support
from test_thesis_quality_flow import QualityLLM, SUMMARY, CAUTIOUS_SUMMARY
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.research_delivery import DeliveryContext
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.research import ResearchThesis


PRESENTATION = "Q3单季资料尚待核实；UFS4.1为资料中的技术版本；研究期限12个月；每交易单位1股普通股。"


def presentation_sources():
    bundle = correction_sources()
    row = bundle["sources"][0]
    row["content"] += " SYNTHETIC_ONLY technical product identifier: UFS4.1."
    row["sha256"] = sha256_hex(row["content"].encode())
    return bundle


class PresentationLLM(QualityLLM):
    final_text: str = SUMMARY + PRESENTATION
    invoked_kinds: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        self.invoked_kinds.append(kind)
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if kind == "FinalResearchReport":
            value = json.loads(result.generations[0].message.content)
            value["summary"]["text"] = self.final_text
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        elif kind in ("QualityReview", "QualityRevision"):
            value = json.loads(result.generations[0].message.content)
            if kind == "QualityReview":
                value["findings"][0]["original_text"] = self.final_text
            else:
                value["edits"][0]["expected_text"] = self.final_text
                value["edits"][0]["replacement_text"] = self.final_text.replace(SUMMARY, CAUTIOUS_SUMMARY)
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class PresentationReplayTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    @staticmethod
    def budget():
        return ModelBudget(ceiling_cny=None, max_input_bytes=524288)

    def run_case(self, model, *, root, budget=None, resume=None, replay=False,
                 reassess=False, review=False, protocol=16):
        app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
            live=budget is not None, budget=budget, resume_from=resume,
            replay_presentation_failure=replay, reassess_final=reassess,
            protocol_version=protocol))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "合成格式恢复：保留原始研究结果。",
            sources=presentation_sources(), review=review)
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        return view, app

    def failed_execution(self, *, final_text=SUMMARY + PRESENTATION, mock_old=True):
        original_root = self.root / "original"
        model, budget = PresentationLLM(final_text=final_text), self.budget()
        current_literal = DeliveryContext._literal

        def old_literal(context, text):
            if "Q3单季" in text:
                raise ValueError("UNBOUND_RESEARCH_NUMBER")
            return current_literal(context, text)

        with patch.object(DeliveryContext, "_literal", old_literal if mock_old else current_literal):
            with self.assertRaises(ApplicationError):
                self.run_case(model, root=original_root, budget=budget)
        execution = next(original_root.glob("application/thesis-executions/*"))
        raw = (execution / "runtime-receipt.json").read_bytes()
        receipt = json.loads(raw)
        self.assertEqual(13, budget.calls)
        self.assertEqual(13, len(receipt["model_calls"]))
        self.assertTrue(all(call["output"] for call in receipt["model_calls"]))
        self.assertEqual({"node": "Portfolio Manager", "kind": "FinalResearchReport",
                          "reason": "UNBOUND_RESEARCH_NUMBER"}, receipt["recovery"]["halted"])
        self.assertFalse(list(original_root.glob("application/thesis-cases/*/case.json")))
        return execution, raw, receipt

    def test_explicit_replay_preserves_all_captured_answers_and_budget_without_new_primary_calls(self):
        execution, original_raw, previous = self.failed_execution()
        source_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        model, budget = PresentationLLM(), self.budget()
        view, app = self.run_case(model, root=self.root / "replayed", budget=budget,
            resume=execution, replay=True)
        self.assertEqual([], model.invoked_kinds)
        self.assertEqual(13, view.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(previous["model_calls"], view.latest_report["model_calls"])
        self.assertEqual(previous["budget"], budget.receipt())
        self.assertEqual(previous["budget"], view.latest_report["budget"])
        self.assertEqual(SUMMARY + PRESENTATION, view.latest_report["final_report"]["summary"]["text"])
        replay = view.latest_report["reused_calls"]["presentation_replay"]
        self.assertEqual(sha256_hex(original_raw), replay["original_runtime_sha256"])
        self.assertEqual(previous["recovery"]["halted"], replay["original_halt"])
        self.assertEqual(previous["model_calls"][-1]["run_id"], replay["captured_final_run_id"])
        self.assertIs(False, replay["new_primary_calls_allowed"])
        self.assertIsNone(view.latest_report["recovery"]["halted"])
        self.assertEqual(view, app.read_case(view.case_ref))
        self.assertEqual(source_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        for marker in ("Q3单季", "UFS4.1", "12个月", "每交易单位1股普通股"):
            self.assertIn(marker, Path(view.report_path).read_text())

    def test_default_resume_keeps_the_original_halt_and_makes_no_new_requests(self):
        execution, original_raw, _ = self.failed_execution()
        model = PresentationLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "default-resume", budget=self.budget(), resume=execution)
        self.assertEqual([], model.invoked_kinds)
        self.assertEqual(original_raw, (execution / "runtime-receipt.json").read_bytes())
        self.assertFalse(list((self.root / "default-resume").glob("application/thesis-cases/*/case.json")))

    def test_real_unbound_financial_amount_cannot_use_presentation_replay(self):
        execution, original_raw, _ = self.failed_execution(final_text="EPS=999美元。", mock_old=False)
        model = PresentationLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "invalid-money", budget=self.budget(),
                resume=execution, replay=True)
        self.assertEqual([], model.invoked_kinds)
        self.assertEqual(original_raw, (execution / "runtime-receipt.json").read_bytes())

    def test_missing_captured_final_cannot_fall_through_to_a_new_request(self):
        execution, original_raw, _ = self.failed_execution()
        incomplete = self.root / "incomplete-copy"
        shutil.copytree(execution, incomplete)
        path = incomplete / "runtime-receipt.json"
        data = json.loads(path.read_bytes())
        data["model_calls"][-1]["output"] = []
        path.write_bytes(canonical_json_bytes(data))
        model = PresentationLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "incomplete-replay", budget=self.budget(),
                resume=incomplete, replay=True)
        self.assertEqual([], model.invoked_kinds)
        self.assertEqual(original_raw, (execution / "runtime-receipt.json").read_bytes())

    def test_nonpresentation_halt_is_not_authorized_by_the_flag(self):
        execution, original_raw, _ = self.failed_execution()
        other = self.root / "different-halt-copy"
        shutil.copytree(execution, other)
        path = other / "runtime-receipt.json"
        data = json.loads(path.read_bytes())
        data["recovery"]["halted"]["reason"] = "THESIS_FORWARD_ASSESSMENT_COVERAGE_INVALID"
        path.write_bytes(canonical_json_bytes(data))
        model = PresentationLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "different-halt-replay", budget=self.budget(),
                resume=other, replay=True)
        self.assertEqual([], model.invoked_kinds)
        self.assertEqual(original_raw, (execution / "runtime-receipt.json").read_bytes())

    def test_replay_flag_rejects_missing_resume_reassessment_and_wrong_protocol(self):
        execution, _, _ = self.failed_execution()
        options = [{"resume": None}, {"resume": execution, "reassess": True},
                   {"resume": execution, "protocol": 13}]
        for index, option in enumerate(options):
            with self.subTest(option=option):
                model = PresentationLLM()
                with self.assertRaises(ApplicationError):
                    self.run_case(model, root=self.root / f"bad-option-{index}",
                        budget=self.budget(), replay=True, **option)
                self.assertEqual([], model.invoked_kinds)

    def test_aftercare_runs_once_each_after_primary_replay_without_resetting_budget(self):
        execution, original_raw, previous = self.failed_execution()
        model, budget = PresentationLLM(), self.budget()
        view, app = self.run_case(model, root=self.root / "reviewed-replay", budget=budget,
            resume=execution, replay=True, review=True)
        self.assertEqual(["QualityReview", "QualityRevision"], model.invoked_kinds)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual(previous["model_calls"], view.latest_report["model_calls"])
        self.assertEqual(13, view.latest_report["budget"]["calls"])
        self.assertEqual(15, budget.calls)
        self.assertEqual(budget.receipt(), view.review["budget_total"])
        self.assertEqual(previous["budget"]["usage"], budget.receipt()["usage"][:13])
        self.assertEqual(CAUTIOUS_SUMMARY + PRESENTATION,
                         view.review["effective"]["final_report"]["summary"]["text"])
        self.assertEqual(original_raw, (execution / "runtime-receipt.json").read_bytes())
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_replayed_case_reopens_under_standard_library_only_python(self):
        execution, _, _ = self.failed_execution()
        view, _ = self.run_case(PresentationLLM(), root=self.root / "stdlib-replay", budget=self.budget(),
            resume=execution, replay=True)
        python = Path(__file__).parents[1] / ".venv/bin/python"
        code = ("import sys; from pathlib import Path; from finauditgate.application import FinResearchOps; "
                "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); "
                "assert v.latest_report['reused_calls']['presentation_replay']['new_primary_calls_allowed'] is False; "
                "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        run = subprocess.run([str(python), "-S", "-c", code, str(self.root / "stdlib-replay"), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
