"""Protocol 22: content failures proven from one saved answer and its own prompt; recovery policy v6.

Standard library only. These checks decide when a stage may be asked once more;
they say nothing about the quality of any answer.
"""

from copy import deepcopy
import json
import unittest

from test_research_delivery import report
from test_research_numbers import inputs
from finauditgate.adapters.thesis_content import check, content_failure, content_repair_messages
from finauditgate.adapters.thesis_protocol import layout
from finauditgate.adapters.thesis_recovery import (
    POLICY_V6, RecoveryState, extra_call_limit, policy_for, validate_state,
)


REQUEST = {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12}
SOURCES = {"sources": [{"id": "S01", "content": "SYNTHETIC filing text."}]}
PAYLOAD = {"request": REQUEST, "source_bundle": SOURCES}
RISK = {"analysis": "x", "evidence_refs": ["S01", "S99"], "invalidation_conditions": ["y"]}
FORWARD = "forecast_end: trailing_at_valuation requires forecast_end equal to valuation_date"


def saved_call(kind, payload, answer):
    sent = layout("Portfolio Manager", "TASK", payload)
    return {"run_id": "r1", "node": "Portfolio Manager", "thesis_stage": {"kind": kind, "attempt": 1, "reasoning_effort": "high"},
            "messages": [[{"type": "system", "content": sent[0]["content"]}, {"type": "human", "content": sent[1]["content"]}]],
            "output": [{"id": "o1", "content": json.dumps(answer, ensure_ascii=False), "tool_calls": [], "finish_reason": "stop"}]}


class ContentCheckTest(unittest.TestCase):
    def test_a_forward_draft_failing_a_precalculation_check_is_refused(self):
        draft, _ = inputs()
        self.assertEqual([], check("UnderwritingDraft", draft, PAYLOAD))
        cases = {"forecast_end": ("2027-06-30", "trailing_at_valuation requires forecast_end"),
                 "valuation_date": ("2027-06-30", "THESIS_FORWARD_HORIZON_MISMATCH"),
                 "market_price_date": ("2027-01-05", "THESIS_FORWARD_FUTURE_MARKET_PRICE")}
        for field, (value, expected) in cases.items():
            broken = deepcopy(draft)
            broken[field] = value
            with self.subTest(field=field):
                [found] = check("UnderwritingDraft", broken, PAYLOAD)
                self.assertEqual("FORWARD_INCONSISTENT", found["code"])
                self.assertIn(expected, found["detail"])

    def test_the_three_stage_checks_are_independent_and_the_calculator_error_follows(self):
        draft, _ = inputs()
        draft["scenarios"][0]["scenario_id"] = "F2"
        draft["market_price_date"] = "2027-01-05"
        draft["valuation_date"] = "2027-06-30"
        [found] = check("UnderwritingDraft", draft, PAYLOAD)
        parts = found["detail"].split("; ")
        self.assertEqual(["THESIS_FORWARD_SCENARIO_IDS_INVALID", "THESIS_FORWARD_FUTURE_MARKET_PRICE",
                          "THESIS_FORWARD_HORIZON_MISMATCH"], [part.split(":")[0] for part in parts[:3]])
        self.assertIn("got F2", parts[0])
        self.assertIn("valuation_date must be 2027-12-31", parts[2])
        self.assertEqual(4, len(parts))

    def test_every_proven_problem_of_one_answer_is_listed_in_a_fixed_order(self):
        draft, _ = inputs()
        draft["forecast_end"] = "2027-06-30"
        draft["market_price"]["evidence_refs"] = ["S99", "", "S01", "S99"]
        found = check("UnderwritingDraft", draft, PAYLOAD)
        self.assertEqual(["FORWARD_INCONSISTENT", "UNKNOWN_SOURCE_REFERENCE"], [f["code"] for f in found])
        self.assertEqual(["", "S99"], found[1]["detail"])

    def test_unknown_sources_are_checked_on_critical_stages_only(self):
        self.assertEqual([{"code": "UNKNOWN_SOURCE_REFERENCE", "detail": ["S99"]}], check("RiskBrief", RISK, PAYLOAD))
        self.assertEqual([{"code": "UNKNOWN_SOURCE_REFERENCE", "detail": [""]}],
                         check("RiskBrief", {**RISK, "evidence_refs": ["S01", ""]}, PAYLOAD))
        self.assertEqual([], check("RiskBrief", {**RISK, "evidence_refs": ["S01"]}, PAYLOAD))
        # The analysts and the trader degrade under protocol 20; a final report's citations become findings.
        for kind in ("AnalystReport", "ExecutionReview"):
            with self.subTest(kind=kind):
                self.assertEqual([], check(kind, RISK, PAYLOAD))
        final = report("收入见{{source:S01}}。")
        final["evidence_refs"] = ["S99"]
        self.assertEqual([], check("FinalResearchReport", final, PAYLOAD))

    def test_the_saved_call_alone_proves_the_failure_from_protocol_22(self):
        call = saved_call("RiskBrief", PAYLOAD, RISK)
        self.assertEqual(("CONTENT_CHECK", [{"code": "UNKNOWN_SOURCE_REFERENCE", "detail": ["S99"]}]),
                         content_failure(call, "RiskBrief", 22))
        empty = saved_call("RiskBrief", PAYLOAD, {**RISK, "evidence_refs": [""]})
        self.assertEqual(("CONTENT_CHECK", [{"code": "UNKNOWN_SOURCE_REFERENCE", "detail": [""]}]),
                         content_failure(empty, "RiskBrief", 22))
        self.assertEqual((None, []), content_failure(call, "RiskBrief", 21))
        malformed = {**call, "messages": [call["messages"][0][:1]]}
        self.assertEqual((None, []), content_failure(malformed, "RiskBrief", 22))
        self.assertEqual((None, []), content_failure(saved_call("AnalystReport", PAYLOAD, RISK), "AnalystReport", 22))
        self.assertEqual((None, []), content_failure({**call, "error_type": "APITimeoutError"}, "RiskBrief", 22))
        schema_invalid = saved_call("RiskBrief", PAYLOAD, {**RISK, "extra": 1})
        self.assertEqual((None, []), content_failure(schema_invalid, "RiskBrief", 22))
        self.assertEqual((None, []), content_failure(saved_call("RiskBrief", PAYLOAD, {**RISK, "evidence_refs": ["S01"]}),
                                                     "RiskBrief", 22))

    def test_every_problem_is_appended_after_the_unchanged_prompt(self):
        base = layout("Portfolio Manager", "TASK", PAYLOAD)
        found = [{"code": "FORWARD_INCONSISTENT", "detail": FORWARD}, {"code": "UNKNOWN_SOURCE_REFERENCE", "detail": ["", "S99"]}]
        built = content_repair_messages(base, found)
        self.assertEqual(base[0], built[0])
        self.assertTrue(built[1]["content"].startswith(base[1]["content"] + "\n【程序检查未通过】"))
        tail = built[1]["content"][len(base[1]["content"]):]
        self.assertLess(tail.index("复算前执行的检查"), tail.index("evidence_refs只能使用"))
        self.assertTrue(tail.endswith('"detail":["","S99"]}]'))
        self.assertEqual(built, content_repair_messages(base, found))


