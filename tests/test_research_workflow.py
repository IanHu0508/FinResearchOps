from dataclasses import replace
from datetime import date
import json
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from finauditgate import FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.research_contract import clone, model_evidence_view, required_review_references, normalize_schema_title, DECISION_SCHEMA, validate_shape
from finauditgate.application import ApplicationError, FinResearchOps, ReplayRun
from finauditgate.cashflow import CashflowTask
from finauditgate.research import FundamentalEvidenceTask, ResearchSecurity


FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic/cashflow_investigation.html"


def proposals(evidence):
    first = evidence["analysis"]["facts"][0]["fact_id"]
    contrary = evidence["analysis"]["facts"][2]["fact_id"]
    driver = evidence["analysis"]["drivers"][0]["driver_id"]
    claim = {"statement": "合成样例：需结合现金转化检查利润变化。", "evidence_ids": [first],
             "assumption": "现金转化的持续性尚未得到证明。", "revisit_if": "取得明确的后续回款证据。"}
    analysis = {"summary": "合成的经营分析。", "claims": [claim], "requested_drivers": [driver]}
    challenge = {"summary": "合成的独立质疑。", "claims": [{**claim, "evidence_ids": [contrary]}], "requested_drivers": []}
    decision = {"conclusion": "合成样例的条件性研究结论，不代表模型能力。", "outlook": "混合",
        "claims": [claim], "counterevidence_response": [{"evidence_id": contrary, "treatment": "待核实",
        "reason": "需要检查变化是否持续。"}], "scenarios": [
            {"name": name, "condition": "合成的条件假设。", "evidence_to_check": "需要核对后续披露。"}
            for name in ("维持判断", "支持增强", "支持减弱")], "limitations": ["这是脚本化合成样例。"], "next_steps": ["核实后续披露。"]}
    for step in evidence["steps"]:
        if step["hits"] and step["hits"][0]["note_id"] not in {x["evidence_id"] for x in decision["counterevidence_response"]}:
            decision["counterevidence_response"].append({"evidence_id": step["hits"][0]["note_id"],
                "treatment": "待核实", "reason": "合成示例：新取得的披露仍需区分当期原因与一般政策。"})
    return analysis, challenge, decision


class ScriptedResearcher:
    def __init__(self, mutation=None):
        self.mutation = mutation
        self.invocations = 0

    def run(self, request, evidence, lookup):
        self.invocations += 1
        initial = clone(evidence)
        analysis, challenge, decision = proposals(initial)
        final = lookup(tuple(analysis["requested_drivers"]))
        decision = proposals(final)[2]
        calls = [{"stage": stage, "request": {"request": clone(request), "evidence": model_evidence_view(initial)},
                  "proposal": clone(proposal)} for stage, proposal in (("analysis", analysis), ("challenge", challenge))]
        calls.append({"stage": "synthesis", "request": {"request": clone(request), "evidence": model_evidence_view(final),
            "required_evidence_ids": required_review_references(challenge, final)}, "proposal": clone(decision)})
        result = {"analysis": analysis, "challenge": challenge, "decision": decision, "calls": calls,
                  "budget": {"calls": 3}, "runtime_kind": "SCRIPTED_INTEGRATION",
                  "upstream_commit": "2448d0a12576f9b2ddcd5980a0630833423d1e1b"}
        if self.mutation:
            self.mutation(result)
        return result

    def explain_update(self, payload):
        proposal = update_proposal(payload)
        return {"proposal": proposal, "call": {"stage": "update", "request": clone(payload), "proposal": clone(proposal)},
                "budget": {"calls": 4}}


def update_proposal(payload):
    return {"summary": "合成对照：相同标签仍可对应不同证据。", "items": [
        {"prior_claim_id": old["claim_id"], "current_claim_ids": [payload["current_claims"][0]["claim_id"]],
         "assessment": "待核实", "reason": "合成示例：现有证据尚不足以证明原假设持续成立。",
         "evidence_ids": [payload["evidence"]["analysis"]["facts"][0]["fact_id"]], "next_check": "核实与该假设相关的后续资料。"}
        for old in payload["prior_claims"]]}


