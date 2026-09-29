"""Protocol 19: any proven schema error gets one unchanged re-ask; v18 behaviour is frozen."""

from copy import deepcopy
import json
import unittest

from test_thesis_format import forward_revision, revision, saved
from test_thesis_recovery_v3 import BASE, call
from finauditgate.adapters.thesis_format import format_failure, normalize_format
from finauditgate.adapters.thesis_recovery import (
    POLICY_V3, POLICY_V4, RecoveryState, failure_reason, policy_for, validate_recoveries, validate_state,
)
from finauditgate.application.research_delivery import INSTRUCTION, contract_for, final_instruction


def analyst(**changes):
    value = {"analysis": "SYNTHETIC analysis", "observations": [{"statement": "SYNTHETIC", "evidence_refs": ["S01"]}],
             "coverage": "partial", "limits": ["SYNTHETIC limit"], "evidence_refs": ["S01"]}
    value.update(changes)
    return value


class FormatFailureV19Test(unittest.TestCase):
    def test_other_schema_errors_are_asked_again_only_from_protocol_19(self):
        missing = analyst()
        del missing["evidence_refs"]
        noted = revision()
        noted["updates"][0]["action_note"] = "SYNTHETIC 过程说明"
        long = analyst(observations=[{"statement": "SYNTHETIC", "evidence_refs": ["S01"]}] * 13)
        mixed = revision()
        del mixed["updates"][0]["reason"]
        mixed["updates"][0]["extra"] = "SYNTHETIC"
        rating = {"plan": {"recommendation": "Strong Buy", "rationale": "SYNTHETIC", "strategic_actions": "SYNTHETIC"},
                  "assessments": [], "valuation_basis_and_gaps": "SYNTHETIC"}
        cases = (("AnalystReport", missing), ("RevisionBrief", noted), ("AnalystReport", long),
                 ("RevisionBrief", mixed), ("ResearchEvaluation", rating))
        expected = ([["missing", "evidence_refs"]], [["extra", "updates", 0, "action_note"]], [["too_long", "observations"]],
                    [["missing", "updates", 0, "reason"], ["extra", "updates", 0, "extra"]], [["enum", "plan", "recommendation"], ["too_short", "assessments"]])
        for (kind, value), errors in zip(cases, expected):
            with self.subTest(kind=kind, value=str(value)[:60]):
                answer = saved(json.dumps(value, ensure_ascii=False), kind=kind)
                self.assertEqual(("SCHEMA_INVALID", errors), format_failure(answer, kind, 19))
                self.assertEqual((None, []), format_failure(answer, kind, 18))

    def test_narrower_repairs_and_non_schema_failures_keep_their_class(self):
        drifted = saved(json.dumps(forward_revision("accounting_correction")), kind="ForwardRevision")
        self.assertEqual("ENUM_INVALID", format_failure(drifted, "ForwardRevision", 19)[0])
        value = revision()
        del value["updates"][0]["reason"]
        self.assertEqual("MISSING_REASON", format_failure(saved(json.dumps(value)), "RevisionBrief", 19)[0])
        self.assertEqual(("UNPARSEABLE", []), format_failure(saved("{x"), "RevisionBrief", 19))
        self.assertEqual((None, []), format_failure(saved(json.dumps(revision())), "RevisionBrief", 19))
        self.assertEqual((None, []), format_failure({**saved("{"), "error_type": "APIError"}, "RevisionBrief", 19))
        truncated = saved('{"updates": [')
        truncated["output"][0]["finish_reason"] = "length"
        self.assertEqual(("LENGTH", []), failure_reason(truncated, kind="RevisionBrief", protocol_version=19))
        self.assertEqual(("EMPTY_RESPONSE", []), failure_reason(saved(" "), kind="RevisionBrief", protocol_version=19))

    def test_blank_notes_are_dropped_only_from_protocol_19(self):
        raw = {"claims": [{"statement": "S", "claims_note": ""}, {"statement": "T", "claims_note": " \n"},
                          {"statement": "U", "claims_note": "SYNTHETIC 有内容的说明"}]}
        before = deepcopy(raw)
        self.assertEqual({"claims": [{"statement": "S"}, {"statement": "T"},
                                     {"statement": "U", "claims_note": "SYNTHETIC 有内容的说明"}]},
                         normalize_format(raw, "InitialBrief", 19))
        self.assertEqual(raw, normalize_format(raw, "InitialBrief", 18))
        self.assertEqual(before, raw)


