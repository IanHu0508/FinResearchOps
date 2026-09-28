"""Protocol 19 through the pinned four-analyst graph, with synthetic providers only.

A schema-invalid answer gets one re-ask with the unchanged prompt; everything
the reader proves about v18 recoveries still holds, and v18 itself is unchanged.
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
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.tradingagents_thesis import resume_protocol
from finauditgate.application.research_delivery import final_instruction
from finauditgate.application.thesis_case import validate


REMOVED = "均线周期、产品型号、页码和计数等其他含数字的写法会被标为待核，能用文字表达时不写数字。"
DRIFTS = {"action_note": ("Trader", "ExecutionReview"), "missing_refs": ("Market Analyst", "AnalystReport"),
          "blank_note": ("Bear Researcher", "InitialBrief"), "quote": ("Bear Researcher", "RevisionBrief"),
          "label": ("Portfolio Manager", "ForwardRevision"), "lax_date": ("Portfolio Manager", "UnderwritingDraft"),
          "lax_date_bad_ref": ("Portfolio Manager", "UnderwritingDraft"), "bad_ref": ("Portfolio Manager", "UnderwritingDraft")}


def drifted(name, times=1):
    node, kind = DRIFTS[name]
    return v18.FormatDriftLLM(broken_node=node, broken_kind=kind, drift=name, broken_times=times)


class ProtocolV19FlowTest(unittest.TestCase):
    setUp = v18.ProtocolV18FlowTest.setUp
    halted = v18.ProtocolV18FlowTest.halted

    def run_case(self, model=None, *, root=None, protocol=19, resume=None, review=False):
        return v18.ProtocolV18FlowTest.run_case(self, model, root=root, protocol=protocol, resume=resume, review=review)

    def test_new_run_is_v19_without_the_removed_sentence_and_reopens_with_standard_library_only(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual(("finresearchops.thesis-case/v19", "thesis-stage-recovery/v4", []),
                         (record["schema_version"], record["recovery"]["policy"], record["recovery"]["attempts"]))
        final = next(r for r in model.requests if r["schema"] == "FinalResearchReport")
        self.assertIn("年份写成“2025年”并与期间连写", final["text"])
        self.assertNotIn(REMOVED, final["text"])
        process = Path(view.report_path).with_name("process-record.md").read_text()
        self.assertIn("恢复只处理明确的技术失败、无法解析或不符合结构约定的回答", process)
        self.assertIn("无法解析或不符合结构约定时用原提示重问一次", process)
        self.assertEqual(view, app.read_case(view.case_ref))
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
                  "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
                  "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v19';"
                  "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_schema_invalid_answers_are_asked_again_once_with_the_same_prompt(self):
        for name in ("action_note", "missing_refs"):
            node, kind = DRIFTS[name]
            with self.subTest(drift=name):
                view, app, model = self.run_case(drifted(name), root=self.root / name)
                record = view.latest_report
                stage = [r for r in model.requests if (r["node"], r["schema"]) == (node, kind)]
                self.assertEqual(2, len(stage))
                self.assertEqual(stage[0]["text"], stage[1]["text"])
                [attempt] = record["recovery"]["attempts"]
                errors = {"action_note": [["extra", "proposal", "action_note"]], "missing_refs": [["missing", "evidence_refs"]]}[name]
                self.assertEqual((node, kind, "SCHEMA_INVALID", [], [], errors),
                                 (attempt["node"], attempt["kind"], attempt["reason"],
                                  attempt["missing_reason_paths"], attempt["enum_paths"], attempt["schema_errors"]))
                self.assertEqual(18, len(record["model_calls"]))
                self.assertEqual(view, app.read_case(view.case_ref))
                runtime = self.halted(drifted(name), self.root / (name + "-v18"), protocol=18)
                self.assertEqual([], runtime["recovery"]["attempts"])
                runtime = self.halted(drifted(name, times=2), self.root / (name + "-twice"), protocol=19)
                self.assertEqual({"node": node, "kind": kind, "reason": "THESIS_STAGE_SCHEMA_INVALID"}, runtime["recovery"]["halted"])
                self.assertEqual(["SCHEMA_INVALID"], [a["reason"] for a in runtime["recovery"]["attempts"]])

    def test_v18_recovery_classes_still_apply_inside_v19(self):
        for name, reason in (("quote", "UNPARSEABLE"), ("label", "ENUM_INVALID")):
            with self.subTest(drift=name):
                view, app, _ = self.run_case(drifted(name), root=self.root / name)
                self.assertEqual([reason], [a["reason"] for a in view.latest_report["recovery"]["attempts"]])
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_schema_is_checked_before_content_so_citation_failures_still_halt(self):
        view, app, model = self.run_case(drifted("lax_date"), root=self.root / "lax")
        self.assertEqual([("SCHEMA_INVALID", [["date", "forecast_start"]])],
                         [(a["reason"], a["schema_errors"]) for a in view.latest_report["recovery"]["attempts"]])
        self.assertEqual(2, sum(r["schema"] == "UnderwritingDraft" for r in model.requests))
        self.assertEqual(view, app.read_case(view.case_ref))
        runtime = self.halted(drifted("bad_ref"), self.root / "bad-ref", protocol=19)
        self.assertEqual(([], "THESIS_UNKNOWN_SOURCE_REFERENCE"),
                         (runtime["recovery"]["attempts"], runtime["recovery"]["halted"]["reason"]))
        runtime = self.halted(drifted("lax_date_bad_ref", times=2), self.root / "both", protocol=19)
        self.assertEqual("THESIS_STAGE_SCHEMA_INVALID", runtime["recovery"]["halted"]["reason"])

    def test_quality_revision_uses_the_v19_rules(self):
        view, app, model = self.run_case(v18.FourAnalystQualityLLM(), root=self.root / "quality", review=True)
        revision = next(r for r in model.requests if r["schema"] == "QualityRevision")
        self.assertIn(final_instruction(19), revision["text"])
        self.assertNotIn(REMOVED, revision["text"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_blank_notes_are_dropped_without_a_second_call(self):
        view, app, _ = self.run_case(drifted("blank_note"), root=self.root / "blank")
        record = view.latest_report
        self.assertEqual((17, []), (len(record["model_calls"]), record["recovery"]["attempts"]))
        raw = json.loads(record["model_calls"][5]["output"][0]["content"])
        self.assertEqual([""] * len(raw["claims"]), [c["claims_note"] for c in raw["claims"]])
        self.assertTrue(all("claims_note" not in c for c in record["initial"]["Bear Researcher"]["claims"]))
        self.assertEqual(view, app.read_case(view.case_ref))
        self.halted(drifted("blank_note"), self.root / "blank-v18", protocol=18)

    def test_resume_keeps_the_v19_chain_and_refuses_other_protocols(self):
        view, _, _ = self.run_case(drifted("action_note"), root=self.root / "original")
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        self.assertEqual(19, resume_protocol(execution, 19))
        repeated, app, model = self.run_case(FourAnalystLLM(), root=self.root / "resumed", resume=execution)
        self.assertEqual([], model.requests)
        self.assertEqual("SAME_V19_FLOW", repeated.latest_report["reused_calls"]["budget_origin"])
        self.assertEqual(view.latest_report["recovery"], repeated.latest_report["recovery"])
        self.assertEqual(repeated, app.read_case(repeated.case_ref))
        request = json.loads((execution / "request.json").read_bytes())
        with self.assertRaises(ValueError):
            CompletedCalls(execution, request["request"], request["sources"], "deepseek-flash", protocol_version=18)

    def test_relabelled_or_downgraded_records_are_refused(self):
        view, _, _ = self.run_case(drifted("action_note"), root=self.root / "relabel")
        record = deepcopy(view.latest_report)
        validate(record)
        for mutate in (lambda r: r.update(schema_version="finresearchops.thesis-case/v18"),
                       lambda r: r["recovery"].update(policy="thesis-stage-recovery/v3"),
                       lambda r: r["recovery"]["attempts"][0].update(reason="UNPARSEABLE")):
            broken = deepcopy(record)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate(broken)


if __name__ == "__main__":
    unittest.main()
