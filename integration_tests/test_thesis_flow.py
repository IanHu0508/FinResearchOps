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


def synthetic_forward_draft(*, no_multiple=False, empty=False):
    def assumption(value, basis="analyst_assumption"):
        return {"value": value, "basis_type": basis, "reason": "Synthetic input basis; not a financial forecast.", "evidence_refs": ["S01"]}
    scenarios = []
    for i, margin in enumerate((0.2, 0.25), 1):
        values = {"revenue": 100, "operating_margin": margin, "net_nonoperating_income": 0,
            "effective_tax_rate": 0.25, "diluted_ordinary_shares": 10,
            "non_working_capital_adjustments": 3, "operating_asset_liability_cash_effect": -2, "cash_capex": 5,
            "fx_reporting_per_price_currency": 1, "exit_pe": None if no_multiple else 10,
            "cash_dividend_per_traded_unit": None}
        scenarios.append({"scenario_id": f"F{i}", "name": f"经营路径{i}", "drivers": "SYNTHETIC_FORWARD_DRIVER",
            **{key: assumption(value) for key, value in values.items()},
            "noncontrolling_attribution": {"nature": "profit", "amount": assumption(0)},
            "valuation_reasoning": "Conditional earnings transmission, not a fair multiple.",
            "evidence_that_changes_case": "Next operating margin observation."})
    return {"business_model": "Synthetic operating company", "reporting_currency": "USD", "price_currency": "USD",
        "amount_unit": "million", "earnings_basis": "trailing_at_valuation", "forecast_start": "2026-03-03", "forecast_end": "2027-03-02",
        "valuation_date": "2027-03-02", "market_price_date": "2026-03-02",
        "market_price": assumption(10, "reported"), "shares_per_traded_unit": assumption(1, "reported"),
        "scenarios": [] if empty else scenarios, "limitations": ["Synthetic assumptions do not establish real value."]}