class RecoveryPolicyV4Test(unittest.TestCase):
    def test_policy_follows_protocol_and_admits_schema_invalid_only_in_v4(self):
        self.assertEqual((POLICY_V3, POLICY_V4), (policy_for(18), policy_for(19)))
        state = RecoveryState(policy=POLICY_V4)
        state.reserve("Trader", "ExecutionReview", {"run_id": "a"}, "SCHEMA_INVALID", [["extra", "proposal", "action_note"]], "high")
        validate_state(state.snapshot())
        for mutate in (lambda row: row.update(schema_errors=[]), lambda row: row.pop("schema_errors"),
                       lambda row: row.update(reason="UNPARSEABLE")):
            broken = state.snapshot()
            mutate(broken["attempts"][0])
            with self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
                validate_state(broken)
        v3 = RecoveryState(policy=POLICY_V3).snapshot()
        v3["attempts"] = deepcopy(state.snapshot()["attempts"])
        with self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
            validate_state(v3)
        chained = RecoveryState(policy=POLICY_V4)
        chained.reserve("Trader", "ExecutionReview", {"run_id": "a"}, "CONNECTION", [], "high")["retry_run_id"] = "b"
        self.assertTrue(chained.may_reserve("Trader", "ExecutionReview", {"run_id": "b"}, "SCHEMA_INVALID"))
        once = RecoveryState(policy=POLICY_V4)
        once.reserve("Trader", "ExecutionReview", {"run_id": "a"}, "SCHEMA_INVALID", [["extra", "proposal", "x"]], "high")["retry_run_id"] = "b"
        self.assertFalse(once.may_reserve("Trader", "ExecutionReview", {"run_id": "b"}, "SCHEMA_INVALID"))

    def test_schema_invalid_retry_is_bound_to_the_unchanged_prompt(self):
        value = revision()
        value["updates"][0]["action_note"] = "SYNTHETIC 过程说明"
        first = call("first", json.dumps(value, ensure_ascii=False))
        second = call("second", json.dumps(revision()), attempt=2)
        state = RecoveryState(policy=POLICY_V4)
        errors = format_failure(first, "RevisionBrief", 19)[1]
        state.reserve("Bull Researcher", "RevisionBrief", first, "SCHEMA_INVALID", errors, "max")["retry_run_id"] = "second"
        snapshot = state.snapshot()
        wrong = deepcopy(snapshot)
        wrong["attempts"][0]["schema_errors"] = [["extra", "updates", 1, "action_note"]]
        with self.assertRaisesRegex(ValueError, "FAILURE_NOT_PROVEN"):
            validate_recoveries([first, second], wrong, complete=True, protocol_version=19)
        self.assertEqual(({"first"}, {"second": first}),
                         validate_recoveries([first, second], snapshot, complete=True, protocol_version=19))
        changed = deepcopy(second)
        changed["messages"][0][0]["content"] += " SYNTHETIC changed"
        with self.assertRaisesRegex(ValueError, "INPUT_CHANGED"):
            validate_recoveries([first, changed], snapshot, complete=True, protocol_version=19)
        valid = call("first", json.dumps(revision()))
        with self.assertRaisesRegex(ValueError, "FAILURE_NOT_PROVEN"):
            validate_recoveries([valid, second], snapshot, complete=True, protocol_version=19)
        for protocol in (16, 17, 18):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
                validate_recoveries([first, second], snapshot, complete=True, protocol_version=protocol)


class RecoveryClassesV4Test(unittest.TestCase):
    def test_v4_keeps_every_v3_reason_and_chain(self):
        for reason in ("UNPARSEABLE", "ENUM_INVALID", "MISSING_REASON", "SCHEMA_INVALID"):
            with self.subTest(reason=reason):
                state = RecoveryState(policy=POLICY_V4)
                state.reserve("Bull", "RevisionBrief", {"run_id": "a"}, "CONNECTION", [], "max")["retry_run_id"] = "b"
                self.assertTrue(state.may_reserve("Bull", "RevisionBrief", {"run_id": "b"}, reason))
                paths = {"MISSING_REASON": [["updates", 0, "reason"]], "ENUM_INVALID": [["changes", 0, "replacement", "basis_type"]],
                         "SCHEMA_INVALID": [["extra", "updates", 0, "x"]]}.get(reason, [])
                state.reserve("Bull", "RevisionBrief", {"run_id": "b"}, reason, paths, "max")
                validate_state(state.snapshot())


class ProtocolSelectionTest(unittest.TestCase):
    def test_new_runs_use_21_or_16_and_resumes_keep_their_recorded_protocol(self):
        import tempfile
        from pathlib import Path
        from finauditgate.adapters.tradingagents_thesis import select_protocol
        self.assertEqual((21, 16), (select_protocol(None, True), select_protocol(None, False)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(21, select_protocol(root, True))
            for recorded, four in ((16, False), (17, True), (18, True), (19, True), (20, True), (21, True)):
                (root / "runtime-receipt.json").write_text(json.dumps(
                    {"schema_version": "finresearchops.thesis-runtime/v3", "protocol_version": recorded}))
                self.assertEqual(recorded, select_protocol(root, four))
            with self.assertRaisesRegex(ValueError, "THESIS_RESUME_PROTOCOL_MISMATCH"):
                select_protocol(root, False)
            (root / "runtime-receipt.json").write_text(json.dumps({"schema_version": "finresearchops.thesis-runtime/v1"}))
            self.assertEqual(21, select_protocol(root, True))


class InstructionV19Test(unittest.TestCase):
    def test_v19_drops_only_the_sentence_that_invited_spelled_out_numbers(self):
        removed = "均线周期、产品型号、页码和计数等其他含数字的写法会被标为待核，能用文字表达时不写数字。"
        self.assertIn(removed, final_instruction(18))
        self.assertNotIn(removed, final_instruction(19))
        self.assertEqual(final_instruction(18).replace(removed, ""), final_instruction(19))
        self.assertEqual(INSTRUCTION, final_instruction(17))
        self.assertEqual(2, contract_for({"schema_version": "finresearchops.thesis-case/v19"}))


if __name__ == "__main__":
    unittest.main()
