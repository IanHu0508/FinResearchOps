"""Protocol 18 through the pinned four-analyst graph, with synthetic providers only.

These checks establish when a stage may be asked again and what the saved Case
must prove afterwards; they say nothing about the quality of any answer.
"""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
from native_support import patched_native_runtime
from test_four_analyst_flow import CONSTRAINT, HYPOTHESIS, USER_VIEW, FourAnalystLLM, FourAnalystQualityLLM, four_sources
from test_research_numbers import inputs
from test_thesis_correction import support
from finauditgate.adapters.thesis_format import schema_errors
from finauditgate.application.research_delivery import final_instruction
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.thesis_schemas_v18 import SCHEMAS
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher, resume_protocol
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.application.thesis_case import validate
from finauditgate.research import ResearchThesis


REPAIR_MARKER = "本次仅修正上一响应中不在候选值内的依据类型标签"
LABEL_PATH = ["changes", 0, "replacement", "amount", "basis_type"]


class FormatDriftLLM(FourAnalystLLM):
    """Deviate at one stage; later requests for that stage answer normally."""
    broken_node: str = "Bear Researcher"
    broken_kind: str = "RevisionBrief"
    broken_times: int = 1
    drift: str = "quote"
    tamper_repair: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        request = self.requests[-1]
        if not schema or (request["node"], schema.__name__) != (self.broken_node, self.broken_kind):
            return result
        attempt = sum((r["node"], r["schema"]) == (self.broken_node, self.broken_kind) for r in self.requests)
        value = json.loads(result.generations[0].message.content)
        marked = next((m.content for m in messages if REPAIR_MARKER in m.content), None)
        if marked is not None:
            repaired = json.loads(marked.rsplit("\n", 1)[1])["previous_response"]
            repaired["changes"][0]["replacement"]["amount"]["basis_type"] = "analyst_assumption"
            if self.tamper_repair:
                repaired["changes"][0]["reason"] += "（修复时改写）"
            return self._result(json.dumps(repaired, ensure_ascii=False))
        if attempt > self.broken_times:
            return result
        if self.drift == "quote":
            text = json.dumps(value, ensure_ascii=False)
            return self._result(text.replace("：", '："未转义引号"', 1) if "：" in text else text[:-1] + '"}')
        if self.drift == "label":
            value["changes"][0]["replacement"]["amount"]["basis_type"] = "accounting_correction"
        elif self.drift == "echo":
            value["beliefs"][1]["belief_id_note"] = value["beliefs"][1]["belief_id"]
        elif self.drift == "labels_in_prose":
            value["summary"]["text"] += "2025上半年的经营变化仍需核对，H2资料尚未披露。"
        elif self.drift == "action_note":
            value["proposal"]["action_note"] = "SYNTHETIC：该动作仅为过程提案，不作为最终经理的硬门槛。"
        elif self.drift == "missing_refs":
            del value["evidence_refs"]
        elif self.drift == "blank_note":
            for claim in value["claims"]:
                claim["claims_note"] = ""
        elif self.drift in ("lax_date", "lax_date_bad_ref", "bad_ref"):
            if self.drift != "bad_ref":
                value["forecast_start"] += "T00:00:00"
            if self.drift != "lax_date":
                value["scenarios"][0]["revenue"]["evidence_refs"].append("S99_SYNTHETIC_UNKNOWN")
        return self._result(json.dumps(value, ensure_ascii=False))