class ResearchWorkflowTest(unittest.TestCase):
    def test_schema_title_echo_is_narrowly_normalized(self):
        raw = {"title": "ResearchDecision", "unexpected_financial_field": "keep for rejection"}
        cleaned, changes = normalize_schema_title(raw, DECISION_SCHEMA)
        self.assertNotIn("title", cleaned)
        self.assertIn("unexpected_financial_field", cleaned)
        self.assertIn("title", raw)
        self.assertEqual(["REMOVED_EXACT_SCHEMA_TITLE_ECHO"], changes)
        with self.assertRaisesRegex(ValueError, "SHAPE_INVALID"):
            validate_shape(cleaned, DECISION_SCHEMA)
        other = {"title": "Financial opinion"}
        self.assertEqual((other, []), normalize_schema_title(other, DECISION_SCHEMA))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        (self.workspace / "finaudit-gate/.git").mkdir(parents=True)
        (self.workspace / "private").mkdir()
        self.root = self.workspace / "private/runs"
        self.task = CashflowTask("synthetic-research", FrozenDocumentPackage(
            "synthetic-aurora", "synthetic.html", FIXTURE.read_bytes(), date(2026, 3, 1)),
            "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000001/synthetic.htm",
            "0001111111-26-000001", "1111111", date(2025, 12, 31), date(2024, 12, 31), date(2026, 3, 2), "USD")
        self.command = ResearchSecurity(self.task, "AURORA", "盈利变化是否有持续现金转化支持？")

    def app(self, researcher=None):
        return FinResearchOps(artifact_root=self.root, researcher=researcher)

    def test_initial_evidence_has_no_requested_lookup_and_replays(self):
        gate = FinAuditGate(artifact_root=self.root)
        outcome = gate.run(FundamentalEvidenceTask(self.task))
        self.assertEqual([], outcome.report["steps"])
        self.assertTrue(outcome.report["analysis"]["metrics"])
        self.assertTrue(gate.replay(outcome.run_ref).consistent)

    def test_research_lookup_is_explicit_and_replayable(self):
        gate = FinAuditGate(artifact_root=self.root)
        first = gate.run(FundamentalEvidenceTask(self.task))
        ids = tuple(d["driver_id"] for d in first.report["analysis"]["drivers"][:2])
        outcome = gate.run(FundamentalEvidenceTask(self.task, ids))
        self.assertEqual(2, len(outcome.report["steps"]))
        self.assertTrue(gate.replay(outcome.run_ref).consistent)
        self.assertEqual("HUMAN_REVIEW", outcome.decision.value)

    def test_research_preserves_concept_sign_and_annual_measurement(self):
        # A positive cash-flow addback can carry a negative signed XBRL result.
        data = self.task.document.document_bytes.replace(
            b'name="us-gaap:Depreciation"',
            b'name="example:EquityMethodResults" sign="-"').replace(
            b'<td>Depreciation</td>', b'<td>Share of equity method results</td>')
        task = replace(self.task, document=replace(self.task.document, document_bytes=data))
        gate = FinAuditGate(artifact_root=self.root)
        outcome = gate.run(FundamentalEvidenceTask(task))
        record = outcome.report
        fact = next(f for f in record["analysis"]["facts"] if f["concept"] == "example:EquityMethodResults")
        view = model_evidence_view(record)
        projected = next(f for f in view["analysis"]["facts"] if f["fact_id"] == fact["fact_id"])
        self.assertEqual(fact["concept"], projected["concept"])
        self.assertEqual("-20000000", projected["xbrl_signed_value"])
        self.assertEqual("20000000", projected["statement_signed_amount"])
        meaning = record["reference_meanings"][fact["fact_id"]]
        self.assertEqual("ANNUAL_RECONCILIATION_COMPONENT", meaning["measure_kind"])
        self.assertIn("不代表期末余额", meaning["use_limit"])
        self.assertIn("不证明正收益", meaning["use_limit"])
        self.assertTrue(gate.replay(outcome.run_ref).consistent)

    def test_research_report_shows_core_meaning_beside_interpretation(self):
        view = self.app(ScriptedResearcher()).handle(self.command)
        html = Path(view.workpaper_paths[0]).read_text()
        self.assertIn("核验口径：", html)
        self.assertIn("期间", html)

    def test_unknown_and_duplicate_driver_requests_are_rejected(self):
        with self.assertRaises(ValueError):
            FundamentalEvidenceTask(self.task, ("driver-1", "driver-1"))
        with self.assertRaisesRegex(ValueError, "NOT_ADMISSIBLE"):
            FinAuditGate(artifact_root=self.root).run(FundamentalEvidenceTask(self.task, ("driver-1",)))

    def test_future_source_stops_before_any_research_model(self):
        filing = replace(self.task, document=replace(self.task.document, declared_published_at=date(2026, 4, 1)))
        fake = ScriptedResearcher()
        with self.assertRaisesRegex(ApplicationError, "CORE_INPUT_UNAVAILABLE"):
            self.app(fake).handle(replace(self.command, filing=filing))
        self.assertEqual(0, fake.invocations)

    def test_complete_case_reopens_and_core_replays_without_researcher(self):
        view = self.app(ScriptedResearcher()).handle(self.command)
        reopened = self.app().read_case(view.case_ref)
        self.assertEqual(view, reopened)
        self.assertEqual("AWAITING_REVIEW", reopened.status)
        self.assertEqual(2, len(view.run_refs))
        for ref in view.run_refs:
            self.assertTrue(self.app().handle(ReplayRun(ref)).consistent)
        text = Path(view.workpaper_paths[0]).read_text()
        self.assertIn("合成演示", text)
        self.assertIn("重要材料与反证的处理", text)

    def test_previous_case_is_used_only_after_new_decision_is_saved(self):
        previous = self.app(ScriptedResearcher()).handle(self.command)
        app = self.app(ScriptedResearcher())
        original = app.read_case
        seen = []
        def read(ref):
            if ref == previous.case_ref:
                seen.append(len(list((self.root / "application/research-executions").glob("*/new-decision.json"))))
            return original(ref)
        app.read_case = read
        updated = app.handle(replace(self.command, previous_case_ref=previous.case_ref))
        self.assertTrue(seen and min(seen) >= 2)
        self.assertEqual(previous.case_ref, updated.latest_report["comparison"]["previous_case_ref"])
        for call in updated.latest_report["result"]["calls"]:
            self.assertNotIn("previous_case_ref", json.dumps(call["request"]))

    def test_nonexistent_previous_case_does_not_spend_model_calls(self):
        fake = ScriptedResearcher()
        with self.assertRaisesRegex(ApplicationError, "PREVIOUS_RESEARCH_CASE_NOT_FOUND"):
            self.app(fake).handle(replace(self.command, previous_case_ref="case-" + "d" * 64))
        self.assertEqual(0, fake.invocations)

    def test_previous_case_from_another_task_is_rejected(self):
        previous = self.app(ScriptedResearcher()).handle(self.command)
        with self.assertRaisesRegex(ApplicationError, "PREVIOUS_RESEARCH_TASK_MISMATCH"):
            self.app(ScriptedResearcher()).handle(replace(self.command, symbol="OTHER", previous_case_ref=previous.case_ref))

    def test_unaddressed_counterevidence_does_not_become_a_saved_case(self):
        def mutate(result):
            result["decision"]["counterevidence_response"][0]["evidence_id"] = result["analysis"]["claims"][0]["evidence_ids"][0]
        with self.assertRaisesRegex(ApplicationError, "COUNTEREVIDENCE_NOT_ADDRESSED"):
            self.app(ScriptedResearcher(mutate)).handle(self.command)
        self.assertFalse((self.root / "application/research-cases").exists())
        self.assertTrue(list((self.root / "application/research-executions").glob("*/failure.json")))

    def test_fabricated_evidence_reference_is_rejected(self):
        def mutate(result):
            result["decision"]["claims"][0]["evidence_ids"] = ["invented-fact"]
        with self.assertRaisesRegex(ApplicationError, "UNKNOWN_EVIDENCE_REFERENCE"):
            self.app(ScriptedResearcher(mutate)).handle(self.command)

    def test_reconciliation_driver_is_a_valid_derived_evidence_reference(self):
        gate = FinAuditGate(artifact_root=self.root)
        record = gate.run(FundamentalEvidenceTask(self.task)).report
        from finauditgate.adapters.research_contract import validate_proposal
        proposal = proposals(record)[0]
        proposal['claims'][0]['evidence_ids'] = [record['analysis']['drivers'][0]['driver_id']]
        validate_proposal(proposal, record)

    def test_newly_retrieved_material_cannot_be_silently_ignored(self):
        def mutate(result):
            result["decision"]["counterevidence_response"] = result["decision"]["counterevidence_response"][:1]
        with self.assertRaisesRegex(ApplicationError, "COUNTEREVIDENCE_NOT_ADDRESSED"):
            self.app(ScriptedResearcher(mutate)).handle(self.command)

    def test_initial_draft_receiving_the_other_draft_is_rejected(self):
        def mutate(result):
            result["calls"][1]["request"]["other_draft"] = result["analysis"]
        with self.assertRaisesRegex(ApplicationError, "CONTEXT_OR_STAGE_CHANGED"):
            self.app(ScriptedResearcher(mutate)).handle(self.command)

    def test_report_or_source_tampering_is_detected(self):
        view = self.app(ScriptedResearcher()).handle(self.command)
        path = Path(view.workpaper_paths[0])
        path.write_text(path.read_text() + "tampered")
        with self.assertRaisesRegex(ApplicationError, "RESEARCH_CASE_INTEGRITY_FAILED"):
            self.app().read_case(view.case_ref)

    def test_native_source_conflicts_cannot_be_hidden_by_an_adapter(self):
        changed = replace(self.task, currency="EUR")
        fake = ScriptedResearcher()
        with self.assertRaisesRegex(ApplicationError, "CORE_INPUT_UNAVAILABLE"):
            self.app(fake).handle(replace(self.command, filing=changed))
        self.assertEqual(0, fake.invocations)

    def test_model_cannot_replace_the_core_packet_before_synthesis(self):
        def mutate(result):
            result["calls"][2]["request"]["evidence"]["analysis"]["metrics"]["profit"]["current"] = "999999"
        with self.assertRaisesRegex(ApplicationError, "CONTEXT_OR_STAGE_CHANGED"):
            self.app(ScriptedResearcher(mutate)).handle(self.command)

    def test_model_cannot_rewrite_financial_numbers_in_narrative(self):
        def mutate(result):
            result["decision"]["conclusion"] = "披露金额为21.4亿元。"
        with self.assertRaisesRegex(ApplicationError, "NARRATIVE_MUST_NOT_RETYPE_NUMBERS"):
            self.app(ScriptedResearcher(mutate)).handle(self.command)

    def test_explicit_disclosure_units_are_normalized_by_core(self):
        data = self.task.document.document_bytes.replace(b'USD', b'CNY')
        data = data.replace(b'</body>', b'<h2>Operating Activities</h2><p>Operating cash flow context for 2025 includes a fictional contractual disclosure of RMB2.35 billion and RMB450 million.</p></body>')
        task = replace(self.task, currency="CNY", document=replace(self.task.document, document_bytes=data))
        gate = FinAuditGate(artifact_root=self.root)
        out = gate.run(FundamentalEvidenceTask(task))
        amounts = out.report["disclosure_amounts"]
        self.assertIn("2350000000.00", [x["value"] for x in amounts])
        self.assertIn("450000000", [x["value"] for x in amounts])
        self.assertTrue(gate.replay(out.run_ref).consistent)

    def test_cli_configuration_key_is_not_printed(self):
        from finauditgate.cli import main
        config = self.workspace / "private/model.env"
        config.write_text("DEEPSEEK_API_KEY=synthetic-secret-marker\n")
        config.chmod(0o600)
        view = SimpleNamespace(case_ref="case-" + "a" * 64, status="AWAITING_REVIEW",
            report_path="synthetic-report.md", latest_report={"signal": "REVIEW", "budget": {"calls": 0}})
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=False), patch("finauditgate.cli.FinResearchOps") as app, contextlib.redirect_stdout(output):
            app.return_value.handle.return_value = view
            code = main(["--artifact-root", str(self.root), "tradingagents-baseline", "--symbol", "AURORA",
                         "--as-of", "2026-03-02", "--env-file", str(config)])
        self.assertEqual(0, code)
        self.assertNotIn("synthetic-secret-marker", output.getvalue())

    def test_cli_rejects_world_readable_key_configuration(self):
        from finauditgate.cli import main
        config = self.workspace / "private/model.env"
        config.write_text("DEEPSEEK_API_KEY=synthetic-secret-marker\n")
        config.chmod(0o644)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = main(["--artifact-root", str(self.root), "tradingagents-baseline", "--symbol", "AURORA",
                         "--as-of", "2026-03-02", "--env-file", str(config)])
        self.assertEqual(2, code)
        self.assertIn("MODEL_CONFIG_REQUIRES_PRIVATE_PERMISSIONS", output.getvalue())
        self.assertNotIn("synthetic-secret-marker", output.getvalue())