class ThesisLLM(NativeSyntheticLLM):
    reasoning_effort: str | None = None
    rating: str = "Buy"
    independent_rating: str = "Hold"
    no_multiple: bool = False
    empty_forecast: bool = False
    omit_forward_assessment: bool = False
    omit_belief_update: bool = False
    leak_sensitivity_ref: bool = False
    omit_aggregate_finance_refs: bool = False
    fail_review: bool = False
    omit_update: bool = False
    split_revision: bool = False
    fail_second_revision: bool = False
    markdown_manager: bool = False
    nested_manager_valuation: bool = False

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
                "evidence_refs": ref, "would_change_mind": "RESEARCHER_ONLY_TRIGGER"}
                for i, c in enumerate(payload["own_initial"]["claims"])],
                "counter_responses": [{"claim_id": c["id"], "response": "acknowledge", "reason": "Synthetic counterevidence matters.",
                    "evidence_refs": ref} for c in payload["opponent_initial"]["claims"]]}
            if self.omit_update:
                value["updates"][1]["claim_id"] = value["updates"][0]["claim_id"]
        elif name == "IndependentAssessment":
            value = {"decision": {"rating": self.independent_rating, "executive_summary": "INDEPENDENT_RATIONALE"},
                "beliefs": [{"belief_id": f"D{i}", "statement": f"INDEPENDENT_BELIEF_{i}", "business_mechanism": "Financial mechanism.",
                    "evidence_refs": ["S02"] if self.leak_sensitivity_ref else ref, "would_change_mind": "INDEPENDENT_ONLY_TRIGGER", "uncertainty": "Unknown persistence."} for i in (1, 2)]}
        elif name == "UnderwritingDraft":
            value = synthetic_forward_draft(no_multiple=self.no_multiple, empty=self.empty_forecast)
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
                         "valuation_basis_and_gaps": "No validated valuation.", "next_observations": ["Next filing."],
                         "financial_analysis": {"operating_performance": "SYNTHETIC_OPERATING_BRIDGE",
                             "earnings_quality": "SYNTHETIC_ATTRIBUTION", "cash_and_capital_allocation": "SYNTHETIC_CASH_ALLOCATION",
                             "valuation_and_price_requirements": "SYNTHETIC_CONDITIONAL_VALUATION", "evidence_refs": ref},
                         "forward_assessment": {"economic_conclusion": "SYNTHETIC_COMPUTED_FORWARD_ANALYSIS",
                             "confidence_and_limits": "A calculator does not certify forecasts or ratings.",
                             "scenario_assessments": [{"scenario_id": s["scenario_id"], "disposition": "conditional",
                                 "reason": "Margin change produces EPS " + str(s["eps_per_traded_unit"]),
                                 "what_changes_the_view": "Subsequent margin evidence."} for s in payload["forward_calculations"]["scenario_results"]]},
                         "decision_update": {"basis_for_rating": "Synthetic evidence changes earnings; this is not calibration proof.",
                             "adopted_assumptions": [],
                             "belief_updates": [{"belief_id": b["belief_id"], "status": "revise" if i == 0 else "maintain",
                                "new_statement": "Revised financial belief." if i == 0 else None,
                                "update_basis": "source_observation" if i == 0 else "no_new_basis",
                                "reason": "COUNTEREVIDENCE_TO_INDEPENDENT_BELIEF", "financial_implication": "Changed earnings expectation.",
                                "evidence_refs": ref} for i, b in enumerate(payload["independent_beliefs"])]}}
                if self.omit_belief_update:
                    value["decision_update"]["belief_updates"][1]["belief_id"] = "D1"
                if self.omit_aggregate_finance_refs:
                    del value["financial_analysis"]["evidence_refs"]
                if self.omit_forward_assessment:
                    value["forward_assessment"]["scenario_assessments"] = []
        elif name == "ExecutionReview":
            value = {"proposal": {"action": "Sell", "reasoning": "TRADER_ACTION_MARKER", "entry_price": None,
                                  "stop_loss": None, "position_sizing": None},
                     "feasibility_conditions": ["TRADER_THRESHOLD_MARKER: Confirm liquidity."], "missing_portfolio_inputs": ["Holdings unknown."]}
        elif name == "RiskBrief":
            value = {"analysis": node + "_RISK_MARKER", "evidence_refs": ref, "invalidation_conditions": ["RISK_ONLY_TRIGGER"]}
        else:
            if self.fail_review:
                raise RuntimeError("SYNTHETIC_REVIEW_FAILURE")
            value = {"findings": [{"issue": "REVIEW_OPPOSES_BUY", "evidence_refs": ref, "next_check": "Human check."}],
                     "coverage_and_limits": "Synthetic review is not a certificate."}
        if self.nested_manager_valuation and name == "ResearchEvaluation":
            value["plan"]["valuation_basis_and_gaps"] = value.pop("valuation_basis_and_gaps")
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

    def run_thesis(self, model=None, frozen=True, review=True, root=None, resume=None, reassess=False, sources=None, **context):
        model = model or ThesisLLM()
        command = ResearchThesis("AURORA", date(2026, 3, 2), "经营与价格是否支持投资？", sources=(sources or source_bundle()) if frozen else None, review=review, **context)
        application = FinResearchOps(artifact_root=root or self.root, researcher=ThesisResearcher(resume_from=resume, reassess_final=reassess, protocol_version=10))
        with patched_native_runtime(model) as data, patch.object(application._gate, "run", side_effect=AssertionError("CORE_NOT_A_PREREQUISITE")):
            view = application.handle(command)
        return view, model, data

    def test_user_view_is_report_only_while_hypotheses_and_constraints_reach_research(self):
        results = []
        for label in ("DESIRED_BUY_WITHOUT_NEW_FACTS", "DESIRED_SELL_WITHOUT_NEW_FACTS"):
            view, model, _ = self.run_thesis(root=self.root / label, user_view=label,
                hypotheses=("NEW_PRODUCT_REVENUE_HYPOTHESIS",), research_constraints=("LIQUIDITY_RESEARCH_CONSTRAINT",))
            for request in model.requests[:12]:
                self.assertNotIn(label, request["text"])
                self.assertNotIn("user_view", request["payload"]["request"])
                self.assertIn("NEW_PRODUCT_REVENUE_HYPOTHESIS", request["text"])
                self.assertIn("LIQUIDITY_RESEARCH_CONSTRAINT", request["text"])
            self.assertIn(label, Path(view.report_path).read_text())
            self.assertIn(label, model.requests[-1]["payload"]["saved_report"])
            self.assertEqual(label, view.latest_report["request"]["user_view"])
            results.append([request["text"] for request in model.requests[:12]])
        self.assertEqual(results[0], results[1])

    def test_record_cannot_claim_different_substantive_context_than_actual_requests(self):
        view, _, _ = self.run_thesis(hypotheses=("ORIGINAL_HYPOTHESIS",))
        changed = deepcopy(view.latest_report)
        changed["request"]["hypotheses"] = ["UNDELIVERED_HYPOTHESIS"]
        with self.assertRaisesRegex(ValueError, "THESIS_RESEARCH_REQUEST_BINDING_INVALID"):
            validate(changed)

    def test_context_validation_does_not_silently_drop_or_rewrite_items(self):
        for context in ({"hypotheses": ("",)}, {"research_constraints": ("x",) * 6},
                        {"user_view": " "}, {"hypotheses": ["mutable-list"]}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                ResearchThesis("AURORA", date(2026, 3, 2), "Question", **context)

    def test_observed_manager_field_location_is_recovered_without_rewriting_its_value(self):
        view, _, _ = self.run_thesis(ThesisLLM(nested_manager_valuation=True))
        record = view.latest_report
        raw = record["model_calls"][4]["output"][0]["content"]
        self.assertEqual(json.loads(raw)["plan"]["valuation_basis_and_gaps"],
                         record["research_evaluation"]["valuation_basis_and_gaps"])
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))
        from finauditgate.adapters.thesis_responses import response_candidate
        conflicting = json.loads(raw)
        conflicting["valuation_basis_and_gaps"] = "A competing version must not be selected."
        with self.assertRaisesRegex(ValueError, "THESIS_RESEARCH_FIELD_LOCATION_CONFLICT"):
            response_candidate([{"content": json.dumps(conflicting)}], "ResearchEvaluation")

    def test_upstream_text_extraction_cannot_change_the_structured_final_rating(self):
        with patch("tradingagents.graph.trading_graph.TradingAgentsGraph.process_signal", return_value="Hold"):
            view, _, _ = self.run_thesis(ThesisLLM(rating="REVIEW"))
        self.assertEqual("REVIEW", view.latest_report["signal"])
        receipt = next(self.root.glob("application/thesis-executions/*/native-signal.json"))
        self.assertEqual({"upstream_text_extraction": "Hold", "structured_final_rating": "REVIEW",
                          "selection": "STRUCTURED_FINAL_DECISION"}, json.loads(receipt.read_text()))
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))

    def test_actual_drafts_are_independent_revisions_symmetric_and_final_rating_fresh(self):
        view, model, data = self.run_thesis()
        record = view.latest_report
        self.assertEqual(expected_topology(), record["topology"])
        self.assertEqual(12, len(record["model_calls"]))
        self.assertEqual(13, len(model.requests))
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
            self.assertEqual("max" if request["schema"] in ("IndependentAssessment", "UnderwritingDraft", "FinalAssessment", "DataReview") else None,
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

    def test_declared_sensitivity_changes_cannot_reach_independent_underwriting(self):
        results = []
        for name, anchor in (("optimistic", "UNSUPPORTED_MULTIPLE_60"), ("pessimistic", "UNSUPPORTED_MULTIPLE_3")):
            bundle = source_bundle()
            bundle["schema_version"] = "finresearchops.thesis-sources/v2"
            bundle["sources"][0]["use"] = "research"
            bundle["sources"].append({"id": "S02", "origin": "SYNTHETIC_AUTHORED_SCENARIO", "availability_note": "Not observed evidence",
                                      "use": "sensitivity", "content": anchor, "sha256": sha256_hex(anchor.encode())})
            view, model, _ = self.run_thesis(root=self.root / name, sources=bundle)
            self.assertEqual(2, len(view.latest_report["source_bundle"]["sources"]))
            for request in model.requests[:12]:
                self.assertNotIn(anchor, request["text"])
            independent = model.requests[9]
            self.assertEqual("IndependentAssessment", independent["schema"])
            for marker in ("_INITIAL_", "_REVISED", "_RISK_MARKER", "PRIOR_RATING_MARKER", "TRADER_THRESHOLD_MARKER"):
                self.assertNotIn(marker, independent["text"])
            self.assertNotIn(anchor, model.requests[11]["text"])
            self.assertIn(anchor, model.requests[-1]["payload"]["saved_report"])
            self.assertIn("INDEPENDENT_BELIEF_1", model.requests[11]["text"])
            self.assertIn("COUNTEREVIDENCE_TO_INDEPENDENT_BELIEF", Path(view.report_path).read_text())
            self.assertIn("过程附录：研究经理计划", Path(view.report_path).read_text())
            results.append(model.requests)
        self.assertEqual([r["text"] for r in results[0][:12]], [r["text"] for r in results[1][:12]])
        self.assertEqual(results[0][11]["text"], results[1][11]["text"])

    def test_unseen_sensitivity_reference_and_false_update_receipts_are_rejected(self):
        for label, model, code in (
                ("unseen", ThesisLLM(leak_sensitivity_ref=True), "THESIS_UNKNOWN_SOURCE_REFERENCE"),
                ("missing", ThesisLLM(omit_belief_update=True), "THESIS_DECISION_UPDATE_INVALID")):
            with self.subTest(label=label), self.assertRaises(ApplicationError) as ctx:
                self.run_thesis(model, root=self.root / label)
            self.assertIn(code, str(ctx.exception.__cause__))
            self.assertFalse(list((self.root / label).glob("application/thesis-cases/*/case.json")))

    def test_initial_rating_and_trigger_fields_are_not_final_judgment_inputs(self):
        results = []
        for before in ("Buy", "Sell"):
            view, model, _ = self.run_thesis(ThesisLLM(independent_rating=before), root=self.root / before)
            final = model.requests[11]
            self.assertNotIn("independent_assessment", final["payload"])
            self.assertIn("independent_beliefs", final["payload"])
            for marker in ("INDEPENDENT_RATIONALE", "INDEPENDENT_ONLY_TRIGGER", "RESEARCHER_ONLY_TRIGGER", "RISK_ONLY_TRIGGER"):
                self.assertNotIn(marker, final["text"])
            self.assertEqual({"before": before, "after": "Buy", "changed": before != "Buy"}, view.latest_report["rating_comparison"])
            changed = deepcopy(view.latest_report)
            changed["rating_comparison"]["changed"] = not changed["rating_comparison"]["changed"]
            with self.assertRaisesRegex(ValueError, "THESIS_RATING_COMPARISON_INVALID"):
                validate(changed)
            results.append(final["text"])
        self.assertEqual(results[0], results[1])

    def test_financial_analysis_is_delivered_verbatim_and_reviewed(self):
        view, model, _ = self.run_thesis()
        self.assertEqual("finresearchops.thesis-case/v10", view.latest_report["schema_version"])
        finance = view.latest_report["final_assessment"]["financial_analysis"]
        report = Path(view.report_path).read_text()
        for field, value in finance.items():
            if field != "evidence_refs":
                self.assertIn(value, report)
                self.assertIn(value, model.requests[-1]["payload"]["saved_report"])
        changed = deepcopy(view.latest_report)
        changed["final_assessment"]["financial_analysis"]["operating_performance"] = "Unreported financial claim"
        with self.assertRaisesRegex(ValueError, "THESIS_DELIVERED_ASSESSMENT_CHANGED"):
            validate(changed)

    def test_forward_math_reaches_final_before_rating_and_is_saved_verbatim(self):
        view, model, _ = self.run_thesis()
        record = view.latest_report
        forecast, final = model.requests[10], model.requests[11]
        self.assertEqual("UnderwritingDraft", forecast["schema"])
        self.assertNotIn("12/15/18", forecast["text"])
        self.assertEqual({"node", "request", "source_bundle", "updated_claims", "risk_briefs"}, set(forecast["payload"]))
        self.assertNotIn("INDEPENDENT_BELIEF_1", forecast["text"])
        self.assertEqual(record["forward_draft"], final["payload"]["forward_draft"])
        self.assertEqual(record["forward_calculations"], final["payload"]["forward_calculations"])
        rows = final["payload"]["forward_calculations"]["scenario_results"]
        self.assertEqual([1.5, 1.875], [r["eps_per_traded_unit"] for r in rows])
        self.assertEqual([11, 14.75], [r["cash_after_capex_proxy"] for r in rows])
        self.assertEqual([0.5, 0.875], [r["return_ex_dividend"] for r in rows])
        self.assertAlmostEqual(10 / 1.5, rows[0]["price_only_break_even_pe"])
        self.assertEqual([None, None], [r["return_with_dividend"] for r in rows])
        report = Path(view.report_path).read_text()
        self.assertIn("SYNTHETIC_COMPUTED_FORWARD_ANALYSIS", report)
        self.assertIn("SYNTHETIC_FORWARD_DRIVER", report)
        self.assertEqual(report, model.requests[-1]["payload"]["saved_report"])
        changed = deepcopy(record)
        changed["forward_calculations"]["scenario_results"][0]["eps_per_traded_unit"] = 99
        with self.assertRaisesRegex(ValueError, "THESIS_FORWARD_CALCULATIONS_NOT_DELIVERED"):
            validate(changed)

    def test_missing_multiple_keeps_forecasts_and_report_without_fabricated_price(self):
        view, model, _ = self.run_thesis(ThesisLLM(no_multiple=True, rating="REVIEW"))
        rows = view.latest_report["forward_calculations"]["scenario_results"]
        self.assertEqual([1.5, 1.875], [r["eps_per_traded_unit"] for r in rows])
        self.assertTrue(all(r["exit_price_per_traded_unit"] is None for r in rows))
        self.assertTrue(all(r["return_ex_dividend"] is None for r in rows))
        self.assertAlmostEqual(10 / 1.5, rows[0]["price_only_break_even_pe"])
        self.assertAlmostEqual(10 / 1.875, rows[1]["price_only_break_even_pe"])
        self.assertIn("维持起点股价所需PE", Path(view.report_path).read_text().replace(" ", ""))
        self.assertIn("SYNTHETIC_COMPUTED_FORWARD_ANALYSIS", Path(view.report_path).read_text())
        self.assertEqual("REVIEW", view.latest_report["signal"])
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))

    def test_unsuitable_business_can_deliver_qualitative_research_without_forced_pe(self):
        view, _, _ = self.run_thesis(ThesisLLM(empty_forecast=True, rating="REVIEW"))
        self.assertEqual([], view.latest_report["forward_calculations"]["scenario_results"])
        self.assertIn("当前没有可列示的量化情景", Path(view.report_path).read_text())
        self.assertIn("SYNTHETIC_COMPUTED_FORWARD_ANALYSIS", Path(view.report_path).read_text())

    def test_all_forecast_paths_require_an_explicit_final_assessment(self):
        with self.assertRaises(ApplicationError) as ctx:
            self.run_thesis(ThesisLLM(omit_forward_assessment=True))
        self.assertIn("THESIS_FORWARD_ASSESSMENT_COVERAGE_INVALID", str(ctx.exception.__cause__))
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))

    def test_forecast_valuation_horizon_and_market_cutoff_are_bound_before_final(self):
        for field, value, error in (("valuation_date", "2026-03-02", "THESIS_FORWARD_HORIZON_MISMATCH"),
                                     ("market_price_date", "2026-03-03", "THESIS_FORWARD_FUTURE_MARKET_PRICE")):
            with self.subTest(field=field):
                draft = synthetic_forward_draft()
                draft[field] = value
                model = ThesisLLM()
                with patch(__name__ + ".synthetic_forward_draft", return_value=draft):
                    with self.assertRaises(ApplicationError) as ctx:
                        self.run_thesis(model, root=self.root / field)
                self.assertIn(error, str(ctx.exception.__cause__))
                self.assertNotIn("FinalAssessment", [r["schema"] for r in model.requests])

    def test_attribution_nature_reaches_final_with_correct_effect_and_unchanged_cash(self):
        for nature, effect, eps in (("profit", -2, 1.3), ("loss", 2, 1.7)):
            with self.subTest(nature=nature):
                draft = synthetic_forward_draft()
                for s in draft["scenarios"]:
                    s["noncontrolling_attribution"]["nature"] = nature
                    s["noncontrolling_attribution"]["amount"]["value"] = 2
                with patch(__name__ + ".synthetic_forward_draft", return_value=draft):
                    view, model, _ = self.run_thesis(root=self.root / nature)
                result = model.requests[11]["payload"]["forward_calculations"]["scenario_results"][0]
                self.assertEqual(effect, result["noncontrolling_attribution_effect"])
                self.assertEqual(eps, result["eps_per_traded_unit"])
                self.assertEqual(11, result["cash_after_capex_proxy"])
                self.assertEqual(view, FinResearchOps(artifact_root=self.root / nature).read_case(view.case_ref))

    def test_negative_attribution_amount_is_not_silently_reinterpreted_as_a_loss(self):
        draft = synthetic_forward_draft()
        draft["scenarios"][0]["noncontrolling_attribution"]["amount"]["value"] = -2
        model = ThesisLLM()
        with patch(__name__ + ".synthetic_forward_draft", return_value=draft):
            with self.assertRaises(ApplicationError):
                self.run_thesis(model)
        self.assertNotIn("FinalAssessment", [r["schema"] for r in model.requests])

    def test_financial_sections_cannot_be_missing_or_use_unknown_sources(self):
        view, _, _ = self.run_thesis()
        for alteration in ("missing", "unknown_ref"):
            changed = deepcopy(view.latest_report)
            finance = changed["final_assessment"]["financial_analysis"]
            if alteration == "missing":
                del finance["earnings_quality"]
            else:
                finance["evidence_refs"] = ["NONEXISTENT_SOURCE"]
            with self.assertRaisesRegex(ValueError, "THESIS_FINANCIAL_ANALYSIS_REQUIRED"):
                validate(changed)

    def test_optional_chapter_reference_summary_is_not_fabricated(self):
        view, _, _ = self.run_thesis(ThesisLLM(omit_aggregate_finance_refs=True))
        self.assertIsNone(view.latest_report["final_assessment"]["financial_analysis"]["evidence_refs"])
        self.assertIn("未自动补写引用", Path(view.report_path).read_text())
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))

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
        self.assertEqual(10, len(model.requests))
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
        final = receipt["model_calls"][11]["output"][0]
        value = json.loads(final["content"])
        final["content"] = json.dumps({"decision": value["decision"]})
        (execution / "runtime-receipt.json").write_text(json.dumps(receipt))
        recovered, model, _ = self.run_thesis(root=self.root / "recovered", resume=execution)
        self.assertEqual(11, recovered.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(["Portfolio Manager", "Data Review Agent"], [r["node"] for r in model.requests])
        review_input = model.requests[-1]["payload"]["saved_report"]
        self.assertEqual(Path(recovered.report_path).read_text(), review_input)
        self.assertIn("Bull Researcher_INITIAL_2", review_input)

    def test_explicit_reassessment_keeps_the_exact_prefix_through_independent_underwriting(self):
        original, _, _ = self.run_thesis(root=self.root / "original")
        original_bytes = Path(original.report_path).read_bytes()
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        revised, model, _ = self.run_thesis(ThesisLLM(rating="Sell"), root=self.root / "reassessed",
                                          resume=execution, reassess=True)
        self.assertEqual(["FinalAssessment", "DataReview"], [r["schema"] for r in model.requests])
        self.assertEqual(11, revised.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(original.latest_report["model_calls"][:11], revised.latest_report["model_calls"][:11])
        self.assertEqual(original.latest_report["forward_draft"], revised.latest_report["forward_draft"])
        self.assertEqual(original.latest_report["forward_calculations"], revised.latest_report["forward_calculations"])
        self.assertEqual("Buy", original.latest_report["signal"])
        self.assertEqual("Sell", revised.latest_report["signal"])
        self.assertEqual(original_bytes, Path(original.report_path).read_bytes())
        self.assertNotEqual(original.case_ref, revised.case_ref)
        with self.assertRaises(ApplicationError) as ctx:
            self.run_thesis(root=self.root / "invalid", reassess=True)
        self.assertIn("THESIS_REASSESS_REQUIRES_RESUME", str(ctx.exception.__cause__))

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
