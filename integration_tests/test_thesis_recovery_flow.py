"""Bounded faults across the real graph, saved Case, reader and resume path."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APITimeoutError, LengthFinishReasonError
from openai.types.chat import ChatCompletion
from pydantic import Field

from native_support import patched_native_runtime
from test_thesis_delivery import SelectionLLM
from test_thesis_correction import correction_sources, support
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.thesis_recovery import FORMAT_COMMENT
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.research import ResearchThesis


class RecoveryLLM(SelectionLLM):
    faults: dict[str, str] = Field(default_factory=dict)
    stage_calls: dict[str, int] = Field(default_factory=dict)
    mutate_repair: bool = False
    harmless_format: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        payload = json.loads(messages[-1].content)
        kind = schema.__name__ if schema else ""
        key = payload.get("node", "") + ":" + kind
        count = self.stage_calls.get(key, 0) + 1
        self.stage_calls[key] = count
        fault = self.faults.get(key)
        if fault == "always_empty" or (fault == "empty" and count == 1):
            return self._result("")
        if fault in ("timeout", "connection") and count == 1:
            request = httpx.Request("POST", "https://synthetic.invalid")
            raise APITimeoutError(request=request) if fault == "timeout" else APIConnectionError(request=request)
        if fault == "length" and count == 1:
            raw = ChatCompletion.model_validate({"id": "synthetic-length", "object": "chat.completion", "created": 0,
                "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "length", "message": {
                    "role": "assistant", "content": "", "reasoning_content": "SYNTHETIC exhausted reasoning"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 65536, "total_tokens": 65636,
                    "completion_tokens_details": {"reasoning_tokens": 65536}}})
            raise LengthFinishReasonError(completion=raw)
        marker = "缺项路径及原响应：\n"
        if marker in messages[0].content:
            repair = json.loads(messages[0].content.split(marker, 1)[1])
            value = repair["previous_response"]
            for key2, i, field in repair["missing_reason_paths"]:
                value[key2][i][field] = "SYNTHETIC：原资料支持该经营机制，仍需后续反证验证。"
            if self.mutate_repair:
                value["updates"][0]["status"] = "withdraw"
            return self._result(json.dumps(value))
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if schema:
            value = json.loads(result.generations[0].message.content)
            if fault == "unbound_final":
                value["summary"]["text"] = "EPS=999美元。"
            if fault == "complete_length":
                value["rating"] = "Sell" if count == 1 else "Buy"
                if count == 1:
                    completion = ChatCompletion.model_validate({"id": "synthetic-complete-length", "object": "chat.completion", "created": 0,
                        "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "length", "message": {
                            "role": "assistant", "content": json.dumps(value, ensure_ascii=False)}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 65536, "total_tokens": 65636}})
                    raise LengthFinishReasonError(completion=completion)
            if fault == "missing" and count == 1:
                del value["updates"][0]["reason"]
            if self.harmless_format:
                if kind == "InitialBrief":
                    value["claims"][0]["evidence_refs_note"] = None
                elif kind == "ExecutionReview":
                    value["proposal"].update(_comment=FORMAT_COMMENT, stop_loss_note=None)
                elif kind == "ResearchEvaluation":
                    value["plan"]["strategic_actions_note"] = "conditional"
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class ThesisRecoveryFlowTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model, *, root=None, resume=None, budget=None, reassess=False, interrupt_backoff=False):
        app = FinResearchOps(artifact_root=root or self.root, researcher=ThesisResearcher(
            live=budget is not None, budget=budget, resume_from=resume, reassess_final=reassess))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "合成研究：保持完整内容。", sources=correction_sources(), review=False)
        with patched_native_runtime(model), patch("finauditgate.adapters.thesis_invocation.sleep", side_effect=KeyboardInterrupt if interrupt_backoff else None), \
                patch("finauditgate.adapters.model_http.model_http_client", return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        return view, app

    def test_format_normalization_preserves_semantic_note_without_extra_calls(self):
        view, app = self.run_case(RecoveryLLM(harmless_format=True))
        self.assertEqual("finresearchops.thesis-case/v16", view.latest_report["schema_version"])
        self.assertEqual(13, len(view.latest_report["model_calls"]))
        self.assertEqual([], view.latest_report["recovery"]["attempts"])
        self.assertIn("conditional", view.latest_report["research_evaluation"]["plan"]["strategic_actions"])
        self.assertTrue(any("strategic_actions_note" in o.get("content", "") for c in view.latest_report["model_calls"] for o in c.get("output", [])))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_missing_reason_is_filled_without_changing_other_fields_and_replays(self):
        model = RecoveryLLM(faults={"Bull Researcher:RevisionBrief": "missing"})
        view, app = self.run_case(model)
        record = view.latest_report
        self.assertEqual(14, len(record["model_calls"]))
        recovery = record["recovery"]["attempts"][0]
        self.assertEqual("MISSING_REASON", recovery["reason"])
        calls = {c["run_id"]: c for c in record["model_calls"]}
        old = json.loads(calls[recovery["failed_run_id"]]["output"][0]["content"])
        new = json.loads(calls[recovery["retry_run_id"]]["output"][0]["content"])
        self.assertTrue(new["updates"][0].pop("reason"))
        self.assertEqual(old, new)
        self.assertEqual(view, app.read_case(view.case_ref))
        execution = next(self.root.glob("application/thesis-executions/*"))
        repeat = RecoveryLLM()
        replayed, _ = self.run_case(repeat, root=self.root / "resume", resume=execution)
        self.assertEqual({}, repeat.stage_calls)
        self.assertEqual(13, replayed.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(record["model_calls"], replayed.latest_report["model_calls"])
        forged = deepcopy(record)
        forged["recovery"]["attempts"][0]["missing_reason_paths"] = [["updates", 1, "reason"]]
        with self.assertRaises(ValueError):
            validate(forged)

    def test_repair_that_changes_judgment_is_rejected_and_cannot_loop_on_resume(self):
        with self.assertRaises(ApplicationError):
            self.run_case(RecoveryLLM(faults={"Bull Researcher:RevisionBrief": "missing"}, mutate_repair=True))
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))
        execution = next(self.root.glob("application/thesis-executions/*"))
        model = RecoveryLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "resume", resume=execution)
        self.assertEqual({}, model.stage_calls)

    def test_empty_response_recovers_once_and_two_empty_responses_halt(self):
        view, _ = self.run_case(RecoveryLLM(faults={"Bull Researcher:InitialBrief": "empty"}), root=self.root / "one")
        self.assertEqual("EMPTY_RESPONSE", view.latest_report["recovery"]["attempts"][0]["reason"])
        model = RecoveryLLM(faults={"Bull Researcher:InitialBrief": "always_empty"})
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "two")
        self.assertEqual(2, model.stage_calls["Bull Researcher:InitialBrief"])
        execution = next((self.root / "two").glob("application/thesis-executions/*"))
        never = RecoveryLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(never, root=self.root / "resume", resume=execution)
        self.assertEqual({}, never.stage_calls)

    def test_global_two_recoveries_cannot_be_reset_by_resume(self):
        faults = {"Bull Researcher:InitialBrief": "empty", "Bear Researcher:InitialBrief": "empty", "Trader:ExecutionReview": "empty"}
        model = RecoveryLLM(faults=faults)
        with self.assertRaises(ApplicationError):
            self.run_case(model)
        self.assertEqual(1, model.stage_calls["Trader:ExecutionReview"])
        execution = next(self.root.glob("application/thesis-executions/*"))
        state = json.loads((execution / "runtime-receipt.json").read_text())["recovery"]
        self.assertEqual(2, len(state["attempts"]))
        again = RecoveryLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(again, root=self.root / "resume", resume=execution)
        self.assertEqual({}, again.stage_calls)

    def test_length_recovery_is_high_and_keeps_all_billed_usage(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288, max_output_tokens=65536)
        view, _ = self.run_case(RecoveryLLM(faults={"Bear Researcher:RevisionBrief": "length"}), budget=budget)
        recovery = view.latest_report["recovery"]["attempts"][0]
        self.assertEqual("LENGTH", recovery["reason"])
        self.assertEqual("high", recovery["retry_effort"])
        self.assertEqual(14, budget.calls)
        self.assertTrue(any(u.get("output_tokens") == 65536 for u in budget.usage))
        self.assertEqual(65536, budget.max_output_tokens)

    def test_transport_recovery_keeps_unknown_reserve_and_resume_budget(self):
        for fault in ("timeout", "connection"):
            root = self.root / fault
            budget = ModelBudget(ceiling_cny=None, max_calls=14, max_input_bytes=524288)
            view, _ = self.run_case(RecoveryLLM(faults={"Bull Researcher:InitialBrief": fault}), root=root, budget=budget)
            self.assertEqual(14, budget.calls)
            self.assertIsNone(budget.receipt()["uncached_price_estimate_cny"])
            execution = next(root.glob("application/thesis-executions/*"))
            restored = ModelBudget(ceiling_cny=None, max_calls=14, max_input_bytes=524288)
            replayed, _ = self.run_case(RecoveryLLM(), root=self.root / (fault + "-resume"), resume=execution, budget=restored)
            self.assertEqual(view.latest_report["budget"], replayed.latest_report["budget"])
            self.assertEqual(budget.receipt(), restored.receipt())
            with self.assertRaisesRegex(ValueError, "MODEL_CALL_LIMIT"):
                restored.reserve("must not become a free third attempt")

    def test_explicit_final_reassessment_retains_prior_recovery_and_cumulative_budget(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        old, _ = self.run_case(RecoveryLLM(faults={"Portfolio Manager:FinalResearchReport": "empty"}), budget=budget)
        execution = next(self.root.glob("application/thesis-executions/*"))
        continued = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        new, app = self.run_case(RecoveryLLM(), root=self.root / "reassess", resume=execution, budget=continued, reassess=True)
        self.assertEqual(12, new.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(1, len(new.latest_report["recovery"]["attempts"]))
        self.assertEqual(2, len(new.latest_report["recovery"]["retired_final_calls"]))
        self.assertEqual(budget.calls + 1, continued.calls)
        self.assertEqual(new, app.read_case(new.case_ref))

    def test_interrupted_backoff_resumes_reserved_retry_without_new_primary_attempt(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        with self.assertRaises(KeyboardInterrupt):
            self.run_case(RecoveryLLM(faults={"Bull Researcher:InitialBrief": "timeout"}), budget=budget, interrupt_backoff=True)
        execution = next(self.root.glob("application/thesis-executions/*"))
        before = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertIsNone(before["recovery"]["attempts"][0]["retry_run_id"])
        self.assertIsNone(before["recovery"]["halted"])
        restored = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        model = RecoveryLLM()
        view, app = self.run_case(model, root=self.root / "resume", resume=execution, budget=restored)
        self.assertEqual(1, model.stage_calls["Bull Researcher:InitialBrief"])
        self.assertEqual(14, restored.calls)
        self.assertEqual(before["model_calls"][0], view.latest_report["model_calls"][0])
        self.assertEqual(2, view.latest_report["model_calls"][1]["thesis_stage"]["attempt"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_financial_hard_failure_cannot_be_regenerated_by_ordinary_resume(self):
        with self.assertRaises(ApplicationError):
            self.run_case(RecoveryLLM(faults={"Portfolio Manager:FinalResearchReport": "unbound_final"}))
        execution = next(self.root.glob("application/thesis-executions/*"))
        receipt = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual("UNBOUND_RESEARCH_NUMBER", receipt["recovery"]["halted"]["reason"])
        model = RecoveryLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "resume", resume=execution)
        self.assertEqual({}, model.stage_calls)

    def test_final_pending_retry_preserves_both_generation_records(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        with self.assertRaises(KeyboardInterrupt):
            self.run_case(RecoveryLLM(faults={"Portfolio Manager:FinalResearchReport": "timeout"}),
                          budget=budget, interrupt_backoff=True)
        execution = next(self.root.glob("application/thesis-executions/*"))
        before = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual(13, budget.calls)
        restored = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        model = RecoveryLLM()
        view, app = self.run_case(model, root=self.root / "resume", resume=execution, budget=restored)
        self.assertEqual({"Portfolio Manager:FinalResearchReport": 1}, model.stage_calls)
        self.assertEqual(14, restored.calls)
        self.assertEqual(before["final_generation"], view.latest_report["final_generation"][:1])
        self.assertEqual(["FAILED", "COMPLETED"], [r["status"] for r in view.latest_report["final_generation"]])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_complete_length_answer_is_kept_without_changing_sell_to_buy(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288, max_output_tokens=65536)
        model = RecoveryLLM(faults={"Portfolio Manager:FinalResearchReport": "complete_length"})
        view, app = self.run_case(model, budget=budget)
        record = view.latest_report
        self.assertEqual("Sell", record["signal"])
        self.assertEqual(1, model.stage_calls["Portfolio Manager:FinalResearchReport"])
        self.assertEqual(13, budget.calls)
        self.assertEqual([], record["recovery"]["attempts"])
        self.assertEqual("RETAINED_LENGTH", record["final_generation"][0]["status"])
        self.assertTrue(record["model_calls"][-1]["retained_length"])
        self.assertIn("failure_response", record["model_calls"][-1])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_resume_cannot_lower_both_reserve_copies_or_switch_runtime_mode(self):
        budget = ModelBudget(ceiling_cny=None, max_input_bytes=524288)
        self.run_case(RecoveryLLM(faults={"Bull Researcher:InitialBrief": "timeout"}), budget=budget)
        execution = next(self.root.glob("application/thesis-executions/*"))
        original = json.loads((execution / "runtime-receipt.json").read_text())
        request = json.loads((execution / "request.json").read_text())
        edited = deepcopy(original)
        edited["budget"]["reserved_upper_cny"] = "0.03"
        edited["budget_checkpoint"]["receipt"]["reserved_upper_cny"] = "0.03"
        read = Path.read_bytes
        with patch.object(Path, "read_bytes", lambda p: json.dumps(edited).encode() if p == execution / "runtime-receipt.json" else read(p)):
            with self.assertRaisesRegex(ValueError, "RESERVATION_MISMATCH"):
                CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=16)
        model = RecoveryLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "offline-resume", resume=execution)
        self.assertEqual({}, model.stage_calls)
