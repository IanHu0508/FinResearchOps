"""Intermediate manager IDs are visible gaps, never guessed final decisions."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from native_support import patched_native_runtime
from test_four_analyst_flow import (
    CONSTRAINT, HYPOTHESIS, USER_VIEW, FourAnalystLLM, four_sources,
)
from test_thesis_correction import support
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.thesis_protocol import ThesisSession
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.research import ResearchThesis


UNBOUND_IDS = ("H1", "H2", "QUANT", "MACRO", "H3", "H4", "BULL_VIEW", "BEAR_VIEW")
QUESTION = "中间稿编号缺项保留提示，最终逐项判断仍须完整。"


class ManagerCoverageLLM(FourAnalystLLM):
    duplicate_manager_id: bool = False
    blank_manager_reason: bool = False
    final_problem: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        if kind not in ("ResearchEvaluation", "ForwardRevision"):
            return result
        value = json.loads(result.generations[0].message.content)
        if kind == "ResearchEvaluation":
            identifiers = list(UNBOUND_IDS)
            if self.duplicate_manager_id:
                identifiers[-1] = identifiers[0]
            value["assessments"] = [{"claim_id": identity, "disposition": "conditional",
                "reason": f"SYNTHETIC_MANAGER_REASON_{i}：保留本项论述与条件，不猜测它对应哪个原始论点。"}
                for i, identity in enumerate(identifiers)]
            value["valuation_basis_and_gaps"] = "SYNTHETIC_MANAGER_VALUATION：缺少校准，不将中间稿编号当成评级依据。"
            if self.blank_manager_reason:
                value["assessments"][0]["reason"] = ""
        elif self.final_problem == "claims":
            value["claim_assessments"][0]["claim_id"] = "H99"
        elif self.final_problem == "beliefs":
            value["belief_updates"][0]["belief_id"] = "D4"
        result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class ManagerCoverageDisplayTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    @staticmethod
    def budget():
        return ModelBudget(ceiling_cny="100", max_calls=24,
                           max_input_bytes=524288, max_output_tokens=65536)

    def run_case(self, model=None, *, root=None, protocol=17, resume=None, replay=False, budget=None):
        root = root or self.root
        model = model or ManagerCoverageLLM()
        budget = budget or self.budget()
        app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(
            protocol_version=protocol, live=True, budget=budget, resume_from=resume,
            replay_presentation_failure=replay))
        command = ResearchThesis("AURORA", date(2026, 3, 2), QUESTION,
            sources=four_sources(), review=False, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        with patched_native_runtime(model), patch(
                "finauditgate.adapters.model_http.model_http_client",
                return_value=SimpleNamespace(close=lambda: None)):
            view = app.handle(command)
        execution = next(root.glob("application/thesis-executions/*"))
        return view, app, model, budget, execution

    def assert_preserved_and_final_complete(self, view, app):
        record = view.latest_report
        validate(record)
        self.assertEqual(17, len(record["exchanges"]))
        manager = next(e for e in record["exchanges"] if e["kind"] == "ResearchEvaluation")
        raw = next(o["content"] for c in record["model_calls"] for o in c.get("output", [])
                   if o.get("id") == manager["response_id"])
        raw_value = json.loads(raw)
        self.assertEqual(raw_value, manager["parsed"])
        self.assertEqual(raw_value, record["research_evaluation"])
        self.assertEqual(8, len(raw_value["assessments"]))
        claims = [row["id"] for row in record["updated_claims"]]
        final_claims = [row["claim_id"] for row in record["forward_revision"]["claim_assessments"]]
        final_beliefs = [row["belief_id"] for row in record["forward_revision"]["belief_updates"]]
        self.assertCountEqual(claims, final_claims)
        self.assertCountEqual([row["belief_id"] for row in record["independent_assessment"]["beliefs"]], final_beliefs)
        self.assertTrue(all(identity not in final_claims for identity in UNBOUND_IDS))
        main = Path(view.report_path).read_text()
        appendix = Path(view.report_path).with_name("process-record.md").read_text()
        display = main + appendix
        self.assertIn("研究经理中间稿的编号提示", display)
        self.assertIn("未逐项绑定的原论点：" + "、".join(claims), display)
        self.assertIn("不能绑定到原论点的经理编号：", display)
        for row in raw_value["assessments"]:
            self.assertIn(row["claim_id"], display)
            self.assertIn(row["reason"], display)
        self.assertIn(raw_value["valuation_basis_and_gaps"], display)
        for field in ("rationale", "strategic_actions"):
            self.assertIn(raw_value["plan"][field], display)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_v17_preserves_every_unbound_manager_assessment_and_completes_final_coverage(self):
        view, app, model, _, _ = self.run_case()
        self.assert_preserved_and_final_complete(view, app)
        self.assertEqual(17, len(model.requests))
        self.assertEqual(list(UNBOUND_IDS), [r["claim_id"] for r in view.latest_report["research_evaluation"]["assessments"]])
        manager_call = next(c for c in view.latest_report["model_calls"] if c["node"] == "Research Manager")
        system_prompt = manager_call["messages"][0][0]["content"]
        for claim in view.latest_report["updated_claims"]:
            self.assertIn(claim["id"], system_prompt)
        self.assertIn("QUANT", system_prompt)
        self.assertIn("claim_id", system_prompt)

    def test_duplicate_unbound_ids_do_not_drop_any_original_manager_reason(self):
        view, app, _, _, _ = self.run_case(ManagerCoverageLLM(duplicate_manager_id=True))
        self.assert_preserved_and_final_complete(view, app)
        assessments = view.latest_report["research_evaluation"]["assessments"]
        self.assertEqual(2, sum(row["claim_id"] == "H1" for row in assessments))
        text = Path(view.report_path).read_text() + Path(view.report_path).with_name("process-record.md").read_text()
        self.assertIn("重复", text)
        self.assertIn(assessments[0]["reason"], text)
        self.assertIn(assessments[-1]["reason"], text)

    def test_v16_keeps_its_original_strict_manager_coverage(self):
        model = ManagerCoverageLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, protocol=16)
        self.assertEqual("ResearchEvaluation", model.requests[-1]["schema"])
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))

    def test_final_claim_and_belief_coverage_remain_strict(self):
        for problem in ("claims", "beliefs"):
            with self.subTest(problem=problem):
                root = self.root / problem
                model = ManagerCoverageLLM(final_problem=problem)
                with self.assertRaises(ApplicationError):
                    self.run_case(model, root=root)
                self.assertEqual("ForwardRevision", model.requests[-1]["schema"])
                self.assertFalse(list(root.glob("application/thesis-cases/*/case.json")))

    def test_blank_manager_reason_is_not_converted_to_a_coverage_warning(self):
        model = ManagerCoverageLLM(blank_manager_reason=True)
        with self.assertRaises(ApplicationError):
            self.run_case(model)
        self.assertEqual("ResearchEvaluation", model.requests[-1]["schema"])
        self.assertFalse(list(self.root.glob("application/thesis-cases/*/case.json")))

    def old_manager_halt(self):
        original_ask, original_run = ThesisSession.ask, ThesisSession.run_node

        def old_prompt(session, node, kind, *args, **kwargs):
            version = session.protocol_version
            try:
                if version == 17 and kind == "ResearchEvaluation":
                    session.protocol_version = 16
                return original_ask(session, node, kind, *args, **kwargs)
            finally:
                session.protocol_version = version

        def old_coverage(session, node, state, config):
            value = original_run(session, node, state, config)
            if node == "Research Manager":
                session.coverage(session.research["assessments"], [c["id"] for c in session.updated_claims()])
            return value

        root = self.root / "old"
        model, budget = ManagerCoverageLLM(), self.budget()
        with patch.object(ThesisSession, "ask", old_prompt), patch.object(ThesisSession, "run_node", old_coverage), \
                self.assertRaises(ApplicationError):
            self.run_case(model, root=root, budget=budget)
        self.assertEqual(9, budget.calls)
        execution = next(root.glob("application/thesis-executions/*"))
        original = {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()}
        runtime = json.loads(original[Path("runtime-receipt.json")])
        self.assertEqual({"node": "Research Manager", "kind": "ResearchEvaluation",
                          "reason": "THESIS_CLAIM_COVERAGE_INVALID"}, runtime["recovery"]["halted"])
        self.assertEqual(9, len(runtime["model_calls"]))
        return execution, original, runtime, budget

    def test_default_resume_does_not_reopen_the_old_manager_halt(self):
        execution, original, _, _ = self.old_manager_halt()
        model = ManagerCoverageLLM()
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=self.root / "default", resume=execution)
        self.assertEqual([], model.requests)
        self.assertEqual(original, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})

    def test_explicit_replay_keeps_nine_old_answers_and_only_adds_eight_new_paid_calls(self):
        execution, original, runtime, old_budget = self.old_manager_halt()
        view, app, model, budget, _ = self.run_case(root=self.root / "explicit", resume=execution, replay=True)
        self.assert_preserved_and_final_complete(view, app)
        self.assertEqual(8, len(model.requests))
        self.assertEqual("ExecutionReview", model.requests[0]["schema"])
        self.assertEqual(17, budget.calls)
        self.assertEqual(old_budget.receipt()["usage"], budget.receipt()["usage"][:9])
        self.assertEqual(9, view.latest_report["reused_calls"]["used_calls"])
        self.assertEqual(runtime["model_calls"], view.latest_report["model_calls"][:9])
        self.assertEqual(runtime["recovery"]["halted"],
                         view.latest_report["reused_calls"]["presentation_replay"]["original_halt"])
        self.assertEqual(json.loads(runtime["model_calls"][-1]["output"][0]["content"]),
                         view.latest_report["research_evaluation"])
        self.assertEqual(original, {p.relative_to(execution): p.read_bytes() for p in execution.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