class ModelBudgetTest(unittest.TestCase):
    def test_observed_output_violation_stops_following_calls(self):
        budget = ModelBudget(max_output_tokens=10)
        budget.reserve("short")
        self.assertFalse(budget.record_usage({"input_tokens": 4, "output_tokens": 11}))
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_LIMIT_VIOLATION"):
            budget.reserve("another")

    def test_truncated_response_cannot_trigger_a_paid_fallback(self):
        budget = ModelBudget(max_output_tokens=10)
        budget.reserve("short")
        self.assertFalse(budget.record_usage({"input_tokens": 4, "output_tokens": 10}, truncated=True))
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_TRUNCATED"):
            budget.reserve("fallback")

    def test_budget_rejects_before_overspending(self):
        budget = ModelBudget(ceiling_cny="0.001")
        with self.assertRaisesRegex(ValueError, "MODEL_SPEND_LIMIT"):
            budget.reserve("one request")
        self.assertEqual(0, budget.calls)

    def test_call_and_input_limits_are_independent(self):
        budget = ModelBudget(max_calls=1, max_input_bytes=10)
        with self.assertRaisesRegex(ValueError, "MODEL_INPUT_LIMIT"):
            budget.reserve("x" * 11)
        budget.reserve("ok")
        with self.assertRaisesRegex(ValueError, "MODEL_CALL_LIMIT"):
            budget.reserve("ok")

    def test_missing_usage_never_becomes_zero_cost(self):
        budget = ModelBudget()
        budget.reserve("ok")
        budget.record_usage({})
        self.assertIsNone(budget.receipt()["uncached_price_estimate_cny"])

    def test_invalid_budget_inputs_are_rejected(self):
        for value in ("NaN", "Infinity", "-1", "0", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ModelBudget(ceiling_cny=value)
