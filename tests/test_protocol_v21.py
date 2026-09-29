"""Protocol 21: the same words in a cache-friendly order, and reasoning effort by stage.

Standard library only. These checks establish what every stage sends and how a
saved prompt is read back; they say nothing about the quality of any answer.
"""

import json
import unittest

from finauditgate.adapters.thesis_format import enum_repair_messages
from finauditgate.adapters.thesis_invocation import EFFORT_V21
from finauditgate.adapters.thesis_protocol import (
    SHARED_HEAD, STAGE_HEAD, SYSTEM, SYSTEM_V21, TASK_HEAD, layout, messages, payload_of,
)
from finauditgate.adapters.thesis_recovery import POLICY_V5, RecoveryState, length_retry_effort, repair_messages
from finauditgate.adapters.thesis_repair import repair_messages as number_repair_messages
from finauditgate.core.artifacts import canonical_json_bytes


SHARED = {"request": {"symbol": "AURORA", "as_of": "2026-03-02"},
          "source_bundle": {"sources": [{"id": "S01", "content": "SYNTHETIC filing text.\n\n【本阶段输入】\nnot a separator"}]}}


class LayoutTest(unittest.TestCase):
    def test_payload_reads_back_in_both_layouts(self):
        payload = {**SHARED, "analyst_reports": {"News Analyst": {"x": 1}}, "updated_claims": [{"id": "B1"}]}
        new = layout("Research Manager", "TASK", payload)
        old = messages("Research Manager", "TASK", payload)
        self.assertEqual({"node": "Research Manager", **payload}, payload_of(new[1]["content"], 21))
        self.assertEqual(payload_of(old[1]["content"], 20), payload_of(new[1]["content"], 21))
        self.assertTrue(new[1]["content"].endswith(TASK_HEAD + "TASK"))

    def test_each_protocol_reads_only_the_layout_it_sent(self):
        payload = {**SHARED, "updated_claims": [{"id": "B1"}]}
        new = layout("Research Manager", "TASK", payload)[1]["content"]
        old = messages("Research Manager", "TASK", payload)[1]["content"]
        shared_end = new.index(STAGE_HEAD)
        for broken in (old, new.replace('"B1"', '"B1" ', 1), new[:shared_end] + STAGE_HEAD + '{"node":"x","request":{}}' + TASK_HEAD + "T",
                       new.replace(SHARED_HEAD, "", 1)):
            with self.subTest(broken=broken[:40]), self.assertRaises(ValueError):
                payload_of(broken, 21)
        with self.assertRaises(ValueError):
            payload_of(new, 20)

    def test_every_stage_shares_one_system_message_and_the_shared_sources_first(self):
        first = layout("Bull Researcher", "TASK A", {**SHARED, "own_initial": {"claims": []}})
        second = layout("Portfolio Manager", "TASK B", {**SHARED, "portfolio_context": {}})
        self.assertEqual(first[0], second[0])
        self.assertEqual(SYSTEM_V21, first[0]["content"])
        self.assertTrue(SYSTEM_V21.startswith(SYSTEM))
        head = SHARED_HEAD + canonical_json_bytes(SHARED).decode() + STAGE_HEAD
        self.assertTrue(first[1]["content"].startswith(head))
        self.assertTrue(second[1]["content"].startswith(head))

    def test_the_protocol_20_words_are_kept_in_the_task(self):
        payload = {**SHARED}
        old = messages("Market Analyst", "INSTRUCTION", payload)
        old[0]["content"] += "\n输出协议：SCHEMA"
        new = layout("Market Analyst", old[0]["content"][len(SYSTEM) + 1:], payload)
        self.assertTrue(new[1]["content"].endswith(TASK_HEAD + "INSTRUCTION\n输出协议：SCHEMA"))

    def test_repairs_are_appended_after_the_unchanged_prompt(self):
        base = layout("Portfolio Manager", "TASK", {**SHARED})
        candidate = {"summary": {"text": "x"}}
        for built in (repair_messages(base, candidate, [["updates", 0, "reason"]], tail=True),
                      number_repair_messages(base, [{"field": "summary", "sentence": "收入为12.5亿元。"}], tail=True)):
            self.assertEqual(base[0], built[0])
            self.assertTrue(built[1]["content"].startswith(base[1]["content"]))
            self.assertGreater(len(built[1]["content"]), len(base[1]["content"]))
        old = messages("Portfolio Manager", "TASK", {**SHARED})
        self.assertEqual(old[1], repair_messages(old, candidate, [["updates", 0, "reason"]])[1])

    def test_enum_repair_also_goes_to_the_end(self):
        value = {"changes": [{"replacement": {"basis_type": "accounting_correction"}}]}
        base = layout("Portfolio Manager", "TASK", {**SHARED})
        from unittest.mock import patch
        with patch("finauditgate.adapters.thesis_format.schema_errors",
                   return_value=[("enum", ("changes", 0, "replacement", "basis_type"), ("reported",))]):
            built = enum_repair_messages(base, "ForwardRevision", value, [["changes", 0, "replacement", "basis_type"]], tail=True)
        self.assertEqual(base[0], built[0])
        self.assertTrue(built[1]["content"].startswith(base[1]["content"]))
        self.assertIn('"previous_response"', built[1]["content"][len(base[1]["content"]):])

    def test_reasoning_effort_by_stage(self):
        self.assertEqual({"AnalystReport": "low", "InitialBrief": "high", "RevisionBrief": "high", "ResearchEvaluation": "high",
                          "ExecutionReview": "low", "RiskBrief": "high", "IndependentAssessment": "max",
                          "UnderwritingDraft": "max", "ForwardRevision": "high", "FinalResearchReport": "high"}, EFFORT_V21)

    def test_a_truncated_answer_is_asked_again_one_level_lower(self):
        self.assertEqual(["high", "low", "low", "high", "high"],
                         [length_retry_effort("max", 21), length_retry_effort("high", 21), length_retry_effort("low", 21),
                          length_retry_effort("max", 20), length_retry_effort("low", 20)])
        state = RecoveryState(policy=POLICY_V5)
        entry = state.reserve("Portfolio Manager", "FinalResearchReport", {"run_id": "a"}, "LENGTH", [], "high",
                              length_effort=length_retry_effort("high", 21))
        self.assertEqual("low", entry["retry_effort"])


if __name__ == "__main__":
    unittest.main()
