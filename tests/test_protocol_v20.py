"""Protocol 20 helpers: sentence-level number repair, degradation proofs, recovery policy v5.

Everything here is synthetic and runs with the standard library only. These
checks decide which sentences may be rewritten and when a stage may be left
out; they say nothing about the quality of any answer.
"""

from copy import deepcopy
import json
import unittest

from test_number_contract_v2 import bundle
from test_research_delivery import report
from test_research_numbers import inputs
from finauditgate.adapters.thesis_degrade import DEGRADABLE, NOTE, case_status, placeholder, proven
from finauditgate.adapters.thesis_recovery import (
    POLICY_V4, POLICY_V5, RecoveryState, extra_call_limit, policy_for, validate_state,
)
from finauditgate.adapters.thesis_repair import (
    MAX_SENTENCES, SCHEMA, apply_replacements, final_texts, parse_replacements, refused_sentences,
    repair_messages, sentences,
)
from finauditgate.application.research_delivery import contract_for, final_instruction, normalize_report, report_context


REQUEST = {"symbol": "AURORA", "as_of": "2026-12-31", "horizon_months": 12}
VALUE = "收入为12.5亿元。"


def refused(value):
    return refused_sentences(normalize_report(value, bundle()), *inputs(), bundle(), REQUEST, changes=[], beliefs=[])


def output(value):
    return [{"id": "synthetic-output", "content": json.dumps(value, ensure_ascii=False), "tool_calls": []}]


