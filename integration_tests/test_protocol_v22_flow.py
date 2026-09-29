"""Protocol 22 through the pinned four-analyst graph, with synthetic providers only.

A content failure the program proves from the saved answer and its own prompt gets
one more request at the same stage, with the proven problems appended after the
unchanged prompt. These checks establish what is sent and saved, not answer quality.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx
from openai import APITimeoutError, LengthFinishReasonError
from openai.types.chat import ChatCompletion

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import test_protocol_v18_flow as v18
from native_support import payload_of
from test_four_analyst_flow import FourAnalystLLM
from finauditgate.adapters import thesis_content
from finauditgate.adapters.thesis_recovery import call_messages
from finauditgate.application import ApplicationError
from finauditgate.application.thesis_case import validate
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex

UNKNOWN = "S99_SYNTHETIC_UNKNOWN"
TARGETS = {"final_source_ids": "FinalResearchReport", "forward": "UnderwritingDraft", "forward_unknown": "UnderwritingDraft",
           "unknown_ref": "RiskBrief", "empty_ref": "RiskBrief"}
FORWARD_STOP = {"node": "Portfolio Manager", "kind": "UnderwritingDraft", "reason": "THESIS_FORWARD_INCONSISTENT"}


def first_refs(value):
    """The first evidence_refs list of an answer, depth first."""
    if isinstance(value, dict):
        if isinstance(value.get("evidence_refs"), list):
            return value["evidence_refs"]
        children = value.values()
    else:
        children = value if isinstance(value, list) else []
    return next((refs for refs in map(first_refs, children) if refs is not None), None)


def cut_off(content, reasoning=None):
    """A provider answer that stopped at the output limit, with or without complete content."""
    message = {"role": "assistant", "content": content, **({"reasoning_content": reasoning} if reasoning else {})}
    return LengthFinishReasonError(completion=ChatCompletion.model_validate({
        "id": "synthetic-length", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "length", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 65536, "total_tokens": 65636}}))


class ContentDriftLLM(FourAnalystLLM):
    """Scripted calls of one stage: actions[i] is its (i+1)-th call, later calls are normal.

    "drift" carries the content failure, "kept" carries it complete at the output limit,
    "timeout" is a transport failure and "exhausted" an empty answer at the output limit.
    """
    drift: str = ""
    actions: list = ["drift"]
    seen: dict = {}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        schema = kwargs.get("synthetic_schema")
        kind = schema.__name__ if schema else ""
        payload = payload_of(messages[-1].content)
        if kind != TARGETS.get(self.drift) or (kind == "RiskBrief" and payload["node"] != "Aggressive Analyst"):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self.seen[kind] = self.seen.get(kind, 0) + 1
        action = self.actions[self.seen[kind] - 1] if self.seen[kind] <= len(self.actions) else "normal"
        if action == "timeout":
            raise APITimeoutError(request=httpx.Request("POST", "https://synthetic.invalid"))
        if action == "exhausted":
            raise cut_off("", reasoning="SYNTHETIC exhausted reasoning")
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if action == "normal":
            return result
        value = json.loads(result.generations[0].message.content)
        if self.drift == "final_source_ids":
            first = payload["source_bundle"]["sources"][0]["id"]
            text = json.dumps(value, ensure_ascii=False)
            value = json.loads(re.sub(r"\{\{source:E\d{4}\}\}", "{{source:" + first + "}}", text))
        elif self.drift.startswith("forward"):
            value["forecast_end"] = "2026-12-31"
            if self.drift == "forward_unknown":
                first_refs(value).append(UNKNOWN)
        else:
            value["evidence_refs"] = [*value["evidence_refs"], UNKNOWN if self.drift == "unknown_ref" else ""]
        content = json.dumps(value, ensure_ascii=False)
        if action == "kept":
            raise cut_off(content)
        return self._result(content)


class ProtocolV22FlowTest(unittest.TestCase):
    setUp = v18.ProtocolV18FlowTest.setUp
    halted = v18.ProtocolV18FlowTest.halted

    def run_case(self, model=None, *, root=None, protocol=22, resume=None, review=False):
        with patch("finauditgate.adapters.thesis_invocation.sleep"):
            return v18.ProtocolV18FlowTest.run_case(self, model, root=root, protocol=protocol, resume=resume, review=review)

    def stdlib_reopen(self, root, case_ref):
        repo = Path(__file__).parents[1]
        script = ("import sys;from pathlib import Path;from finauditgate.application import FinResearchOps;"
                  "v=FinResearchOps(artifact_root=Path(sys.argv[1])).read_case(sys.argv[2]);"
                  "assert v.latest_report['schema_version']=='finresearchops.thesis-case/v22';"
                  "assert not any(n in sys.modules for n in ('pydantic','openai','langgraph','tradingagents'))")
        completed = subprocess.run([str(repo / ".venv/bin/python"), "-S", "-c", script, str(root), case_ref],
            env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_new_run_is_v22_and_the_final_task_ends_with_the_citation_rule(self):
        view, app, model = self.run_case()
        record = view.latest_report
        self.assertEqual(("finresearchops.thesis-case/v22", "thesis-stage-recovery/v6", []),
                         (record["schema_version"], record["recovery"]["policy"], record["recovery"]["attempts"]))
        final = next(c for c in record["model_calls"] if c["thesis_stage"]["kind"] == "FinalResearchReport")
        self.assertIn("最后提醒：{{source:…}}里只能写", call_messages(final)[1]["content"][-200:])
        self.assertEqual(view, app.read_case(view.case_ref))
        self.stdlib_reopen(self.root, view.case_ref)

    def test_each_proven_content_failure_is_asked_again_once_and_delivered(self):
        for drift, codes, detail in (("forward", ["FORWARD_INCONSISTENT"], None),
                                     ("unknown_ref", ["UNKNOWN_SOURCE_REFERENCE"], [UNKNOWN]),
                                     ("empty_ref", ["UNKNOWN_SOURCE_REFERENCE"], [""]),
                                     ("forward_unknown", ["FORWARD_INCONSISTENT", "UNKNOWN_SOURCE_REFERENCE"], [UNKNOWN])):
            with self.subTest(drift=drift):
                view, app, _ = self.run_case(ContentDriftLLM(drift=drift, seen={}), root=self.root / drift)
                record = view.latest_report
                [row] = record["recovery"]["attempts"]
                self.assertEqual(("CONTENT_CHECK", codes), (row["reason"], [f["code"] for f in row["check"]]))
                if detail is not None:
                    self.assertEqual(detail, row["check"][-1]["detail"])
                failed, retry = (next(c for c in record["model_calls"] if c["run_id"] == row[key])
                                 for key in ("failed_run_id", "retry_run_id"))
                self.assertEqual(call_messages(failed)[0], call_messages(retry)[0])
                self.assertTrue(call_messages(retry)[1]["content"].startswith(call_messages(failed)[1]["content"] + "\n【程序检查未通过】"))
                self.assertEqual(view, app.read_case(view.case_ref))
                tampered = deepcopy(record)
                tampered["recovery"]["attempts"][0]["check"][-1]["detail"] = ["S00"] if detail is not None else "OTHER"
                with self.assertRaises(ValueError):
                    validate(tampered)
                if len(codes) == 2:
                    dropped = deepcopy(record)
                    del dropped["recovery"]["attempts"][0]["check"][1]
                    with self.assertRaises(ValueError):
                        validate(dropped)
                    self.stdlib_reopen(self.root / drift, view.case_ref)

    def test_a_content_request_may_follow_one_transport_retry_and_resume_when_pending(self):
        view, app, _ = self.run_case(ContentDriftLLM(drift="forward", actions=["timeout", "drift"], seen={}), root=self.root / "chain")
        self.assertEqual(["READ_TIMEOUT", "CONTENT_CHECK"], [a["reason"] for a in view.latest_report["recovery"]["attempts"]])
        self.assertEqual(view, app.read_case(view.case_ref))
        original, sent = thesis_content.content_repair_messages, []

        def interrupt_once(base, found):
            sent.append(found)
            if len(sent) == 1:
                raise KeyboardInterrupt
            return original(base, found)
        with patch.object(thesis_content, "content_repair_messages", side_effect=interrupt_once), self.assertRaises(KeyboardInterrupt):
            self.run_case(ContentDriftLLM(drift="unknown_ref", seen={}), root=self.root / "pending")
        execution = next((self.root / "pending").glob("application/thesis-executions/*"))
        [pending] = json.loads((execution / "runtime-receipt.json").read_bytes())["recovery"]["attempts"]
        self.assertEqual(("CONTENT_CHECK", None), (pending["reason"], pending["retry_run_id"]))
        view, app, _ = self.run_case(ContentDriftLLM(drift="unknown_ref", actions=[], seen={}), root=self.root / "resumed", resume=execution)
        [row] = view.latest_report["recovery"]["attempts"]
        self.assertEqual((pending["failed_run_id"], pending["check"]), (row["failed_run_id"], row["check"]))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_a_recorded_problem_the_failed_answer_does_not_prove_is_refused(self):
        view, app, _ = self.run_case(ContentDriftLLM(drift="unknown_ref", seen={}), root=self.root / "forged")
        record = deepcopy(view.latest_report)
        row = record["recovery"]["attempts"][0]
        old = json.dumps(row["check"], ensure_ascii=False, separators=(",", ":"))
        row["check"][0]["detail"] = ["S00"]
        new = json.dumps(row["check"], ensure_ascii=False, separators=(",", ":"))
        retry = next(c for c in record["model_calls"] if c["run_id"] == row["retry_run_id"])
        self.assertTrue(retry["messages"][0][1]["content"].endswith(old))
        for message in retry["messages"][0]:
            message["content"] = message["content"].replace(old, new)
        for exchange in record["exchanges"]:
            for message in exchange["messages"]:
                message["content"] = message["content"].replace(old, new)
        with self.assertRaisesRegex(ValueError, "THESIS_RECOVERY_FAILURE_NOT_PROVEN"):
            validate(record)
        # A saved call without its user message is an integrity failure, not a crash of the reader.
        broken = deepcopy(view.latest_report)
        failed = next(c for c in broken["model_calls"] if c["run_id"] == broken["recovery"]["attempts"][0]["failed_run_id"])
        failed["messages"][0] = failed["messages"][0][:1]
        with self.assertRaises(ValueError):
            validate(broken)
        raw = canonical_json_bytes(broken)
        target = Path(view.report_path).parent.parent / ("case-" + sha256_hex(raw))
        target.mkdir()
        (target / "case.json").write_bytes(raw)
        with self.assertRaises(ApplicationError):
            app.read_case(target.name)

    def test_the_reader_repeats_the_forward_checks_on_the_saved_draft(self):
        view, _, _ = self.run_case(root=self.root / "ids")
        self.assertEqual(["F1", "F2"], [s["scenario_id"] for s in view.latest_report["forward_draft"]["scenarios"]])
        text = canonical_json_bytes(view.latest_report).decode()
        swapped = json.loads(text.replace("F1", "F_SWAP").replace("F2", "F1").replace("F_SWAP", "F2"))
        with self.assertRaisesRegex(ValueError, "THESIS_FORWARD_INCONSISTENT"):
            validate(swapped)

    def test_a_miscited_final_is_delivered_as_in_v21_without_another_request(self):
        view, app, model = self.run_case(ContentDriftLLM(drift="final_source_ids", seen={}), root=self.root / "final")
        record = view.latest_report
        self.assertEqual(("PARTIAL", [], 17), (record["status"], record["recovery"]["attempts"], len(record["model_calls"])))
        self.assertTrue([f for f in record["evidence_check"]["findings"] if f["reason"] == "THESIS_UNKNOWN_EVIDENCE_BLOCK"])
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_an_analyst_citing_an_unknown_source_is_left_out_not_asked_again(self):
        view, app, _ = self.run_case(ContentDriftLLM(invalid_source_node="News Analyst", seen={}), root=self.root / "analyst")
        record = view.latest_report
        [row] = record["recovery"]["degraded"]
        self.assertEqual(("News Analyst", "THESIS_UNKNOWN_SOURCE_REFERENCE", []),
                         (row["node"], row["reason"], record["recovery"]["attempts"]))
        self.assertEqual(view, app.read_case(view.case_ref))

    def test_a_forward_draft_stops_when_no_content_request_is_left(self):
        for name, actions, reasons in (("still", ["drift", "drift"], ["CONTENT_CHECK"]), ("kept", ["kept"], []),
                                       ("after-length", ["exhausted", "drift"], ["LENGTH"])):
            with self.subTest(name=name):
                receipt = self.halted(ContentDriftLLM(drift="forward", actions=actions, seen={}), self.root / name, protocol=22)
                self.assertEqual((FORWARD_STOP, reasons), (receipt["recovery"]["halted"],
                                                           [a["reason"] for a in receipt["recovery"]["attempts"]]))

    def test_v21_keeps_its_behaviour(self):
        record = self.run_case(ContentDriftLLM(drift="final_source_ids", seen={}), protocol=21, root=self.root / "v21")[0].latest_report
        self.assertEqual([], record["recovery"]["attempts"])
        self.assertTrue([f for f in record["evidence_check"]["findings"] if f["reason"] == "THESIS_UNKNOWN_EVIDENCE_BLOCK"])
        final = next(c for c in record["model_calls"] if c["thesis_stage"]["kind"] == "FinalResearchReport")
        self.assertNotIn("最后提醒", call_messages(final)[1]["content"])
        for drift, reason in (("forward", "ValueError"), ("empty_ref", "THESIS_UNKNOWN_SOURCE_REFERENCE")):
            with self.subTest(drift=drift):
                receipt = self.halted(ContentDriftLLM(drift=drift, seen={}), self.root / f"v21-{drift}", protocol=21)
                self.assertEqual((reason, []), (receipt["recovery"]["halted"]["reason"], receipt["recovery"]["attempts"]))


if __name__ == "__main__":
    unittest.main()
