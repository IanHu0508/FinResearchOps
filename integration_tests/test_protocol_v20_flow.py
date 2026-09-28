"""Protocol 20 through the pinned four-analyst graph, with synthetic providers only.

A non-critical stage whose saved answers are all proven unusable continues as
an explicit placeholder and the Case is PARTIAL; a final report refused only
for a few numeric sentences gets one sentence-level repair and is checked
again. These checks establish what the saved Case must prove, not the quality
of any answer.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import test_protocol_v18_flow as v18
from test_four_analyst_flow import FourAnalystLLM
from finauditgate.adapters.thesis_degrade import NOTE
from finauditgate.adapters.thesis_repair import SCHEMA, repair_type
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.tradingagents_thesis import resume_protocol
from finauditgate.application import ApplicationError
from finauditgate.application.research_report import render as render_formal
from finauditgate.application.thesis_case import validate


VALUE = "收入为12.5亿元。"
REPAIRED = "收入变化需按来源核对，不在此处给出金额。"


class NumberDriftLLM(FourAnalystLLM):
    """Add refused numeric sentences to the final report; answer the repair as configured."""
    numeric_sentences: int = 1
    repair: str = "clean"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        if schema is not None and schema.__name__ == "FinalNumberRepair":
            system = messages[0].content
            self.requests.append({"node": json.loads(messages[-1].content)["node"], "schema": "FinalNumberRepair",
                                  "text": "\n".join(m.content for m in messages), "reasoning_effort": self.reasoning_effort})
            line = next(row for row in system.split("\n") if row.startswith('{"refused_sentences":'))
            rows = json.loads(line)["refused_sentences"]
            replacement = {"clean": REPAIRED, "numeric": "收入仍为12.5亿元。"}.get(self.repair, REPAIRED)
            answer = [{"field": r["field"], "original": r["sentence"] if self.repair != "wrong_original" else r["sentence"] + "X",
                       "replacement": replacement} for r in rows]
            return self._result(json.dumps({"replacements": answer}, ensure_ascii=False))
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if schema is not None and schema.__name__ == "FinalResearchReport":
            value = json.loads(result.generations[0].message.content)
            if self.numeric_sentences:
                value["summary"]["text"] += "".join(f"收入为{i + 20}.5亿元。" for i in range(self.numeric_sentences - 1)) + VALUE
            result.generations[0].message.content = json.dumps(value, ensure_ascii=False)
        return result


class DegradeLLM(NumberDriftLLM):
    numeric_sentences: int = 0
    raise_code_node: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        if (self.raise_code_node and schema is not None and schema.__name__ == "AnalystReport"
                and json.loads(messages[-1].content)["node"] == self.raise_code_node):
            raise ValueError("NATIVE_TRACE_CALL_LIMIT")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class FirstDraftsUnparseableLLM(NumberDriftLLM):
    """Both first drafts are unparseable once and the final report needs one sentence repair: three extra calls."""
    seen: dict = {}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        schema = kwargs.get("synthetic_schema")
        if schema is None or schema.__name__ != "InitialBrief":
            return result
        node = json.loads(messages[-1].content)["node"]
        self.seen[node] = self.seen.get(node, 0) + 1
        if self.seen[node] == 1:
            return self._result(result.generations[0].message.content[:-1] + '"}')
        return result


def trader_drift(times=2):
    return v18.FormatDriftLLM(broken_node="Trader", broken_kind="ExecutionReview", drift="action_note", broken_times=times)


class ProtocolV20FlowTest(unittest.TestCase):
    setUp = v18.ProtocolV18FlowTest.setUp
    halted = v18.ProtocolV18FlowTest.halted

    def run_case(self, model=None, *, root=None, protocol=20, resume=None, review=False):
        return v18.ProtocolV18FlowTest.run_case(self, model, root=root, protocol=protocol, resume=resume, review=review)

    def reassess(self, model, root, resume):
        from datetime import date
        from unittest.mock import patch
        from native_support import patched_native_runtime
        from test_four_analyst_flow import CONSTRAINT, HYPOTHESIS, USER_VIEW, four_sources
        from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
        from finauditgate.application import FinResearchOps
        from finauditgate.research import ResearchThesis
        app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher(protocol_version=20, resume_from=resume,
                                                                            reassess_final=True))
        command = ResearchThesis("AURORA", date(2026, 3, 2), "四维分析后形成可复核研究。", sources=four_sources(),
            review=False, hypotheses=(HYPOTHESIS,), research_constraints=(CONSTRAINT,), user_view=USER_VIEW)
        with patched_native_runtime(model), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_stocktwits_messages",
                side_effect=AssertionError("FROZEN_SOCIAL_FETCH_FORBIDDEN")), patch(
                "tradingagents.agents.analysts.sentiment_analyst.fetch_reddit_posts",
                side_effect=AssertionError("FROZEN_SOCIAL_FETCH_FORBIDDEN")):
            return app.handle(command), app

    def reopen_with_standard_library(self, root, case_ref):
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
                  "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
                  "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v20';"
                  "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(root), case_ref],
            env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_new_run_is_v20_without_degradation_or_repair(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual(("finresearchops.thesis-case/v20", "thesis-stage-recovery/v5", 3, [], [], None),
                         (record["schema_version"], record["recovery"]["policy"], record["recovery"]["max_extra_calls"],
                          record["recovery"]["attempts"], record["recovery"]["degraded"], record["number_repair"]))
        self.assertEqual((17, 17), (len(record["exchanges"]), len(record["model_calls"])))
        self.assertEqual(record["evidence_check"]["status"], record["status"])
        process = Path(view.report_path).with_name("process-record.md").read_text()
        self.assertIn("终稿若只因少数句子含未绑定数字被拒", process)
        self.assertNotIn("## 降级省略的非关键角色", process)
        self.assertEqual(view, app.read_case(view.case_ref))
        self.reopen_with_standard_library(self.root, view.case_ref)

    def test_repair_schema_is_the_frozen_schema(self):
        self.assertEqual(SCHEMA, repair_type().model_json_schema())

    def test_final_report_refused_for_one_sentence_is_repaired_once_and_rechecked(self):
        view, app, model = self.run_case(NumberDriftLLM())
        record = view.latest_report
        [attempt] = record["recovery"]["attempts"]
        self.assertEqual(("Portfolio Manager", "FinalResearchReport", "NUMBER_REPAIR", "high",
                          [{"field": "summary", "sentence": VALUE}]),
                         (attempt["node"], attempt["kind"], attempt["reason"], attempt["retry_effort"], attempt["number_sentences"]))
        [repair] = [r for r in model.requests if r["schema"] == "FinalNumberRepair"]
        final_prompt = next(r for r in model.requests if r["schema"] == "FinalResearchReport")["text"]
        self.assertTrue(repair["text"].startswith(final_prompt.split("\n{")[0]))
        self.assertIn("【本次任务变更】", repair["text"])
        self.assertEqual(18, len(record["model_calls"]))
        self.assertEqual(17, len(record["exchanges"]))
        self.assertIn(VALUE, record["exchanges"][-1]["parsed"]["summary"]["text"])
        self.assertNotIn(VALUE, record["final_report"]["summary"]["text"])
        self.assertTrue(record["final_report"]["summary"]["text"].endswith(REPAIRED))
        before, after = deepcopy(record["exchanges"][-1]["parsed"]), deepcopy(record["final_report"])
        before["summary"]["text"] = after["summary"]["text"] = ""
        self.assertEqual(before, after)
        self.assertEqual({"repair_run_id": attempt["retry_run_id"], "replacements": [
            {"field": "summary", "original": VALUE, "replacement": REPAIRED}]}, record["number_repair"])
        self.assertEqual(record["evidence_check"]["status"], record["status"])
        process = Path(view.report_path).with_name("process-record.md").read_text()
        self.assertIn("## 终稿句子级数字修复", process)
        formal, _ = render_formal(record)
        self.assertIn("终稿有1个句子因含未绑定数字被拒收", formal.decode())
        self.assertEqual(view, app.read_case(view.case_ref))
        self.reopen_with_standard_library(self.root, view.case_ref)

    def test_repair_that_is_still_refused_or_misquoted_halts_the_run(self):
        for repair in ("numeric", "wrong_original"):
            with self.subTest(repair=repair):
                root = self.root / repair
                receipt = self.halted(NumberDriftLLM(repair=repair), root, protocol=20)
                [attempt] = receipt["recovery"]["attempts"]
                self.assertEqual("NUMBER_REPAIR", attempt["reason"])
                self.assertIsNotNone(attempt["retry_run_id"])
                self.assertEqual(("Portfolio Manager", "FinalResearchReport"),
                                 (receipt["recovery"]["halted"]["node"], receipt["recovery"]["halted"]["kind"]))

    def test_too_many_refused_sentences_halt_without_a_repair_call(self):
        model = NumberDriftLLM(numeric_sentences=6)
        receipt = self.halted(model, self.root / "many", protocol=20)
        self.assertEqual([], receipt["recovery"]["attempts"])
        self.assertEqual({"node": "Portfolio Manager", "kind": "FinalResearchReport", "reason": "UNBOUND_RESEARCH_NUMBER"},
                         receipt["recovery"]["halted"])
        self.assertFalse([r for r in model.requests if r["schema"] == "FinalNumberRepair"])

    def test_v19_keeps_halting_on_a_refused_final_sentence(self):
        receipt = self.halted(NumberDriftLLM(), self.root / "v19", protocol=19)
        self.assertEqual("UNBOUND_RESEARCH_NUMBER", receipt["recovery"]["halted"]["reason"])

    def test_analyst_whose_answer_is_unusable_is_left_out_and_the_case_is_partial(self):
        view, app, model = self.run_case(DegradeLLM(invalid_source_node="News Analyst"))
        record = view.latest_report
        [row] = record["recovery"]["degraded"]
        self.assertEqual(("News Analyst", "AnalystReport", "THESIS_UNKNOWN_SOURCE_REFERENCE"), (row["node"], row["kind"], row["reason"]))
        self.assertEqual({"degraded": True, "node": "News Analyst", "kind": "AnalystReport",
                          "reason": "THESIS_UNKNOWN_SOURCE_REFERENCE", "note": NOTE}, record["analyst_reports"]["News Analyst"])
        self.assertEqual(("PARTIAL", 16, 17), (record["status"], len(record["exchanges"]), len(record["model_calls"])))
        self.assertNotIn(("News Analyst", "AnalystReport"), [(e["node"], e["kind"]) for e in record["exchanges"]])
        later = next(r for r in model.requests if r["schema"] == "InitialBrief")
        self.assertEqual(record["analyst_reports"]["News Analyst"], later["payload"]["analyst_reports"]["News Analyst"])
        self.assertIn("本角色已按降级规则省略", record["reports"]["news_report"])
        formal, _ = render_formal(record)
        self.assertIn("缺失（程序降级）", formal.decode())
        self.assertIn("已按降级规则省略，报告状态为部分完成", formal.decode())
        process = Path(view.report_path).with_name("process-record.md").read_text()
        self.assertIn("## 降级省略的非关键角色", process)
        self.assertEqual(view, app.read_case(view.case_ref))
        self.reopen_with_standard_library(self.root, view.case_ref)

    def test_review_of_a_degraded_case_names_the_left_out_stage(self):
        from test_four_analyst_flow import FourAnalystQualityLLM
        view, app, _ = self.run_case(FourAnalystQualityLLM(invalid_source_node="News Analyst"), review=True)
        self.assertEqual("PARTIAL", view.latest_report["status"])
        quality = Path(view.report_path).with_name("quality-report.md").read_text()
        self.assertIn("本次有1个非关键角色按降级规则省略，原Case状态为PARTIAL。", quality)
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_trader_left_out_after_its_one_retry_also_fails(self):
        view, app, model = self.run_case(trader_drift())
        record = view.latest_report
        [attempt], [row] = record["recovery"]["attempts"], record["recovery"]["degraded"]
        self.assertEqual(("Trader", "SCHEMA_INVALID"), (attempt["node"], attempt["reason"]))
        self.assertEqual([attempt["failed_run_id"], attempt["retry_run_id"]], row["run_ids"])
        self.assertEqual(("THESIS_STAGE_SCHEMA_INVALID", True), (row["reason"], record["execution_review"]["degraded"]))
        self.assertEqual(("PARTIAL", 16, 18), (record["status"], len(record["exchanges"]), len(record["model_calls"])))
        self.assertEqual(view, app.read_case(view.case_ref))
        once = self.run_case(trader_drift(times=1), root=self.root / "one")[0].latest_report
        self.assertEqual(([], once["evidence_check"]["status"]), (once["recovery"]["degraded"], once["status"]))

    def test_budget_or_trace_limit_stops_are_never_degraded(self):
        receipt = self.halted(DegradeLLM(raise_code_node="News Analyst"), self.root / "limit", protocol=20)
        self.assertEqual([], receipt["recovery"]["degraded"])
        self.assertEqual({"node": "News Analyst", "kind": "AnalystReport", "reason": "NATIVE_TRACE_CALL_LIMIT"},
                         receipt["recovery"]["halted"])

    def test_a_stop_is_not_degraded_when_the_run_is_resumed(self):
        self.halted(DegradeLLM(raise_code_node="News Analyst"), self.root / "limit", protocol=20)
        execution = next((self.root / "limit").glob("application/thesis-executions/*"))
        with self.assertRaises(ApplicationError):
            self.run_case(FourAnalystLLM(), root=self.root / "resumed", resume=execution)
        resumed = next((self.root / "resumed").glob("application/thesis-executions/*"))
        receipt = json.loads((resumed / "runtime-receipt.json").read_bytes())
        self.assertEqual([], receipt["recovery"]["degraded"])
        self.assertEqual(("News Analyst", "AnalystReport"), (receipt["recovery"]["halted"]["node"], receipt["recovery"]["halted"]["kind"]))
        self.assertFalse(list((self.root / "resumed").glob("application/thesis-cases/*/case.json")))

    def test_three_extra_calls_keep_the_data_review(self):
        from test_thesis_quality_flow import QualityLLM

        class ThreeWithReview(FirstDraftsUnparseableLLM, QualityLLM):
            pass
        view, app, _ = self.run_case(ThreeWithReview(seen={}), review=True)
        record = view.latest_report
        self.assertEqual(["UNPARSEABLE", "UNPARSEABLE", "NUMBER_REPAIR"], [a["reason"] for a in record["recovery"]["attempts"]])
        directory = Path(view.report_path).parent
        self.assertFalse((directory / "quality-rejected.json").exists())
        self.assertEqual("finresearchops.thesis-review/v3", view.review["schema_version"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_reassessed_final_can_use_its_sentence_repair(self):
        final_drift = v18.FormatDriftLLM(broken_node="Portfolio Manager", broken_kind="FinalResearchReport", drift="quote")
        self.run_case(final_drift, root=self.root / "first")
        execution = next((self.root / "first").glob("application/thesis-executions/*"))
        view, app = self.reassess(NumberDriftLLM(), self.root / "second", execution)
        recovery = view.latest_report["recovery"]
        self.assertEqual(["UNPARSEABLE", "NUMBER_REPAIR"], [a["reason"] for a in recovery["attempts"]])
        self.assertEqual(2, len(recovery["retired_final_calls"]))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_reader_requires_the_retries_to_be_used_up(self):
        record = self.run_case(DegradeLLM(invalid_source_node="News Analyst"), root=self.root / "d")[0].latest_report
        unparseable = deepcopy(record)
        [row] = unparseable["recovery"]["degraded"]
        call = next(c for c in unparseable["model_calls"] if c["run_id"] == row["run_ids"][0])
        call["output"][0]["content"] = '{"analysis": "x"'
        with self.assertRaisesRegex(ValueError, "THESIS_DEGRADED_STAGE_NOT_PROVEN"):
            validate(unparseable)
        for code in ("NATIVE_TRACE_CALL_LIMIT", "THESIS_STAGE_RECOVERY_EXHAUSTED"):
            stopped = deepcopy(record)
            stopped["recovery"]["degraded"][0]["reason"] = code
            stopped["analyst_reports"]["News Analyst"]["reason"] = code
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate(stopped)

    def test_critical_stage_failures_still_halt(self):
        receipt = self.halted(v18.FormatDriftLLM(broken_node="Bear Researcher", broken_kind="RevisionBrief", broken_times=2),
                              self.root / "critical", protocol=20)
        self.assertEqual([], receipt["recovery"]["degraded"])
        self.assertEqual(("Bear Researcher", "RevisionBrief"), (receipt["recovery"]["halted"]["node"], receipt["recovery"]["halted"]["kind"]))

    def test_resume_after_degradation_reuses_every_call_and_refuses_after_repair(self):
        view, _, _ = self.run_case(DegradeLLM(invalid_source_node="News Analyst"), root=self.root / "original")
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        self.assertEqual(20, resume_protocol(execution, 20))
        repeated, app, model = self.run_case(FourAnalystLLM(), root=self.root / "resumed", resume=execution)
        self.assertEqual([], model.requests)
        self.assertEqual("SAME_V20_FLOW", repeated.latest_report["reused_calls"]["budget_origin"])
        self.assertEqual(view.latest_report["model_calls"], repeated.latest_report["model_calls"])
        self.assertEqual(view.latest_report["recovery"], repeated.latest_report["recovery"])
        self.assertEqual(repeated, app.read_case(repeated.case_ref))
        request = json.loads((execution / "request.json").read_bytes())
        with self.assertRaises(ValueError):
            CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=19)
        self.run_case(NumberDriftLLM(), root=self.root / "repaired")
        repaired = next((self.root / "repaired").glob("application/thesis-executions/*"))
        with self.assertRaisesRegex(ValueError, "THESIS_RESUME_AFTER_NUMBER_REPAIR_UNSUPPORTED"):
            CompletedCalls(repaired, request["request"], request["sources"], "deepseek-flash", protocol_version=20)

    def test_tampered_degradation_or_repair_records_are_refused(self):
        degraded = self.run_case(DegradeLLM(invalid_source_node="News Analyst"), root=self.root / "d")[0].latest_report
        repaired = self.run_case(NumberDriftLLM(), root=self.root / "r")[0].latest_report
        validate(deepcopy(degraded))
        validate(deepcopy(repaired))
        cases = [
            (degraded, lambda r: r.update(status="COMPLETED")),
            (degraded, lambda r: r["analyst_reports"]["News Analyst"].update(reason="OTHER")),
            (degraded, lambda r: r["recovery"]["degraded"][0].update(run_ids=[])),
            (degraded, lambda r: r["recovery"].update(degraded=[])),
            (degraded, lambda r: r["recovery"]["degraded"].append({"node": "Market Analyst", "kind": "AnalystReport",
                                                                   "reason": "X", "run_ids": [r["model_calls"][1]["run_id"]]})),
            (repaired, lambda r: r["final_report"]["summary"].update(text=r["final_report"]["summary"]["text"] + "补充。")),
            (repaired, lambda r: r["number_repair"]["replacements"][0].update(replacement="其他改写。")),
            (repaired, lambda r: r.update(number_repair=None)),
            (repaired, lambda r: r["recovery"]["attempts"][0].update(number_sentences=[{"field": "summary", "sentence": "别句。"}])),
            (repaired, lambda r: r.update(final_report=deepcopy(r["exchanges"][-1]["parsed"]))),
        ]
        for record, mutate in cases:
            broken = deepcopy(record)
            mutate(broken)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate(broken)

    def test_degradation_proof_and_repair_prompt_are_checked_on_their_own(self):
        degraded = self.run_case(DegradeLLM(invalid_source_node="News Analyst"), root=self.root / "d")[0].latest_report
        call = next(c for c in degraded["model_calls"] if c["node"] == "News Analyst")
        answer = json.loads(call["output"][0]["content"])
        answer.update(evidence_refs=["NEWS"], observations=[{"statement": "合成观察。", "evidence_refs": ["NEWS"]}])
        call["output"][0]["content"] = json.dumps(answer, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "THESIS_DEGRADED_STAGE_NOT_PROVEN"):
            validate(degraded)
        repaired = self.run_case(NumberDriftLLM(), root=self.root / "r")[0].latest_report
        repair = next(c for c in repaired["model_calls"] if c["run_id"] == repaired["number_repair"]["repair_run_id"])
        repair["messages"][0][0]["content"] += "另请补充一个目标价。"
        with self.assertRaisesRegex(ValueError, "THESIS_RECOVERY_INPUT_CHANGED"):
            validate(repaired)


if __name__ == "__main__":
    unittest.main()
