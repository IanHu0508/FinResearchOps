"""Exercise correction, effective-number delivery and bounded recovery in the native graph."""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion

from native_support import patched_native_runtime
from test_thesis_flow import ThesisLLM, source_bundle, support, synthetic_forward_draft

from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.native_contract import expected_topology
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from finauditgate.research import ResearchThesis


def correction_sources():
    sources = source_bundle()
    text = ("SYNTHETIC_ONLY: Operating revenue 100 million USD; operating margin 20%; "
            "tax rate 25%; consolidated net income 15 million USD. Profit attributable "
            "to noncontrolling owners is 2 million USD and parent net income is 13 million USD. "
            "This synthetic source does not support a real investment opinion.")
    sources["sources"][0].update(content=text, sha256=sha256_hex(text.encode()))
    return sources


class CorrectionLLM(ThesisLLM):
    rating: str = "REVIEW"
    correct_nci: bool = True
    malformed: str | None = None
    truncations: int = 0
    truncate_revision: bool = False
    max_tokens: int = 64

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema is not None else None
        output_limit = kwargs.get("max_tokens", self.max_tokens)
        if kind not in ("UnderwritingDraft", "ForwardRevision", "FinalResearchReport"):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            self.requests[-1]["max_tokens"] = output_limit
            return result
        payload = json.loads(messages[-1].content)
        self.requests.append({"node": payload["node"], "payload": deepcopy(payload), "schema": kind,
            "text": "\n".join(m.content for m in messages), "reasoning_effort": self.reasoning_effort,
            "structured_method": kwargs.get("synthetic_method"), "max_tokens": output_limit})
        attempt = sum(row["schema"] == kind for row in self.requests)
        if ((kind == "FinalResearchReport" and attempt <= self.truncations)
                or (kind == "ForwardRevision" and self.truncate_revision)):
            raw = ChatCompletion.model_validate({"id": f"synthetic-length-{kind}-{attempt}",
                "object": "chat.completion", "created": 0, "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": "length", "message": {
                    "role": "assistant", "content": '{"rating":"REVIEW","summary":',
                    "reasoning_content": "SYNTHETIC_INTERRUPTED_REASONING"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": output_limit,
                    "total_tokens": 100 + output_limit,
                    "completion_tokens_details": {"reasoning_tokens": output_limit - 2}}})
            raise LengthFinishReasonError(completion=raw)
        if kind == "UnderwritingDraft":
            value = synthetic_forward_draft()
            for scenario in value["scenarios"]:
                attribution = scenario["noncontrolling_attribution"]
                attribution["nature"] = "loss" if self.correct_nci else "profit"
                attribution["amount"].update(value=2.0,
                    reason="ORIGINAL_WRONG_NCI_REASON" if self.correct_nci else "SYNTHETIC_UNCHANGED_NCI")
            return self._result(json.dumps(value))
        if kind == "ForwardRevision":
            changes = []
            if self.correct_nci:
                for scenario in payload["forward_draft"]["scenarios"]:
                    changes.append({"scenario_id": scenario["scenario_id"], "field": "noncontrolling_attribution",
                        "expected_before": {"nature": "loss", "amount": 2.0},
                        "replacement": {"nature": "profit", "amount": {"value": 2.0,
                            "basis_type": "analyst_assumption", "reason": "CORRECTED_NCI_SOURCE_BASIS",
                            "evidence_refs": ["S01"]}}, "correction_basis": "source_misread",
                        "reason": "少数股东盈利应从合并净利润扣减；不改变经营与现金假设。", "evidence_refs": ["S01"]})
            value = {"changes": changes,
                "claim_assessments": [{"claim_id": claim["id"], "disposition": "conditional",
                    "reason": "合成经营条件仍需验证。"} for claim in payload["updated_claims"]],
                "belief_updates": [{"belief_id": belief["belief_id"], "status": "revise" if i == 0 else "maintain",
                    "new_statement": "少数股东盈利扣减后的归母口径。" if i == 0 else None,
                    "update_basis": "reasoning_correction" if i == 0 else "no_new_basis",
                    "reason": "合成归属纠正。" if i == 0 else "未出现改变其经营机制的新事实。",
                    "financial_implication": "归母盈利与现金起算口径须分开。", "evidence_refs": ["S01"]}
                    for i, belief in enumerate(payload["independent_beliefs"])],
                "unresolved_issues": ["合成预测不构成真实投资意见。"]}
            if self.malformed == "old_value":
                value["changes"][0]["expected_before"]["amount"] = 999.0
            elif self.malformed == "change_ref":
                value["changes"][0]["evidence_refs"] = ["S999"]
            elif self.malformed == "replacement_ref":
                value["changes"][0]["replacement"]["amount"]["evidence_refs"] = ["S999"]
            return self._result(json.dumps(value))

        def block(text, *metrics):
            return {"text": text, "evidence_refs": ["S01"],
                "metrics": [{"scenario_id": "F1", "metric": metric} for metric in metrics]}

        value = {"rating": self.rating,
            "summary": block("有效归母口径需要扣减少数股东盈利；经营现金流仍从合并净利起算。",
                             "parent_net_income", "eps_per_traded_unit", "operating_cash_flow"),
            "financial_analysis": {
                "operating_performance": block("合成收入与利润率假设保持原样。", "revenue", "operating_margin"),
                "earnings_quality": block("修正归属口径后单列合并与母公司股东盈利。",
                    "consolidated_net_income", "noncontrolling_attribution_effect", "parent_net_income"),
                "cash_and_capital_allocation": block("归属修正不改合并现金；资本购买仅扣一次。",
                    "operating_cash_flow", "cash_after_capex_proxy"),
                "valuation_and_price_requirements": block("条件价格需要相应经营假设兑现；并非公允价值。",
                    "exit_price_per_traded_unit", "price_only_break_even_pe")},
            "strongest_counterevidence": block("还需检验经营假设是否持续；本测试不验证预测准确性。"),
            "scenario_assessments": [{"scenario_id": scenario["scenario_id"], "disposition": "conditional",
                "reason": "以有效参数推演，但经营假设尚需验证。", "what_changes_the_view": "后续经营观察。"}
                for scenario in payload["effective_forward_draft"]["scenarios"]],
            "limitations": ["合成模型响应仅用于工程验证。"]}
        if self.malformed == "metric":
            value["summary"]["metrics"][0]["metric"] = "invented_fair_value"
        elif self.malformed == "metric_scenario":
            value["summary"]["metrics"][0]["scenario_id"] = "F3"
        elif self.malformed == "metric_value":
            value["summary"]["metrics"][0]["value"] = 999.0
        elif self.malformed == "final_ref":
            value["summary"]["evidence_refs"] = ["S999"]
        return self._result(json.dumps(value))


class ThesisCorrectionTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model=None, *, root=None, live=False, review=False, budget=None, **context):
        root = root or self.root
        model = model or CorrectionLLM()
        command = ResearchThesis("AURORA", date(2026, 3, 2), "经营与价格是否支持投资？",
            sources=correction_sources(), review=review, **context)
        application = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(live=live, budget=budget))
        with patched_native_runtime(model), patch.object(application._gate, "run",
                side_effect=AssertionError("CORE_NOT_A_PREREQUISITE")), \
                patch("finauditgate.adapters.model_http.model_http_client",
                      return_value=SimpleNamespace(close=lambda: None)):
            view = application.handle(command)
        return view, model, application

    def test_native_graph_delivers_corrected_numbers_and_preserves_original(self):
        view, model, _ = self.run_case()
        record = view.latest_report
        self.assertEqual("finresearchops.thesis-case/v11", record["schema_version"])
        self.assertEqual(expected_topology(), record["topology"])
        self.assertEqual(13, len(record["exchanges"]))
        self.assertEqual(13, len(record["model_calls"]))
        self.assertEqual(["UnderwritingDraft", "ForwardRevision", "FinalResearchReport"],
                         [row["schema"] for row in model.requests[-3:]])
        original, effective = record["forward_draft"], record["effective_forward_draft"]
        self.assertEqual("loss", original["scenarios"][0]["noncontrolling_attribution"]["nature"])
        self.assertEqual("profit", effective["scenarios"][0]["noncontrolling_attribution"]["nature"])
        self.assertEqual(2, len(record["applied_changes"]))
        old = record["forward_calculations"]["scenario_results"][0]
        new = record["effective_forward_calculations"]["scenario_results"][0]
        self.assertEqual((17, 1.7), (old["parent_net_income"], old["eps_per_traded_unit"]))
        self.assertEqual((13, 1.3), (new["parent_net_income"], new["eps_per_traded_unit"]))
        self.assertEqual(old["operating_cash_flow"], new["operating_cash_flow"])
        self.assertEqual((16, 11), (new["operating_cash_flow"], new["cash_after_capex_proxy"]))
        final_payload = model.requests[-1]["payload"]
        self.assertEqual(effective, final_payload["effective_forward_draft"])
        self.assertEqual(record["effective_forward_calculations"], final_payload["effective_forward_calculations"])
        self.assertNotIn("forward_draft", final_payload)
        self.assertNotIn("forward_calculations", final_payload)
        self.assertNotIn("ORIGINAL_WRONG_NCI_REASON", json.dumps(final_payload))
        self.assertIn("ORIGINAL_WRONG_NCI_REASON", json.dumps(record["forward_draft"]))

    def test_no_change_preserves_exact_draft_and_calculations(self):
        view, _, _ = self.run_case(CorrectionLLM(correct_nci=False))
        record = view.latest_report
        self.assertEqual([], record["forward_revision"]["changes"])
        self.assertEqual([], record["applied_changes"])
        self.assertEqual(record["forward_draft"], record["effective_forward_draft"])
        self.assertEqual(record["forward_calculations"], record["effective_forward_calculations"])

    def test_user_expectation_and_initial_rating_do_not_reach_final(self):
        final_inputs = []
        for index, (expectation, prior_rating) in enumerate((("EXPECT_BUY_MARKER", "Buy"), ("EXPECT_SELL_MARKER", "Sell"))):
            view, model, _ = self.run_case(CorrectionLLM(independent_rating=prior_rating),
                root=self.root / str(index), user_view=expectation,
                hypotheses=("MATERIAL_REVENUE_HYPOTHESIS",), research_constraints=("LIQUIDITY_RESEARCH_SCOPE",))
            final = model.requests[-1]
            final_inputs.append(final["text"])
            self.assertNotIn(expectation, final["text"])
            self.assertNotIn("INDEPENDENT_RATIONALE", final["text"])
            self.assertNotIn("independent_assessment", final["payload"])
            self.assertNotIn("independent_beliefs", final["payload"])
            self.assertIn("MATERIAL_REVENUE_HYPOTHESIS", final["text"])
            self.assertIn("LIQUIDITY_RESEARCH_SCOPE", final["text"])
            self.assertEqual(prior_rating, view.latest_report["rating_comparison"]["before"])
            self.assertIn(expectation, Path(view.report_path).read_text())
        self.assertEqual(final_inputs[0], final_inputs[1])

    def test_main_report_has_effective_metrics_and_separate_process_appendix(self):
        view, model, _ = self.run_case(review=True)
        report = Path(view.report_path)
        current = report.read_text()
        process = (report.parent / "process-record.md").read_text()
        self.assertIn("[过程记录附录](process-record.md)", current)
        self.assertIn("| 归属于母公司股东的净利润 | 13 |", current)
        self.assertIn("| 每交易单位摊薄盈利（EPS） | 1.3 |", current)
        self.assertNotIn("PRIOR_RATING_MARKER", current)
        self.assertNotIn("TRADER_ACTION_MARKER", current)
        self.assertIn("PRIOR_RATING_MARKER", process)
        self.assertIn("TRADER_ACTION_MARKER", process)
        self.assertIn("ORIGINAL_WRONG_NCI_REASON", process)
        self.assertEqual("DataReview", model.requests[-1]["schema"])
        self.assertEqual(current, model.requests[-1]["payload"]["saved_report"])
        self.assertEqual(13, len(view.latest_report["model_calls"]))

    def test_standard_library_reader_reopens_v11_without_model_runtime(self):
        view, _, _ = self.run_case()
        python = Path(__file__).parents[1] / ".venv/bin/python"
        code = ("from pathlib import Path; import sys; from finauditgate.application import FinResearchOps; "
                "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); "
                "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v11'; "
                "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','langchain_core','tradingagents')); "
                "print(v.status)")
        run = subprocess.run([str(python), "-c", code, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
            capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("AWAITING_REVIEW", run.stdout)

    def test_one_sdk_length_retries_only_final_at_high_with_unchanged_limit(self):
        budget = ModelBudget(ceiling_cny=None, max_output_tokens=64)
        view, model, _ = self.run_case(CorrectionLLM(truncations=1), live=True, budget=budget)
        record = view.latest_report
        self.assertEqual(14, len(model.requests))
        self.assertEqual(1, sum(r["schema"] == "UnderwritingDraft" for r in model.requests))
        self.assertEqual(1, sum(r["schema"] == "ForwardRevision" for r in model.requests))
        finals = [r for r in model.requests if r["schema"] == "FinalResearchReport"]
        self.assertEqual(["max", "high"], [r["reasoning_effort"] for r in finals])
        self.assertEqual([64, 64], [r["max_tokens"] for r in finals])
        self.assertEqual(finals[0]["text"], finals[1]["text"])
        self.assertEqual(["TRUNCATED", "COMPLETED"], [r["status"] for r in record["final_generation"]])
        self.assertEqual(14, record["budget"]["calls"])
        self.assertEqual(14, len(record["budget"]["usage"]))
        failed = record["model_calls"][-2]
        self.assertEqual("LengthFinishReasonError", failed["error_type"])
        self.assertNotIn("output", failed)
        failure = failed["failure_response"]
        self.assertEqual(62, failure["provider_usage"]["completion_tokens_details"]["reasoning_tokens"])
        self.assertEqual('{"rating":"REVIEW","summary":', failure["provider_response"]["choices"][0]["message"]["content"])
        execution = next(self.root.glob("application/thesis-executions/*"))
        self.assertTrue((execution / "model-traces/call-013-failure-response.json").exists())
        self.assertEqual(64, budget.max_output_tokens)
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))

    def test_two_sdk_lengths_preserve_failures_and_do_not_save_a_case(self):
        budget = ModelBudget(ceiling_cny=None, max_output_tokens=64)
        model = CorrectionLLM(truncations=2)
        with self.assertRaises(ApplicationError) as raised:
            self.run_case(model, live=True, budget=budget)
        self.assertIsInstance(raised.exception.__cause__, LengthFinishReasonError)
        self.assertEqual(14, len(model.requests))
        self.assertEqual([], list(self.root.glob("application/thesis-cases/*/case.json")))
        execution = next(self.root.glob("application/thesis-executions/*"))
        receipt = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual(["TRUNCATED", "TRUNCATED"], [r["status"] for r in receipt["final_generation"]])
        self.assertEqual(2, len(list((execution / "model-traces").glob("*-failure-response.json"))))
        self.assertEqual([64, 64], [r["max_tokens"] for r in model.requests[-2:]])
        self.assertEqual(14, len(receipt["budget"]["usage"]))
        self.assertIsNotNone(receipt["budget"]["uncached_price_estimate_cny"])
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_TRUNCATED"):
            budget.reserve("Forbidden third final attempt.")

    def test_revision_truncation_does_not_trigger_final_only_retry(self):
        model = CorrectionLLM(truncate_revision=True)
        with self.assertRaises(ApplicationError) as raised:
            self.run_case(model, live=True, budget=ModelBudget(ceiling_cny=None, max_output_tokens=64))
        self.assertIsInstance(raised.exception.__cause__, LengthFinishReasonError)
        self.assertEqual(12, len(model.requests))
        self.assertEqual(1, sum(r["schema"] == "ForwardRevision" for r in model.requests))
        self.assertNotIn("FinalResearchReport", [r["schema"] for r in model.requests])

    def test_incorrect_old_value_or_unknown_correction_sources_stop_before_final(self):
        for error in ("old_value", "change_ref", "replacement_ref"):
            with self.subTest(error=error):
                model = CorrectionLLM(malformed=error)
                target = self.root / error
                with self.assertRaises(ApplicationError):
                    self.run_case(model, root=target)
                self.assertEqual(12, len(model.requests))
                self.assertEqual([], list(target.glob("application/thesis-cases/*/case.json")))
                self.assertNotIn("FinalResearchReport", [r["schema"] for r in model.requests])

    def test_illegal_or_invented_final_metric_and_unknown_reference_are_rejected(self):
        for error in ("metric", "metric_scenario", "metric_value", "final_ref"):
            with self.subTest(error=error):
                target = self.root / error
                model = CorrectionLLM(malformed=error)
                with self.assertRaises(ApplicationError):
                    self.run_case(model, root=target)
                self.assertEqual(13, len(model.requests))
                self.assertEqual([], list(target.glob("application/thesis-cases/*/case.json")))

    def test_tampered_effective_calculation_is_rejected_by_binding_and_reopen(self):
        view, _, application = self.run_case()
        changed = deepcopy(view.latest_report)
        changed["effective_forward_calculations"]["scenario_results"][0]["parent_net_income"] = 999
        with self.assertRaisesRegex(ValueError, "THESIS_EFFECTIVE_NUMBERS_NOT_DELIVERED|THESIS_EFFECTIVE_REVISION_MISMATCH"):
            validate(changed)
        path = Path(view.report_path).parent / "case.json"
        path.write_bytes(canonical_json_bytes(changed))
        with self.assertRaisesRegex(ApplicationError, "THESIS_CASE_INTEGRITY_FAILED"):
            application.read_case(view.case_ref)

    def test_tampered_current_report_is_rejected_on_reopen(self):
        view, _, application = self.run_case()
        path = Path(view.report_path)
        path.write_text(path.read_text() + "\nSYNTHETIC_TAMPERED_CURRENT_REPORT")
        with self.assertRaisesRegex(ApplicationError, "THESIS_CASE_INTEGRITY_FAILED"):
            application.read_case(view.case_ref)

    def test_tampered_process_appendix_is_rejected_on_reopen(self):
        view, _, application = self.run_case()
        path = Path(view.report_path).parent / "process-record.md"
        path.write_text(path.read_text() + "\nSYNTHETIC_TAMPERED_ORIGINAL_PROCESS")
        with self.assertRaisesRegex(ApplicationError, "THESIS_CASE_INTEGRITY_FAILED"):
            application.read_case(view.case_ref)


if __name__ == "__main__":
    unittest.main()