class RecoveryPolicyV6Test(unittest.TestCase):
    def test_v6_records_the_proven_checks(self):
        self.assertEqual((POLICY_V6, 3), (policy_for(22), extra_call_limit(POLICY_V6)))
        state = RecoveryState(policy=POLICY_V6)
        found = [{"code": "FORWARD_INCONSISTENT", "detail": FORWARD}]
        entry = state.reserve("Portfolio Manager", "UnderwritingDraft", {"run_id": "a"}, "CONTENT_CHECK", found, "max")
        self.assertEqual((found, [], [], [], []), (entry["check"], entry["schema_errors"], entry["enum_paths"],
                                                   entry["missing_reason_paths"], entry["number_sentences"]))
        entry["retry_run_id"] = "b"
        validate_state(state.snapshot())
        other = state.reserve("Aggressive Analyst", "RiskBrief", {"run_id": "c"}, "CONTENT_CHECK",
                              [{"code": "UNKNOWN_SOURCE_REFERENCE", "detail": [""]}], "high")
        other["retry_run_id"] = "d"
        validate_state(state.snapshot())
        self.assertIsNone(state.reserve("Bull Researcher", "InitialBrief", {"run_id": "e"}, "UNPARSEABLE", [], "high")["check"])

    def test_invalid_checks_are_refused(self):
        state = RecoveryState(policy=POLICY_V6)
        state.reserve("Portfolio Manager", "UnderwritingDraft", {"run_id": "a"}, "CONTENT_CHECK",
                      [{"code": "FORWARD_INCONSISTENT", "detail": FORWARD},
                       {"code": "UNKNOWN_SOURCE_REFERENCE", "detail": ["S99"]}], "max")["retry_run_id"] = "b"
        good = state.snapshot()
        validate_state(good)
        forward, unknown = good["attempts"][0]["check"]
        for check_value in (None, [], forward, [unknown, forward], [forward, forward], [{**forward, "code": "OTHER"}],
                            [{**unknown, "detail": []}], [{**unknown, "detail": [1]}], [{**forward, "detail": ["x"]}],
                            [{**forward, "detail": ""}], [{**forward, "extra": 1}]):
            broken = deepcopy(good)
            broken["attempts"][0]["check"] = check_value
            with self.subTest(check=check_value), self.assertRaises(ValueError):
                validate_state(broken)
        for mutate in (lambda row: row.update(reason="UNPARSEABLE"), lambda row: row.pop("check")):
            broken = deepcopy(good)
            mutate(broken["attempts"][0])
            with self.subTest(row=broken["attempts"][0]), self.assertRaises(ValueError):
                validate_state(broken)


if __name__ == "__main__":
    unittest.main()
