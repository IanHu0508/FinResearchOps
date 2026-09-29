"""Protocol 21 through the pinned four-analyst graph, with synthetic providers only.

Every stage sends one fixed system message and starts its user message with the
same shared sources, so a provider prefix cache can serve them; retries and
repairs are appended after the unchanged prompt. Reasoning effort is set by
stage. These checks establish what is sent and saved, not answer quality.
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
import test_protocol_v20_flow as v20
from test_four_analyst_flow import FourAnalystLLM
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion
from native_support import payload_of
from finauditgate.adapters.thesis_protocol import SHARED_HEAD, STAGE_HEAD, SYSTEM_V21
from finauditgate.adapters.thesis_recovery import call_messages
from finauditgate.application.research_report import render as render_formal
from finauditgate.application.thesis_case import validate


class LengthOnceLLM(FourAnalystLLM):
    """The first answer of one stage exhausts its output on reasoning; later answers are normal."""
    target: str = ""
    seen: dict = {}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        key = payload_of(messages[-1].content).get("node", "") + ":" + (schema.__name__ if schema else "")
        self.seen[key] = self.seen.get(key, 0) + 1
        if key == self.target and self.seen[key] == 1:
            raw = ChatCompletion.model_validate({"id": "synthetic-length", "object": "chat.completion", "created": 0,
                "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "length", "message": {
                    "role": "assistant", "content": "", "reasoning_content": "SYNTHETIC exhausted reasoning"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 65536, "total_tokens": 65636,
                    "completion_tokens_details": {"reasoning_tokens": 65536}}})
            raise LengthFinishReasonError(completion=raw)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def calls_of(record, kind=None):
    return [c for c in record["model_calls"] if kind is None or (c.get("thesis_stage") or {}).get("kind") == kind]


def user(call):
    return call_messages(call)[1]["content"]


class ProtocolV21FlowTest(unittest.TestCase):
    setUp = v18.ProtocolV18FlowTest.setUp
    halted = v18.ProtocolV18FlowTest.halted

    def run_case(self, model=None, *, root=None, protocol=21, resume=None, review=False):
        return v18.ProtocolV18FlowTest.run_case(self, model, root=root, protocol=protocol, resume=resume, review=review)

    def test_new_run_is_v21_with_one_shared_prefix_and_staged_effort(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual(("finresearchops.thesis-case/v21", "thesis-stage-recovery/v5", 17),
                         (record["schema_version"], record["recovery"]["policy"], len(record["model_calls"])))
        sent = [call_messages(c) for c in record["model_calls"]]
        self.assertEqual({SYSTEM_V21}, {m[0]["content"] for m in sent})
        self.assertTrue(all(m[1]["content"].startswith(SHARED_HEAD) for m in sent))
        shared = [m[1]["content"].split(STAGE_HEAD, 1)[0] for m in sent]
        final_index = [i for i, c in enumerate(record["model_calls"]) if c["thesis_stage"]["kind"] == "FinalResearchReport"]
        self.assertEqual(1, len({s for i, s in enumerate(shared) if i not in final_index}))
        self.assertNotEqual(shared[0], shared[final_index[0]])
        efforts = {c["thesis_stage"]["kind"]: c["thesis_stage"]["reasoning_effort"] for c in record["model_calls"]}
        self.assertEqual({"AnalystReport": "low", "InitialBrief": "high", "RevisionBrief": "high", "ResearchEvaluation": "high",
                          "ExecutionReview": "low", "RiskBrief": "high", "IndependentAssessment": "max",
                          "UnderwritingDraft": "max", "ForwardRevision": "high", "FinalResearchReport": "high"}, efforts)
        self.assertEqual("high", record["final_generation"][0]["reasoning_effort"])
        self.assertIn("研究报告", render_formal(record)[0].decode())
        self.assertEqual(view, app.read_case(view.case_ref))
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
                  "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
                  "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v21';"
                  "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(self.root), view.case_ref],
            env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_retries_and_repairs_are_appended_after_the_unchanged_prompt(self):
        cases = (("unparseable", v18.FormatDriftLLM(), "UNPARSEABLE"),
                 ("enum", v18.FormatDriftLLM(broken_node="Portfolio Manager", broken_kind="ForwardRevision", drift="label"),
                  "ENUM_INVALID"),
                 ("numbers", v20.NumberDriftLLM(), "NUMBER_REPAIR"))
        for name, model, reason in cases:
            with self.subTest(name=name):
                view, app, _ = self.run_case(model, root=self.root / name)
                record = view.latest_report
                [attempt] = record["recovery"]["attempts"]
                self.assertEqual(reason, attempt["reason"])
                failed, retry = (next(c for c in record["model_calls"] if c["run_id"] == attempt[key])
                                 for key in ("failed_run_id", "retry_run_id"))
                self.assertEqual(call_messages(failed)[0], call_messages(retry)[0])
                self.assertTrue(user(retry).startswith(user(failed)))
                self.assertEqual(reason == "UNPARSEABLE", user(retry) == user(failed))
                self.assertEqual(view, app.read_case(view.case_ref))

    def test_a_truncated_answer_is_asked_again_one_level_lower(self):
        for target, first, retry in (("News Analyst:AnalystReport", "low", "low"),
                                     ("Portfolio Manager:FinalResearchReport", "high", "low")):
            with self.subTest(target=target):
                view, app, _ = self.run_case(LengthOnceLLM(target=target, seen={}), root=self.root / target.split(":")[1])
                record = view.latest_report
                [row] = record["recovery"]["attempts"]
                failed = next(c for c in record["model_calls"] if c["run_id"] == row["failed_run_id"])
                self.assertEqual(("LENGTH", first, retry), (row["reason"], failed["thesis_stage"]["reasoning_effort"], row["retry_effort"]))
                self.assertEqual(view, app.read_case(view.case_ref))
                if target.endswith("FinalResearchReport"):
                    self.assertEqual([("TRUNCATED", "high"), ("COMPLETED", "low")],
                                     [(g["status"], g["reasoning_effort"]) for g in record["final_generation"]])
                    tampered = deepcopy(record)
                    tampered["recovery"]["attempts"][0]["retry_effort"] = "high"
                    with self.assertRaises(ValueError):
                        validate(tampered)

    def test_prompts_and_efforts_the_version_never_sent_are_refused(self):
        record = self.run_case(root=self.root / "v21")[0].latest_report
        old = self.run_case(protocol=20, root=self.root / "v20")[0].latest_report
        system = deepcopy(record)
        system["model_calls"][0]["messages"][0][0]["content"] += "。"
        effort = deepcopy(record)
        effort["model_calls"][0]["thesis_stage"]["reasoning_effort"] = "max"
        relaid = deepcopy(record)
        call = relaid["model_calls"][6]
        payload = payload_of(call["messages"][0][1]["content"])
        call["messages"][0][1]["content"] = json.dumps(payload, ensure_ascii=False)
        forward = deepcopy(old)
        from finauditgate.adapters.thesis_protocol import layout
        first = forward["model_calls"][0]["messages"][0]
        node_payload = json.loads(first[1]["content"])
        rebuilt = layout(node_payload.pop("node"), "TASK", node_payload)
        first[0]["content"], first[1]["content"] = rebuilt[0]["content"], rebuilt[1]["content"]
        for broken, index in ((system, 0), (relaid, 6), (forward, 0)):
            call = broken["model_calls"][index]
            exchange = next(e for e in broken["exchanges"] if any(o.get("id") == e["response_id"] for o in call["output"]))
            exchange["messages"] = call_messages(call)
        for name, broken, code in (("system", system, "THESIS_PROMPT_LAYOUT_INVALID"), ("effort", effort, "THESIS_STAGE_EFFORT_INVALID"),
                                   ("old layout in v21", relaid, "THESIS_PROMPT_LAYOUT_INVALID"), ("v21 layout in v20", forward, "")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, code):
                validate(broken)

    def test_unbound_calls_are_checked_too(self):
        record = self.run_case(v20.DegradeLLM(invalid_source_node="News Analyst"), root=self.root / "unbound")[0].latest_report
        [row] = record["recovery"]["degraded"]
        broken = deepcopy(record)
        call = next(c for c in broken["model_calls"] if c["run_id"] == row["run_ids"][0])
        call["messages"][0][0]["content"] = call["messages"][0][0]["content"].replace("中文回答", "英文回答", 1)
        with self.assertRaisesRegex(ValueError, "THESIS_PROMPT_LAYOUT_INVALID"):
            validate(broken)

    def test_degradation_and_resume_keep_working(self):
        view, _, _ = self.run_case(v20.DegradeLLM(invalid_source_node="News Analyst"), root=self.root / "original")
        record = view.latest_report
        self.assertEqual(("PARTIAL", "News Analyst"), (record["status"], record["recovery"]["degraded"][0]["node"]))
        execution = next((self.root / "original").glob("application/thesis-executions/*"))
        repeated, app, model = self.run_case(FourAnalystLLM(), root=self.root / "resumed", resume=execution)
        self.assertEqual([], model.requests)
        self.assertEqual("SAME_V21_FLOW", repeated.latest_report["reused_calls"]["budget_origin"])
        self.assertEqual(record["model_calls"], repeated.latest_report["model_calls"])
        self.assertEqual(repeated, app.read_case(repeated.case_ref))

    def test_v20_prompts_keep_their_layout(self):
        record = self.run_case(protocol=20, root=self.root / "v20")[0].latest_report
        for call in record["model_calls"]:
            sent = call_messages(call)
            self.assertNotEqual(SYSTEM_V21, sent[0]["content"])
            self.assertEqual(call["node"], json.loads(sent[1]["content"])["node"])
        self.assertEqual("max", record["final_generation"][0]["reasoning_effort"])

    def test_a_changed_shared_source_is_refused(self):
        record = self.run_case(root=self.root / "tamper")[0].latest_report
        validate(deepcopy(record))
        broken = deepcopy(record)
        call = broken["model_calls"][5]
        message = call["messages"][0][1]
        changed = message["content"].replace("SYNTHETIC", "SYNTHETIX", 1)
        self.assertNotEqual(changed, message["content"])
        message["content"] = changed
        exchange = next(e for e in broken["exchanges"] if any(o.get("id") == e["response_id"] for o in call["output"]))
        exchange["messages"][1]["content"] = changed
        with self.assertRaises(ValueError):
            validate(broken)


if __name__ == "__main__":
    unittest.main()
