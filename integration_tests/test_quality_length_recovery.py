"""One proven zero-answer length retry per phase within the global allowance.

SDK-shaped synthetic completions exercise capture, budget, replay and aftercare
without a provider request or any issuer material.
"""

from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion
from pydantic import Field

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources, support
from test_thesis_quality_flow import QualityLLM, SUMMARY, CAUTIOUS_SUMMARY
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import FinResearchOps
from finauditgate.application import thesis_quality as quality
from finauditgate.application.thesis_case import validate
from finauditgate.research import ResearchThesis
from native_support import payload_of


class QualityLengthLLM(QualityLLM):
    length_mode: str = "empty"
    length_attempts: int = 1
    length_kind: str = "QualityReview"
    primary_extras: bool = False
    primary_extra_count: int = 2
    quality_attempts: list[dict] = Field(default_factory=list)
    stage_counts: dict[str, int] = Field(default_factory=dict)
    primary_invocations: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        node = payload_of(messages[-1].content)["node"]
        key = node + ":" + kind
        count = self.stage_counts.get(key, 0) + 1
        self.stage_counts[key] = count
        if kind not in ("QualityReview", "QualityRevision"):
            self.primary_invocations.append(key)
            if (self.primary_extras and kind == "InitialBrief" and count == 1
                    and node in ("Bull Researcher", "Bear Researcher")[:self.primary_extra_count]):
                return self._result("")
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if kind not in ("QualityReview", "QualityRevision"):
            return result
        self.quality_attempts.append({"kind": kind, "effort": self.reasoning_effort,
            "content": result.generations[0].message.content})
        if (self.length_kind != "both" and kind != self.length_kind) or count > self.length_attempts:
            return result
        content = ""
        if self.length_mode == "complete":
            content = result.generations[0].message.content
        elif self.length_mode == "partial":
            content = '{"findings":['
        message = {"role": "assistant", "content": content,
            "reasoning_content": "SYNTHETIC_ONLY exhausted reasoning; no financial facts."}
        if self.length_mode == "refusal":
            message["refusal"] = "SYNTHETIC_REFUSAL"
        elif self.length_mode == "tool":
            message["tool_calls"] = [{"id": "synthetic-tool", "type": "function",
                "function": {"name": "synthetic_unexpected_tool", "arguments": "{}"}}]
        completion = ChatCompletion.model_validate({"id": f"synthetic-length-{kind}-{count}",
            "object": "chat.completion", "created": 0, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "length", "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 65536, "total_tokens": 65636,
                "completion_tokens_details": {"reasoning_tokens": 65536 if not content else 65000}}})
        raise LengthFinishReasonError(completion=completion)


class QualityLengthRecoveryTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    @staticmethod
    def budget():
        return ModelBudget(ceiling_cny=None, max_input_bytes=524288, max_output_tokens=65536)

    def run_case(self, model, *, root=None, resume=None, budget=None):
        root = root or self.root
        budget = budget or self.budget()
        app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
            live=True, budget=budget, resume_from=resume))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "合成复核耗尽恢复：主报告持续可读。",
            sources=correction_sources(), review=True)
        with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        execution = next(root.glob("application/thesis-executions/*"))
        return view, app, budget, execution

    def assert_main_retained(self, view, app):
        validate(view.latest_report)
        self.assertEqual("finresearchops.thesis-case/v16", view.latest_report["schema_version"])
        self.assertEqual(SUMMARY, view.latest_report["final_report"]["summary"]["text"])
        self.assertEqual(13, len(view.latest_report["exchanges"]))
        self.assertTrue(Path(view.report_path).is_file())
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_one_empty_review_length_retries_at_high_and_then_writes_once(self):
        model = QualityLengthLLM()
        view, app, budget, execution = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("finresearchops.thesis-review/v3", view.review["schema_version"])
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual([("QualityReview", "max"), ("QualityReview", "high"), ("QualityRevision", "max")],
            [(r["kind"], r["effort"]) for r in model.quality_attempts])
        self.assertEqual(16, budget.calls)
        self.assertEqual(13, view.latest_report["budget"]["calls"])
        self.assertEqual(budget.receipt(), view.review["budget_total"])
        self.assertEqual([], view.latest_report["recovery"]["attempts"])
        calls = view.review["model_calls"]
        self.assertEqual(3, len(calls))
        self.assertEqual("LengthFinishReasonError", calls[0]["error_type"])
        self.assertEqual(calls[0]["messages"], calls[1]["messages"])
        self.assertEqual(65536, calls[0]["failure_response"]["usage"]["output_tokens"])
        self.assertEqual(["QualityReview", "QualityRevision"], [e["kind"] for e in view.review["exchanges"]])
        self.assertEqual(CAUTIOUS_SUMMARY, view.review["effective"]["final_report"]["summary"]["text"])
        runtime = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual(16, len(runtime["model_calls"]))
        self.assertEqual(budget.receipt(), runtime["budget"])

    def test_two_empty_lengths_stop_without_loop_and_resume_has_no_new_calls(self):
        model = QualityLengthLLM(length_attempts=2)
        view, app, budget, execution = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual(15, budget.calls)
        self.assertEqual(["max", "high"], [r["effort"] for r in model.quality_attempts])
        self.assertEqual(2, len(view.review["model_calls"]))
        self.assertIsNone(view.review["effective"])
        self.assertEqual(view.research_report_path, view.delivery_report_path)
        original = (execution / "runtime-receipt.json").read_bytes()
        again = QualityLengthLLM(length_attempts=0)
        resumed, resumed_app, continued, _ = self.run_case(again, root=self.root / "resume",
            resume=execution, budget=self.budget())
        self.assert_main_retained(resumed, resumed_app)
        self.assertEqual([], again.primary_invocations)
        self.assertEqual([], again.quality_attempts)
        self.assertEqual("PARTIAL", resumed.review["status"])
        self.assertEqual(budget.receipt(), continued.receipt())
        self.assertEqual(view.review["model_calls"], resumed.review["model_calls"])
        self.assertEqual(original, (execution / "runtime-receipt.json").read_bytes())

    def test_complete_valid_json_on_length_is_retained_without_redrawing(self):
        model = QualityLengthLLM(length_mode="complete")
        view, app, budget, _ = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual(["QualityReview", "QualityRevision"], [r["kind"] for r in model.quality_attempts])
        self.assertEqual(15, budget.calls)
        retained = view.review["model_calls"][0]
        self.assertIs(True, retained["retained_length"])
        self.assertEqual("LengthFinishReasonError", retained["error_type"])
        self.assertEqual(model.quality_attempts[0]["content"], retained["output"][0]["content"])
        self.assertEqual(json.loads(model.quality_attempts[0]["content"]), view.review["assessment"])
        self.assertEqual(65536, budget.receipt()["usage"][13]["output_tokens"])

    def test_writer_may_use_the_single_aftercare_retry_when_review_did_not_use_it(self):
        model = QualityLengthLLM(length_kind="QualityRevision")
        view, app, budget, _ = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual([("QualityReview", "max"), ("QualityRevision", "max"), ("QualityRevision", "high")],
            [(r["kind"], r["effort"]) for r in model.quality_attempts])
        self.assertEqual(16, budget.calls)
        self.assertEqual(view.review["model_calls"][1]["messages"], view.review["model_calls"][2]["messages"])

    def test_both_quality_phases_may_retry_once_within_the_global_two_extra_calls(self):
        model = QualityLengthLLM(length_kind="both")
        view, app, budget, _ = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual([("QualityReview", "max"), ("QualityReview", "high"),
                          ("QualityRevision", "max"), ("QualityRevision", "high")],
            [(r["kind"], r["effort"]) for r in model.quality_attempts])
        self.assertEqual(17, budget.calls)
        self.assertEqual(4, len(view.review["model_calls"]))
        self.assertEqual(["QualityReview", "QualityRevision"], [e["kind"] for e in view.review["exchanges"]])
        self.assertEqual([], view.latest_report["recovery"]["attempts"])
        self.assertEqual(CAUTIOUS_SUMMARY, view.review["effective"]["final_report"]["summary"]["text"])
        calls = view.review["model_calls"]
        for offset in (0, 2):
            self.assertEqual(calls[offset]["messages"], calls[offset + 1]["messages"])
            self.assertEqual([1, 2], [c["thesis_stage"]["attempt"] for c in calls[offset:offset + 2]])

    def test_one_primary_extra_and_review_retry_leave_no_writer_retry_allowance(self):
        model = QualityLengthLLM(length_kind="both", primary_extras=True, primary_extra_count=1)
        view, app, budget, _ = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertEqual(1, len(view.latest_report["recovery"]["attempts"]))
        self.assertEqual(14, view.latest_report["budget"]["calls"])
        self.assertEqual([("QualityReview", "max"), ("QualityReview", "high"), ("QualityRevision", "max")],
            [(r["kind"], r["effort"]) for r in model.quality_attempts])
        self.assertEqual(17, budget.calls)
        self.assertEqual(3, len(view.review["model_calls"]))
        self.assertIsNone(view.review["effective"])

    def test_partial_content_refusal_and_tool_output_do_not_qualify_for_zero_answer_retry(self):
        for mode in ("partial", "refusal", "tool"):
            with self.subTest(mode=mode):
                model = QualityLengthLLM(length_mode=mode)
                view, app, budget, _ = self.run_case(model, root=self.root / mode)
                self.assert_main_retained(view, app)
                self.assertEqual("PARTIAL", view.review["status"])
                self.assertEqual(14, budget.calls)
                self.assertEqual(["QualityReview"], [r["kind"] for r in model.quality_attempts])
                self.assertEqual(1, len(view.review["model_calls"]))
                self.assertIsNone(view.review["effective"])

    def test_two_primary_recoveries_exhaust_global_allowance_before_quality_retry(self):
        model = QualityLengthLLM(primary_extras=True)
        view, app, budget, _ = self.run_case(model)
        self.assert_main_retained(view, app)
        self.assertEqual(2, len(view.latest_report["recovery"]["attempts"]))
        self.assertEqual(["EMPTY_RESPONSE", "EMPTY_RESPONSE"],
                         [r["reason"] for r in view.latest_report["recovery"]["attempts"]])
        self.assertEqual(15, view.latest_report["budget"]["calls"])
        self.assertEqual(16, budget.calls)
        self.assertEqual(["QualityReview"], [r["kind"] for r in model.quality_attempts])
        self.assertEqual("PARTIAL", view.review["status"])
        self.assertIsNone(view.review["effective"])

    def test_prior_single_failed_quality_call_resumes_with_one_high_review_and_one_writer(self):
        model = QualityLengthLLM()
        with patch.object(quality, "no_answer_length", return_value=False):
            old, app, old_budget, execution = self.run_case(model)
        self.assert_main_retained(old, app)
        self.assertEqual("PARTIAL", old.review["status"])
        self.assertEqual(14, old_budget.calls)
        self.assertEqual(1, len(old.review["model_calls"]))
        original_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        again = QualityLengthLLM(length_attempts=0)
        resumed, resumed_app, budget, _ = self.run_case(again, root=self.root / "resumed-single",
            resume=execution, budget=self.budget())
        self.assert_main_retained(resumed, resumed_app)
        self.assertEqual("COMPLETED", resumed.review["status"])
        self.assertEqual([], again.primary_invocations)
        self.assertEqual([("QualityReview", "high"), ("QualityRevision", "max")],
            [(r["kind"], r["effort"]) for r in again.quality_attempts])
        self.assertEqual(16, budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], budget.receipt()["usage"][:14])
        self.assertEqual(old.review["model_calls"][0], resumed.review["model_calls"][0])
        self.assertEqual(old.latest_report["model_calls"], resumed.latest_report["model_calls"])
        self.assertEqual(original_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})

    def test_prior_high_review_and_failed_max_writer_resume_only_the_high_writer(self):
        original_predicate = quality.no_answer_length

        def old_single_quality_retry(call):
            return (call.get("thesis_stage", {}).get("kind") != "QualityRevision"
                    and original_predicate(call))

        model = QualityLengthLLM(length_kind="both")
        with patch.object(quality, "no_answer_length", side_effect=old_single_quality_retry):
            old, app, old_budget, execution = self.run_case(model)
        self.assert_main_retained(old, app)
        self.assertEqual("PARTIAL", old.review["status"])
        self.assertEqual(16, old_budget.calls)
        self.assertEqual([("QualityReview", "max"), ("QualityReview", "high"), ("QualityRevision", "max")],
            [(r["kind"], r["effort"]) for r in model.quality_attempts])
        original_files = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        again = QualityLengthLLM(length_attempts=0)
        resumed, resumed_app, budget, new_execution = self.run_case(again, root=self.root / "resumed-writer",
            resume=execution, budget=self.budget())
        self.assert_main_retained(resumed, resumed_app)
        self.assertEqual("COMPLETED", resumed.review["status"])
        self.assertEqual([], again.primary_invocations)
        self.assertEqual([("QualityRevision", "high")],
            [(r["kind"], r["effort"]) for r in again.quality_attempts])
        self.assertEqual(17, budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], budget.receipt()["usage"][:16])
        self.assertEqual(old.review["model_calls"], resumed.review["model_calls"][:3])
        self.assertEqual(old.latest_report["model_calls"], resumed.latest_report["model_calls"])
        self.assertEqual(original_files, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})
        completed_files = {p.relative_to(new_execution): p.read_bytes() for p in new_execution.rglob("*") if p.is_file()}
        replay_model = QualityLengthLLM(length_attempts=0)
        replayed, replayed_app, replay_budget, _ = self.run_case(replay_model, root=self.root / "replayed-complete",
            resume=new_execution, budget=self.budget())
        self.assert_main_retained(replayed, replayed_app)
        self.assertEqual("COMPLETED", replayed.review["status"])
        self.assertEqual([], replay_model.primary_invocations)
        self.assertEqual([], replay_model.quality_attempts)
        self.assertEqual(budget.receipt(), replay_budget.receipt())
        self.assertEqual(resumed.review["model_calls"], replayed.review["model_calls"])
        self.assertEqual(completed_files, {p.relative_to(new_execution): p.read_bytes() for p in new_execution.rglob("*") if p.is_file()})