class ProtocolV18FlowTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def run_case(self, model=None, *, root=None, protocol=18, resume=None, review=False):
        model = model or FourAnalystLLM()
        app = FinResearchOps(artifact_root=root or self.root, researcher=ThesisResearcher(
            protocol_version=protocol, resume_from=resume))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "四维分析后形成可复核研究。",
            sources=four_sources(), review=review, hypotheses=(HYPOTHESIS,),
            research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        with patched_native_runtime(model), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_stocktwits_messages",
                side_effect=AssertionError("FROZEN_SOCIAL_FETCH_FORBIDDEN")), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_reddit_posts",
                side_effect=AssertionError("FROZEN_SOCIAL_FETCH_FORBIDDEN")):
            view = app.handle(command)
        return view, app, model

    def halted(self, model, root, protocol=18):
        with self.assertRaises(ApplicationError):
            self.run_case(model, root=root, protocol=protocol)
        execution = next(root.glob("application/thesis-executions/*"))
        self.assertFalse(list(root.glob("application/thesis-cases/*/case.json")))
        return json.loads((execution / "runtime-receipt.json").read_bytes())

    def test_new_run_is_v18_and_reopens_with_standard_library_only(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual("finresearchops.thesis-case/v18", record["schema_version"])
        self.assertEqual("thesis-stage-recovery/v3", record["recovery"]["policy"])
        self.assertEqual([], record["recovery"]["attempts"])
        self.assertEqual(17, len(record["model_calls"]))
        final = [r for r in model.requests if r["schema"] == "FinalResearchReport"]
        self.assertIn("年份写成“2025年”并与期间连写", final[0]["text"])
        self.assertIn(final_instruction(18), final[0]["text"])
        self.assertEqual(view, app.read_case(view.case_ref))
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
                  "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
                  "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v18';"
                  "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_unparseable_answer_is_asked_again_once_with_the_same_prompt(self):
        view, app, model = self.run_case(FormatDriftLLM(), root=self.root / "retried")
        record = view.latest_report
        stage = [r for r in model.requests if (r["node"], r["schema"]) == ("Bear Researcher", "RevisionBrief")]
        self.assertEqual(2, len(stage))
        self.assertEqual(stage[0]["text"], stage[1]["text"])
        self.assertEqual(18, len(record["model_calls"]))
        [attempt] = record["recovery"]["attempts"]
        self.assertEqual(("Bear Researcher", "RevisionBrief", "UNPARSEABLE", [], []),
                         (attempt["node"], attempt["kind"], attempt["reason"], attempt["missing_reason_paths"], attempt["enum_paths"]))
        failed = next(c for c in record["model_calls"] if c["run_id"] == attempt["failed_run_id"])
        with self.assertRaises(json.JSONDecodeError):
            json.loads(failed["output"][0]["content"])
        self.assertEqual(view, app.read_case(view.case_ref))
        runtime = self.halted(FormatDriftLLM(broken_times=2), self.root / "twice")
        self.assertEqual({"node": "Bear Researcher", "kind": "RevisionBrief", "reason": "JSONDecodeError"},
                         runtime["recovery"]["halted"])
        self.assertEqual(2, sum(c["node"] == "Bear Researcher" and c["thesis_stage"]["kind"] == "RevisionBrief"
                                for c in runtime["model_calls"]))
        runtime = self.halted(FormatDriftLLM(), self.root / "v17", protocol=17)
        self.assertEqual([], runtime["recovery"]["attempts"])

    def test_out_of_vocabulary_basis_label_alone_is_repaired(self):
        model = FormatDriftLLM(broken_node="Portfolio Manager", broken_kind="ForwardRevision", drift="label")
        view, app, _ = self.run_case(model, root=self.root / "label")
        record = view.latest_report
        [attempt] = record["recovery"]["attempts"]
        self.assertEqual(("ENUM_INVALID", [LABEL_PATH]), (attempt["reason"], attempt["enum_paths"]))
        requests = [r for r in model.requests if r["schema"] == "ForwardRevision"]
        self.assertEqual([False, True], [REPAIR_MARKER in r["text"] for r in requests])
        failed = next(c for c in record["model_calls"] if c["run_id"] == attempt["failed_run_id"])
        original = json.loads(failed["output"][0]["content"])
        original["changes"][0]["replacement"]["amount"]["basis_type"] = "analyst_assumption"
        self.assertEqual(original, record["forward_revision"])
        self.assertEqual(view, app.read_case(view.case_ref))
        runtime = self.halted(FormatDriftLLM(broken_node="Portfolio Manager", broken_kind="ForwardRevision",
                                             drift="label", tamper_repair=True), self.root / "tampered")
        self.assertEqual("THESIS_REPAIR_CHANGED_EXISTING_CONTENT", runtime["recovery"]["halted"]["reason"])

    def test_echo_note_is_dropped_without_a_second_call(self):
        model = FormatDriftLLM(broken_node="Portfolio Manager", broken_kind="IndependentAssessment", drift="echo")
        view, app, _ = self.run_case(model, root=self.root / "echo")
        record = view.latest_report
        self.assertEqual(17, len(record["model_calls"]))
        self.assertEqual([], record["recovery"]["attempts"])
        raw = json.loads(record["model_calls"][13]["output"][0]["content"])
        self.assertEqual(raw["beliefs"][1]["belief_id"], raw["beliefs"][1]["belief_id_note"])
        self.assertNotIn("belief_id_note", record["independent_assessment"]["beliefs"][1])
        self.assertEqual(view, app.read_case(view.case_ref))
        self.halted(FormatDriftLLM(broken_node="Portfolio Manager", broken_kind="IndependentAssessment", drift="echo"),
                    self.root / "echo-v17", protocol=17)

    def test_time_labels_complete_a_report_that_v17_leaves_partial(self):
        drift = dict(broken_node="Portfolio Manager", broken_kind="FinalResearchReport", drift="labels_in_prose")
        old, _, _ = self.run_case(FormatDriftLLM(**drift), root=self.root / "labels-v17", protocol=17)
        self.assertEqual("PARTIAL", old.latest_report["status"])
        self.assertEqual(["2025", "H2"], [f["reference"] for f in old.latest_report["evidence_check"]["findings"]])
        view, app, _ = self.run_case(FormatDriftLLM(**drift), root=self.root / "labels-v18")
        self.assertEqual("COMPLETED", view.latest_report["status"])
        self.assertEqual([], view.latest_report["evidence_check"]["findings"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_resume_keeps_the_v18_chain_and_refuses_other_protocols(self):
        view, _, _ = self.run_case(FormatDriftLLM(), root=self.root / "original")
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        self.assertEqual(18, resume_protocol(execution, 18))
        with self.assertRaisesRegex(ValueError, "THESIS_RESUME_PROTOCOL_MISMATCH"):
            resume_protocol(execution, 16)
        repeated, app, model = self.run_case(FourAnalystLLM(), root=self.root / "resumed", resume=execution)
        self.assertEqual([], model.requests)
        self.assertEqual("SAME_V18_FLOW", repeated.latest_report["reused_calls"]["budget_origin"])
        self.assertEqual(view.latest_report["model_calls"], repeated.latest_report["model_calls"])
        self.assertEqual(view.latest_report["recovery"], repeated.latest_report["recovery"])
        self.assertEqual(repeated, app.read_case(repeated.case_ref))
        request = json.loads((execution / "request.json").read_bytes())
        with self.assertRaises(ValueError):
            CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=17)

    def test_relabelled_or_downgraded_records_are_refused(self):
        view, _, _ = self.run_case(FormatDriftLLM(), root=self.root / "relabel")
        record = deepcopy(view.latest_report)
        validate(record)
        for mutate in (lambda r: r.update(schema_version="finresearchops.thesis-case/v17"),
                       lambda r: r["recovery"].update(policy="thesis-stage-recovery/v2"),
                       lambda r: r["recovery"]["attempts"][0].update(reason="EMPTY_RESPONSE")):
            broken = deepcopy(record)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate(broken)

    def test_frozen_stage_schemas_equal_the_live_models(self):
        from finauditgate.adapters.thesis_analysts import schemas as analyst_schemas
        from finauditgate.adapters.thesis_correction import correction_schemas
        from finauditgate.adapters.thesis_protocol import schemas
        types = schemas()
        types.update(correction_schemas(types, bound=True, selected=True))
        types.update(analyst_schemas())
        for kind, frozen in SCHEMAS.items():
            with self.subTest(kind=kind):
                self.assertEqual(frozen, types[kind].model_json_schema())
        live = [k for k in types if k not in ("FinalAssessment", "DataReview")]
        self.assertEqual(sorted(live), sorted(SCHEMAS))

    def test_optional_quality_review_reopens_on_a_v18_case(self):
        view, app, model = self.run_case(FourAnalystQualityLLM(), root=self.root / "quality", review=True)
        self.assertEqual("finresearchops.thesis-case/v18", view.latest_report["schema_version"])
        self.assertEqual(19, len(model.requests))
        self.assertEqual("COMPLETED", view.review["status"])
        self.assertEqual("quality-report.md", Path(view.delivery_report_path).name)
        revision = next(r for r in model.requests if r["schema"] == "QualityRevision")
        self.assertIn(final_instruction(18), revision["text"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_standard_library_and_pydantic_agree_on_boundary_values(self):
        import pydantic
        from finauditgate.adapters.thesis_correction import correction_schemas
        from finauditgate.adapters.thesis_protocol import schemas
        types = schemas()
        types.update(correction_schemas(types, bound=True, selected=True))
        draft = deepcopy(inputs()[0])
        draft["limitations"] = ["SYNTHETIC limitation"]
        cases = {"currency newline": lambda d: d.update(reporting_currency="USD\n"),
                 "scenario newline": lambda d: d["scenarios"][0].update(scenario_id="F1\n"),
                 "invalid date": lambda d: d.update(forecast_start="2027-02-30"),
                 "not finite": lambda d: d["scenarios"][0]["revenue"].update(value=float("nan")),
                 "bool number": lambda d: d["scenarios"][0]["revenue"].update(value=True),
                 "label": lambda d: d["scenarios"][0]["revenue"].update(basis_type="accounting_correction"),
                 "unchanged": lambda d: None}
        for name, mutate in cases.items():
            value = deepcopy(draft)
            mutate(value)
            try:
                types["UnderwritingDraft"].model_validate(value)
                live = False
            except pydantic.ValidationError:
                live = True
            with self.subTest(case=name):
                self.assertEqual(live, bool(schema_errors("UnderwritingDraft", value)))

    def test_standard_library_and_pydantic_agree_on_saved_stage_answers(self):
        view, _, _ = self.run_case(FormatDriftLLM(), root=self.root / "agree")
        record = view.latest_report
        for exchange in record["exchanges"]:
            with self.subTest(kind=exchange["kind"], node=exchange["node"]):
                if exchange["kind"] != "FinalResearchReport":
                    self.assertEqual([], schema_errors(exchange["kind"], exchange["parsed"]))


if __name__ == "__main__":
    unittest.main()