class SentenceRepairTest(unittest.TestCase):
    def test_sentences_concatenate_back_to_the_exact_text(self):
        for text in ("", "。", "。。开头", "无句号", "一句。二句！三句？\n四句", "尾部换行。\n", "多个标点！？。", "a.b?c!d\n\n"):
            with self.subTest(text=text):
                self.assertEqual(text, "".join(sentences(text)))

    def test_only_value_position_sentences_are_listed_in_report_order(self):
        value = report("经营改善。" + VALUE + "现金仍需观察。")
        value["limitations"].append("毛利率为35%。")
        value["scenario_assessments"][0]["reason"] = "经营条件未证实。利润为3亿元，仍待核对。"
        self.assertEqual([{"field": "summary", "sentence": VALUE},
                          {"field": "scenario_assessments[0].reason", "sentence": "利润为3亿元，仍待核对。"},
                          {"field": "limitations[1]", "sentence": "毛利率为35%。"}], refused(value))

    def test_pending_labels_and_clean_reports_are_not_repairs(self):
        self.assertIsNone(refused(report("经营改善，仍需观察。")))
        self.assertIsNone(refused(report("2025年上半年的收入增长仍需核对。")))

    def test_more_than_five_or_repeated_sentences_are_not_repairable(self):
        many = "".join(f"收入为{i}亿元。" for i in range(1, MAX_SENTENCES + 2))
        self.assertIsNone(refused(report(many)))
        self.assertEqual(MAX_SENTENCES, len(refused(report(many[:many.index("收入为6")]))))
        self.assertIsNone(refused(report(VALUE + "经营改善。" + VALUE)))

    def test_replacements_change_exactly_the_refused_sentences(self):
        value = report("经营改善。" + VALUE + "现金仍需观察。")
        rows = refused(value)
        done = apply_replacements(value, rows, [{"field": "summary", "original": VALUE, "replacement": "收入变化需按来源核对。"}])
        self.assertEqual("经营改善。收入变化需按来源核对。现金仍需观察。", done["summary"]["text"])
        unchanged = deepcopy(done)
        unchanged["summary"]["text"] = value["summary"]["text"]
        self.assertEqual(value, unchanged)
        report_context(normalize_report(done, bundle()), *inputs(), bundle(), REQUEST, changes=[], beliefs=[], contract=2)
        with self.assertRaisesRegex(ValueError, "UNBOUND_RESEARCH_NUMBER"):
            report_context(normalize_report(value, bundle()), *inputs(), bundle(), REQUEST, changes=[], beliefs=[], contract=2)

    def test_line_breaks_and_surrounding_spaces_need_not_be_copied(self):
        value = report("经营改善。\n  " + VALUE + "\n现金仍需观察。")
        rows = refused(value)
        self.assertEqual([{"field": "summary", "sentence": "  " + VALUE}], rows)
        done = apply_replacements(value, rows, [{"field": "summary", "original": VALUE + "\n", "replacement": " 收入变化需核对。 "}])
        self.assertEqual("经营改善。\n  收入变化需核对。\n现金仍需观察。", done["summary"]["text"])
        self.assertIsNone(refused(report(VALUE + " " + VALUE)))

    def test_refusals_are_confirmed_in_their_context(self):
        accepted_in_context = "利润有三点需要注意！下面分述：经营改善。"
        self.assertIsNone(refused(report(accepted_in_context)))
        self.assertEqual([{"field": "summary", "sentence": VALUE}], refused(report(accepted_in_context + VALUE)))
        # A refusal no single sentence shows, or another contract error, means no repair call.
        self.assertIsNone(refused(report("利润为\n12。" + VALUE)))
        other_error = report(VALUE)
        other_error["limitations"].append("另见{{bogus:x}}。")
        self.assertIsNone(refused(other_error))

    def test_mismatched_missing_extra_or_blank_replacements_are_refused(self):
        value = report("经营改善。" + VALUE)
        rows = refused(value)
        good = {"field": "summary", "original": VALUE, "replacement": "收入变化需核对。"}
        for broken in ([], [{**good, "original": "收入为12.5亿元"}], [{**good, "field": "limitations[0]"}],
                       [good, good], [good, {**good, "original": "经营改善。"}], [{**good, "replacement": "  "}]):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                apply_replacements(value, rows, broken)

    def test_answer_must_match_the_frozen_schema_exactly(self):
        good = {"replacements": [{"field": "summary", "original": VALUE, "replacement": "收入变化需核对。"}]}
        self.assertEqual(good["replacements"], parse_replacements(output(good)))
        for broken in ({"replacements": []}, {"replacements": good["replacements"] * 6}, {**good, "note": "x"},
                       {"replacements": [{**good["replacements"][0], "reason": "x"}]},
                       {"replacements": [{**good["replacements"][0], "original": ""}]},
                       {"replacements": [{**good["replacements"][0], "replacement": 7}]}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                parse_replacements(output(broken))
        with self.assertRaises(ValueError):
            parse_replacements([{**output(good)[0], "tool_calls": [{"name": "x"}]}])
        with self.assertRaises(ValueError):
            parse_replacements(output(good) * 2)

    def test_prompt_appends_only_the_refused_sentences_and_schema(self):
        base = [{"role": "system", "content": "BASE"}, {"role": "user", "content": "{\"node\":\"Portfolio Manager\"}"}]
        rows = [{"field": "summary", "sentence": VALUE}]
        prompt = repair_messages(base, rows)
        self.assertEqual("BASE", base[0]["content"])
        self.assertEqual(base[1], prompt[1])
        self.assertTrue(prompt[0]["content"].startswith("BASE\n【本次任务变更】"))
        self.assertIn(json.dumps({"refused_sentences": rows}, ensure_ascii=False, separators=(",", ":")), prompt[0]["content"])
        self.assertIn('"title":"FinalNumberRepair"', prompt[0]["content"])
        self.assertEqual(prompt, repair_messages(base, rows))

    def test_every_contract_field_is_a_repair_target(self):
        value = report("摘要。")
        value["change_explanations"] = [{"scenario_id": "F1", "field": "revenue", "explanation": {"text": "变化。"}}]
        value["belief_explanations"] = [{"belief_id": "D1", "explanation": {"text": "信念。"}}]
        fields = [field for field, _ in final_texts(value)]
        self.assertEqual(["summary", "financial_analysis.cash_and_capital_allocation", "financial_analysis.earnings_quality",
                          "financial_analysis.operating_performance", "financial_analysis.valuation_and_price_requirements",
                          "strongest_counterevidence", "scenario_assessments[0].reason",
                          "scenario_assessments[0].what_changes_the_view", "limitations[0]",
                          "change_explanations[0].explanation", "belief_explanations[0].explanation"], fields)
        for field, text in final_texts(value):
            done = apply_replacements(value, [{"field": field, "sentence": text}],
                                      [{"field": field, "original": text, "replacement": "替换。"}])
            self.assertEqual("替换。", dict(final_texts(done))[field])

    def test_v20_keeps_the_v19_instruction_and_number_contract(self):
        self.assertEqual(final_instruction(19), final_instruction(20))
        self.assertEqual(2, contract_for({"schema_version": "finresearchops.thesis-case/v20"}))
        self.assertEqual(["replacements"], SCHEMA["required"])


class RecoveryPolicyV5Test(unittest.TestCase):
    def state(self):
        return RecoveryState(policy=POLICY_V5)

    def test_v5_allows_three_extra_calls_and_one_number_repair_of_the_final(self):
        self.assertEqual((POLICY_V5, 3, 2), (policy_for(20), extra_call_limit(POLICY_V5), extra_call_limit(POLICY_V4)))
        state = self.state()
        self.assertFalse(state.may_reserve("Trader", "ExecutionReview", {"run_id": "t"}, "NUMBER_REPAIR"))
        state.reserve("Bull Researcher", "RevisionBrief", {"run_id": "a"}, "UNPARSEABLE", [], "max")["retry_run_id"] = "b"
        state.reserve("Portfolio Manager", "FinalResearchReport", {"run_id": "c"}, "LENGTH", [], "max")["retry_run_id"] = "d"
        rows = [{"field": "summary", "sentence": VALUE}]
        entry = state.reserve("Portfolio Manager", "FinalResearchReport", {"run_id": "d"}, "NUMBER_REPAIR", rows, "max")
        self.assertEqual(("high", rows, [], [], []), (entry["retry_effort"], entry["number_sentences"], entry["schema_errors"],
                                                      entry["enum_paths"], entry["missing_reason_paths"]))
        entry["retry_run_id"] = "e"
        validate_state(state.snapshot())
        self.assertFalse(state.may_reserve("Portfolio Manager", "FinalResearchReport", {"run_id": "e"}, "NUMBER_REPAIR"))
        self.assertFalse(state.may_reserve("Neutral Analyst", "RiskBrief", {"run_id": "f"}, "UNPARSEABLE"))

    def test_invalid_v5_states_are_refused(self):
        state = self.state()
        state.reserve("Portfolio Manager", "FinalResearchReport", {"run_id": "d"}, "NUMBER_REPAIR",
                      [{"field": "summary", "sentence": VALUE}], "max")["retry_run_id"] = "e"
        good = state.snapshot()
        validate_state(good)
        later = deepcopy(good)
        later["attempts"].append({**later["attempts"][0], "reason": "UNPARSEABLE", "number_sentences": [],
                                  "node": "Bull Researcher", "kind": "RevisionBrief", "failed_run_id": "x", "retry_effort": "max"})
        for mutate in (lambda s: s["attempts"][0].update(retry_effort="max"),
                       lambda s: s["attempts"][0].update(number_sentences=[]),
                       lambda s: s["attempts"][0].update(node="Trader", kind="ExecutionReview"),
                       lambda s: s["attempts"].append(deepcopy(s["attempts"][0])),
                       lambda s: s.update(max_extra_calls=2), lambda s: s.pop("degraded"),
                       lambda s: s.update(degraded=[{"node": "Bull Researcher", "kind": "InitialBrief", "reason": "X", "run_ids": ["r"]}]),
                       lambda s: s.update(degraded=[{"node": "Trader", "kind": "ExecutionReview", "reason": "X", "run_ids": []}]),
                       lambda s: s.update(degraded=[{"node": "Trader", "kind": "ExecutionReview", "reason": "X", "run_ids": ["r"]}] * 2),
                       lambda s: s.update(degraded=[{"node": "Trader", "kind": "ExecutionReview", "reason": "X", "run_ids": ["r"]}],
                                          halted={"node": "Trader", "kind": "ExecutionReview", "reason": "X"})):
            broken = deepcopy(good)
            mutate(broken)
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                validate_state(broken)
        with self.assertRaises(ValueError):
            validate_state(later)
        v4 = RecoveryState(policy=POLICY_V4)
        self.assertFalse(v4.may_reserve("Portfolio Manager", "FinalResearchReport", {"run_id": "d"}, "NUMBER_REPAIR"))
        with self.assertRaises(ValueError):
            v4.degrade("Trader", "ExecutionReview", "X", ["r"])

    def test_degrade_records_the_stage_and_clears_its_halt(self):
        state = self.state()
        state.halt("Trader", "ExecutionReview", "THESIS_STAGE_SCHEMA_INVALID")
        state.degrade("Trader", "ExecutionReview", "THESIS_STAGE_SCHEMA_INVALID", ["r1", "r2"])
        snapshot = state.snapshot()
        self.assertIsNone(snapshot["halted"])
        self.assertEqual([{"node": "Trader", "kind": "ExecutionReview", "reason": "THESIS_STAGE_SCHEMA_INVALID",
                           "run_ids": ["r1", "r2"]}], snapshot["degraded"])
        with self.assertRaises(ValueError):
            state.degrade("Trader", "ExecutionReview", "AGAIN", ["r3"])


class DegradationProofTest(unittest.TestCase):
    ANALYST = {"analysis": "合成分析。", "observations": [{"statement": "合成观察。", "evidence_refs": ["S01"]}],
               "coverage": "partial", "limits": ["合成限制。"], "evidence_refs": ["S01"]}

    def call(self, run_id, value=None, *, node="News Analyst", kind="AnalystReport", **extra):
        return {"run_id": run_id, "node": node, "thesis_stage": {"kind": kind, "attempt": 1, "reasoning_effort": "high"},
                "output": output(value) if value is not None else [], **extra}

    def test_every_saved_answer_must_fail_its_own_checks(self):
        bad_ref = {**self.ANALYST, "evidence_refs": ["S99"]}
        extra_field = {**self.ANALYST, "note": "x"}
        calls = [self.call("a", bad_ref), self.call("b", extra_field), self.call("c", error_type="APITimeoutError"),
                 self.call("other", self.ANALYST, node="Market Analyst")]
        self.assertEqual(["a", "b", "c"], [c["run_id"] for c in proven(calls, "News Analyst", "AnalystReport", 20, {"S01"})])
        self.assertIsNone(proven([*calls, self.call("d", self.ANALYST)], "News Analyst", "AnalystReport", 20, {"S01"}))
        self.assertIsNone(proven([], "News Analyst", "AnalystReport", 20, {"S01"}))
        self.assertIsNone(proven(calls, "Bull Researcher", "InitialBrief", 20, {"S01"}))
        unavailable_with_rows = {**self.ANALYST, "coverage": "unavailable"}
        self.assertEqual(["u"], [c["run_id"] for c in proven([self.call("u", unavailable_with_rows)],
                                                           "News Analyst", "AnalystReport", 20, {"S01"})])

    def test_placeholder_is_explicit_and_status_is_partial(self):
        self.assertEqual(5, len(DEGRADABLE))
        value = placeholder("Trader", "ExecutionReview", "THESIS_STAGE_SCHEMA_INVALID")
        self.assertEqual({"degraded": True, "node": "Trader", "kind": "ExecutionReview",
                          "reason": "THESIS_STAGE_SCHEMA_INVALID", "note": NOTE}, value)
        self.assertIn("不代表资料中没有相关信息，也不代表中性观点", NOTE)
        self.assertEqual(("PARTIAL", "COMPLETED", "PARTIAL"),
                         (case_status([value], "COMPLETED"), case_status([], "COMPLETED"), case_status([], "PARTIAL")))


if __name__ == "__main__":
    unittest.main()
