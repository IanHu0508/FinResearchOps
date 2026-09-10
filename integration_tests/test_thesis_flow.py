"""Run the pinned native graph; inspect real message boundaries with fake I/O."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda
from native_support import NativeSyntheticLLM, patched_native_runtime

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import test_research_workflow as support

from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.adapters.thesis_protocol import ThesisSession
from finauditgate.adapters.thesis_protocol import schemas
from finauditgate.adapters.model_http import model_http_client
from finauditgate.adapters.thesis_responses import response_candidate
from finauditgate.adapters.native_contract import expected_topology
from finauditgate.application import FinResearchOps, ApplicationError
from finauditgate.application.thesis_case import validate
from finauditgate.core.artifacts import sha256_hex, canonical_json_bytes
from finauditgate.research import ResearchThesis


def source_bundle():
    content = "SYNTHETIC: REVENUE_AND_EQUITY_PRESERVED; Revenue 100 USD; equity 80 USD; valuation uncertain."
    return {"schema_version": "finresearchops.thesis-sources/v1", "symbol": "AURORA", "as_of": "2026-03-02",
            "identity": {"company_name": "Aurora Synthetic Company", "quote_type": "EQUITY"},
            "sources": [{"id": "S01", "origin": "SYNTHETIC_ONLY", "availability_note": "SYNTHETIC_ONLY",
                         "content": content, "sha256": sha256_hex(content.encode())}]}


class ThesisLLM(NativeSyntheticLLM):
    reasoning_effort: str | None = None
    rating: str = "Buy"
    fail_review: bool = False
    omit_update: bool = False
    split_revision: bool = False
    fail_second_revision: bool = False
    markdown_manager: bool = False

    def with_structured_output(self, schema, **kwargs):
        def parsed(message):
            try:
                return {"raw": message, "parsed": schema.model_validate_json(message.content), "parsing_error": None}
            except ValueError as exc:
                return {"raw": message, "parsed": None, "parsing_error": exc}
        return self.bind(synthetic_schema=schema, synthetic_method=kwargs.get("method")) | RunnableLambda(parsed)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if kwargs.get("tools"):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        payload = json.loads(messages[-1].content)
        node = payload["node"]
        schema = kwargs.get("synthetic_schema")
        self.requests.append({"node": node, "payload": deepcopy(payload), "schema": schema.__name__ if schema else None,
                              "text": "\n".join(m.content for m in messages), "reasoning_effort": self.reasoning_effort,
                              "structured_method": kwargs.get("synthetic_method")})
        if schema is None:
            return self._result("SYNTHETIC facts and uncertainties only.")
        name = schema.__name__
        ref = ["S01"]
        if name == "InitialBrief":
            value = {"claims": [{"statement": node + "_INITIAL_" + str(i), "business_mechanism": "Synthetic financial mechanism.",
                "evidence_refs": ref, "would_change_mind": "Subsequent evidence.", "uncertainty": "Synthetic assumptions."} for i in (1, 2)]}
        elif name == "RevisionBrief":
            if self.fail_second_revision and node == "Bear Researcher":
                raise RuntimeError("SYNTHETIC_INTERRUPTION_AFTER_COMPLETED_PREFIX")
            value = {"updates": [{"claim_id": c["id"], "status": "revise" if i == 1 else "maintain",
                "updated_statement": node + "_REVISED" if i == 1 else None,
                "reason": "COUNTEREVIDENCE_CHANGED_REASON" if i == 1 else "Counterevidence insufficient because of scope.",
                "evidence_refs": ref, "would_change_mind": "Observe operating cash flow."}
                for i, c in enumerate(payload["own_initial"]["claims"])],
                "counter_responses": [{"claim_id": c["id"], "response": "acknowledge", "reason": "Synthetic counterevidence matters.",
                    "evidence_refs": ref} for c in payload["opponent_initial"]["claims"]]}
            if self.omit_update:
                value["updates"][1]["claim_id"] = value["updates"][0]["claim_id"]
        elif name in ("ResearchEvaluation", "FinalAssessment"):
            assessments = [{"claim_id": c["id"], "disposition": "conditional", "reason": "Synthetic conditions remain."}
                           for c in payload["updated_claims"]]
            if name == "ResearchEvaluation":
                value = {"plan": {"recommendation": "Sell", "rationale": "PRIOR_RATING_MARKER",
                                  "strategic_actions": "PRIOR_STRATEGY_MARKER"}, "assessments": assessments,
                         "valuation_basis_and_gaps": "Synthetic valuation gap."}
            else:
                value = {"decision": {"rating": self.rating, "executive_summary": "Synthetic fresh opinion.",
                                      "investment_thesis": "Synthetic test does not demonstrate financial ability.",
                                      "price_target": None, "time_horizon": "12 months"}, "assessments": assessments,
                         "strongest_counterevidence": "Synthetic counterevidence.",
                         "why_it_changes_or_does_not_change_the_view": "Evidence narrows the thesis.",
                         "valuation_basis_and_gaps": "No validated valuation.", "next_observations": ["Next filing."]}
        elif name == "ExecutionReview":
            value = {"proposal": {"action": "Sell", "reasoning": "TRADER_ACTION_MARKER", "entry_price": None,
                                  "stop_loss": None, "position_sizing": None},
                     "feasibility_conditions": ["TRADER_THRESHOLD_MARKER: Confirm liquidity."], "missing_portfolio_inputs": ["Holdings unknown."]}
        elif name == "RiskBrief":
            value = {"analysis": node + "_RISK_MARKER", "evidence_refs": ref, "invalidation_conditions": ["New contradictory filing."]}
        else:
            if self.fail_review:
                raise RuntimeError("SYNTHETIC_REVIEW_FAILURE")
            value = {"findings": [{"issue": "REVIEW_OPPOSES_BUY", "evidence_refs": ref, "next_check": "Human check."}],
                     "coverage_and_limits": "Synthetic review is not a certificate."}
        if self.markdown_manager and name == "ResearchEvaluation":
            text = "## Synthetic manager report\n\n### 论点处置\n\n| 论点 | 处置 | 实质理由 |\n|---|---|---|\n"
            text += "\n".join(f"| **{a['claim_id']}** Synthetic claim | **{a['disposition']}** | {a['reason']} |" for a in value["assessments"])
            text += "\n\n### 评级\n\n**" + value["plan"]["recommendation"] + "（合成）**\n\n### 理由\n\n" + value["plan"]["rationale"]
            text += "\n\n### 关键失效条件（研究计划触发项）\n\n" + value["plan"]["strategic_actions"] + "\n\n### 估值依据与缺口\n\n" + value["valuation_basis_and_gaps"]
            return self._result(text)
        if self.split_revision and name == "RevisionBrief":
            return self._result("", tool_calls=[{"name": name, "id": "fragment-" + key,
                "args": {key: child}, "type": "tool_call"} for key, child in value.items()])
        return self._result(json.dumps(value))


class ThesisFlowTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_thesis(self, model=None, frozen=True, review=True, root=None, resume=None):
        model = model or ThesisLLM()
        command = ResearchThesis("AURORA", date(2026, 3, 2), "经营与价格是否支持投资？", sources=source_bundle() if frozen else None, review=review)
        application = FinResearchOps(artifact_root=root or self.root, researcher=ThesisResearcher(resume_from=resume))
        with patched_native_runtime(model) as data, patch.object(application._gate, "run", side_effect=AssertionError("CORE_NOT_A_PREREQUISITE")):
            view = application.handle(command)
        return view, model, data

    def test_actual_drafts_are_independent_revisions_symmetric_and_final_rating_fresh(self):
        view, model, data = self.run_thesis()
        record = view.latest_report
        self.assertEqual(expected_topology(), record["topology"])
        self.assertEqual(10, len(record["model_calls"]))
        self.assertEqual(11, len(model.requests))
        self.assertEqual([], data.calls)
        exchanges = record["exchanges"]
        self.assertNotIn("Bull Researcher_INITIAL", exchanges[1]["messages"][1]["content"])
        self.assertIn("Bull Researcher_INITIAL", exchanges[3]["messages"][1]["content"])
        self.assertNotIn("Bull Researcher_REVISED", exchanges[3]["messages"][1]["content"])
        final = exchanges[-1]["messages"][1]["content"]
        self.assertIn("Bull Researcher_REVISED", final)
        self.assertNotIn("Bull Researcher_INITIAL_2", final)
        for marker in ("PRIOR_RATING_MARKER", "PRIOR_STRATEGY_MARKER", "TRADER_ACTION_MARKER", "TRADER_THRESHOLD_MARKER"):
            self.assertNotIn(marker, final)
        risk = [r for r in model.requests if r["schema"] == "RiskBrief"]
        for request in risk:
            self.assertNotIn("_RISK_MARKER", request["text"])
        self.assertEqual("Buy", record["signal"])
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual("Buy", record["final_assessment"]["decision"]["rating"])
        self.assertEqual("REVIEW_OPPOSES_BUY", view.review["findings"][0]["issue"])
        for request in model.requests:
            self.assertIn("REVENUE_AND_EQUITY_PRESERVED", request["text"])
            self.assertEqual("json_mode", request["structured_method"])
            self.assertEqual("max" if request["schema"] in ("FinalAssessment", "DataReview") else None,
                             request["reasoning_effort"])
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))
        self.assertIn("COUNTEREVIDENCE_CHANGED_REASON", Path(view.report_path).read_text())

    def test_live_vendor_tool_path_keeps_full_returns(self):
        view, model, data = self.run_thesis(frozen=False)
        self.assertEqual(7, len(data.calls))
        self.assertEqual(7, len(view.latest_report["tool_calls"]))
        self.assertEqual(7, len(view.latest_report["source_bundle"]["sources"]))
        for row in view.latest_report["exchanges"]:
            self.assertIn('\\"assets\\": \\"150000\\"', row["messages"][1]["content"])
        self.assertEqual("LIVE_VENDOR", view.latest_report["request"]["data_mode"])
        for exchange in view.latest_report["exchanges"]:
            self.assertNotIn("Synthetic Fundamentals Analyst response", exchange["messages"][1]["content"])
            self.assertNotIn("Synthetic Market Analyst response", exchange["messages"][1]["content"])

    def test_review_disabled_or_failing_preserves_main_and_model_can_sell(self):
        for mode in ("disabled", "failed"):
            with self.subTest(mode=mode):
                root = self.root / mode
                view, _, _ = self.run_thesis(ThesisLLM(rating="Sell", fail_review=mode == "failed"), review=mode != "disabled", root=root)
                self.assertEqual("Sell", view.latest_report["signal"])
                self.assertEqual("DEFERRED" if mode == "disabled" else "PARTIAL", view.review["status"])
                self.assertEqual(view.case_ref[5:], sha256_hex(canonical_json_bytes(view.latest_report)))
                self.assertEqual(view, FinResearchOps(artifact_root=root).read_case(view.case_ref))

    def test_model_can_abstain_without_a_fixed_hold(self):
        view, _, _ = self.run_thesis(ThesisLLM(rating="REVIEW"))
        self.assertEqual("REVIEW", view.latest_report["signal"])
        self.assertTrue(view.latest_report["reports"]["final_trade_decision"].startswith("**Rating**: REVIEW"))

    def test_main_is_reopenable_before_review_and_corrupt_review_does_not_revoke_it(self):
        original = ThesisSession.review
        observed = []
        def review(session, record, config, saved_report):
            cases = list(self.root.glob("application/thesis-cases/*/case.json"))
            self.assertEqual(1, len(cases))
            reopened = FinResearchOps(artifact_root=self.root).read_case(cases[0].parent.name)
            self.assertEqual("PARTIAL", reopened.review["status"])
            observed.append(cases[0].read_bytes())
            self.assertEqual(Path(reopened.report_path).read_text(), saved_report)
            return original(session, record, config, saved_report)
        with patch.object(ThesisSession, "review", review):
            view, _, _ = self.run_thesis()
        case_file = Path(view.report_path).with_name("case.json")
        self.assertEqual(observed[0], case_file.read_bytes())
        for corrupted in ("corrupt synthetic optional file", "[]"):
            case_file.with_name("review.json").write_text(corrupted)
            reopened = FinResearchOps(artifact_root=self.root).read_case(view.case_ref)
            self.assertEqual(view.latest_report, reopened.latest_report)
            self.assertEqual("REVIEW_INTEGRITY_FAILED", reopened.review["reason"])

    def test_report_opinion_cannot_diverge_from_actual_model_result(self):
        view, _, _ = self.run_thesis()
        record = deepcopy(view.latest_report)
        record["final_assessment"]["decision"]["rating"] = "Sell"
        with self.assertRaisesRegex(ValueError, "THESIS_DELIVERED_ASSESSMENT_CHANGED"):
            validate(record)

    def test_standard_library_reopen_and_compact_cli(self):
        view, _, _ = self.run_thesis()
        repo = Path(__file__).parents[1]
        env = {**os.environ, "PYTHONPATH": str(repo / "src")}
        result = subprocess.run([str(repo / ".venv/bin/python"), "-m", "finauditgate.cli", "--artifact-root", str(self.root),
                                 "inspect-case", "--case-ref", view.case_ref], capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Buy", json.loads(result.stdout)["signal"])
        self.assertNotIn("source_bundle", result.stdout)
        code = "from pathlib import Path; import sys; from finauditgate.application import FinResearchOps; FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]); assert not any(n in sys.modules for n in ('langgraph','langchain_core','tradingagents','pydantic'))"
        result = subprocess.run([str(repo / ".venv/bin/python"), "-c", code, str(self.root), view.case_ref], env=env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_disjoint_provider_fragments_and_exact_completed_prefix_recovery(self):
        interrupted = self.root / "interrupted"
        with self.assertRaises(ApplicationError):
            self.run_thesis(ThesisLLM(split_revision=True, fail_second_revision=True), root=interrupted)
        execution = next(interrupted.glob("application/thesis-executions/*"))
        prior = json.loads((execution / "runtime-receipt.json").read_text())
        self.assertEqual(4, len(prior["model_calls"]))
        view, model, _ = self.run_thesis(ThesisLLM(split_revision=True), root=self.root / "recovered", resume=execution)
        self.assertEqual(3, view.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(8, len(model.requests))
        self.assertEqual(prior["model_calls"][:3], view.latest_report["model_calls"][:3])
        self.assertEqual("Buy", view.latest_report["signal"])
        request = json.loads((execution / "request.json").read_text())
        request["request"]["question"] = "Changed question must not reuse old answers"
        (execution / "request.json").write_text(json.dumps(request))
        changed_model = ThesisLLM()
        with self.assertRaises(ApplicationError) as ctx:
            self.run_thesis(changed_model, root=self.root / "rejected", resume=execution)
        self.assertIn("THESIS_RESUME_INPUT_MISMATCH", str(ctx.exception.__cause__))
        self.assertEqual([], changed_model.requests)

    def test_conflicting_response_fragments_are_not_merged(self):
        outputs = [{"tool_calls": [{"name": "RevisionBrief", "args": {"updates": ["one"]}},
                                    {"name": "RevisionBrief", "args": {"updates": ["different"]}}]}]
        with self.assertRaisesRegex(ValueError, "THESIS_RESPONSE_FRAGMENTS_CONFLICT"):
            response_candidate(outputs, "RevisionBrief")

    def test_observed_manager_markdown_is_read_verbatim_without_default_rating(self):
        view, model, _ = self.run_thesis(ThesisLLM(markdown_manager=True))
        plan = view.latest_report["research_evaluation"]["plan"]
        self.assertEqual("Sell", plan["recommendation"])
        self.assertEqual("PRIOR_RATING_MARKER", plan["rationale"])
        self.assertEqual("Buy", view.latest_report["signal"])
        raw = next(c for c in view.latest_report["model_calls"] if c["node"] == "Research Manager")["output"][0]["content"]
        with self.assertRaisesRegex(ValueError, "THESIS_RESEARCH_MARKDOWN_RATING_INVALID"):
            response_candidate([{"content": raw.replace("**Sell（合成）**", "未给评级")}], "ResearchEvaluation")
        with self.assertRaisesRegex(ValueError, "THESIS_RESEARCH_MARKDOWN_LAYOUT_UNSUPPORTED"):
            response_candidate([{"content": raw.replace("### 理由", "### 未知新章节")}], "ResearchEvaluation")

    def test_incomplete_final_is_recomputed_and_full_report_reaches_review(self):
        view, _, _ = self.run_thesis(root=self.root / "original")
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        receipt = json.loads((execution / "runtime-receipt.json").read_text())
        final = receipt["model_calls"][9]["output"][0]
        value = json.loads(final["content"])
        final["content"] = json.dumps({"decision": value["decision"]})
        (execution / "runtime-receipt.json").write_text(json.dumps(receipt))
        recovered, model, _ = self.run_thesis(root=self.root / "recovered", resume=execution)
        self.assertEqual(9, recovered.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(["Portfolio Manager", "Data Review Agent"], [r["node"] for r in model.requests])
        review_input = model.requests[-1]["payload"]["saved_report"]
        self.assertEqual(Path(recovered.report_path).read_text(), review_input)
        self.assertIn("Bull Researcher_INITIAL_2", review_input)

    def test_json_mode_reaches_the_real_sdk_wire_without_unsupported_tool_choice(self):
        import httpx
        from tradingagents.llm_clients.openai_client import DeepSeekChatOpenAI
        seen = []
        value = {"analysis": "Synthetic risk only.", "evidence_refs": ["S01"], "invalidation_conditions": ["Synthetic observation."]}
        def respond(request):
            payload = json.loads(request.content)
            seen.append(payload)
            return httpx.Response(200, json={"id": "synthetic-json-wire", "object": "chat.completion",
                "created": 0, "model": "deepseek-v4-pro", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": json.dumps(value)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})
        with model_http_client("deepseek", 65536, transport=httpx.MockTransport(respond), reasoning_effort="high") as client:
            model = DeepSeekChatOpenAI(model="deepseek-v4-pro", api_key="synthetic-no-account",
                base_url="https://api.deepseek.com", http_client=client, max_retries=0, max_tokens=65536)
            result = model.model_copy(update={"reasoning_effort": "max"}).with_structured_output(
                schemas()["RiskBrief"], method="json_mode", include_raw=True).invoke("Return the synthetic JSON object.")
        self.assertEqual(value, result["parsed"].model_dump(mode="json"))
        self.assertEqual({"type": "json_object"}, seen[0]["response_format"])
        self.assertEqual("max", seen[0]["reasoning_effort"])
        self.assertNotIn("tools", seen[0])
        self.assertNotIn("tool_choice", seen[0])

    def test_missing_claim_update_stops_instead_of_fabricating_coverage(self):
        with self.assertRaises(ApplicationError) as ctx:
            self.run_thesis(ThesisLLM(omit_update=True))
        self.assertIn("THESIS_CLAIM_COVERAGE_INVALID", str(ctx.exception.__cause__))
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))
        failure = json.loads(next(self.root.glob("application/thesis-executions/*/failure.json")).read_text())
        self.assertEqual("THESIS_CLAIM_COVERAGE_INVALID", failure["error_code"])
        self.assertEqual(["ValueError"], failure["cause_types"])

    def test_changed_model_input_or_output_cannot_be_reopened_as_verified_receipt(self):
        view, _, _ = self.run_thesis()
        for mutation in ("input", "output"):
            with self.subTest(mutation=mutation):
                record = deepcopy(view.latest_report)
                if mutation == "input":
                    record["exchanges"][-1]["messages"][1]["content"] += "Injected prior rating"
                else:
                    record["exchanges"][-1]["parsed"]["decision"]["rating"] = "Sell"
                with self.assertRaises(ValueError):
                    validate(record)


if __name__ == "__main__":
    unittest.main()
