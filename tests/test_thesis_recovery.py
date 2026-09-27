"""Content preservation, evidenced retry limits and cumulative budget guards."""

from copy import deepcopy
import json
import unittest

from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.thesis_recovery import (
    FORMAT_COMMENT, RecoveryState, check_missing_repair, close_json_tail, failure_reason, missing_reason_paths,
    normalize_role, repair_messages, validate_recoveries,
)
from finauditgate.adapters.thesis_responses import response_candidate


def saved_call(identity, value, *, prompt=None, attempt=1):
    prompt = prompt or [{"role": "system", "content": "SYNTHETIC fixed instructions"},
                        {"role": "user", "content": '{"node":"Bull Researcher"}'}]
    return {"run_id": identity, "node": "Bull Researcher",
        "messages": [[{"type": "human" if m["role"] == "user" else m["role"], "content": m["content"]} for m in prompt]],
        "thesis_stage": {"kind": "RevisionBrief", "attempt": attempt, "reasoning_effort": "max"},
        "output": [{"id": "response-" + identity, "content": json.dumps(value), "tool_calls": [], "finish_reason": "stop"}]}


class ThesisRecoveryTest(unittest.TestCase):
    def test_exact_format_exceptions_preserve_raw_and_semantic_note(self):
        original = {"proposal": {"action": "Hold", "stop_loss_note": None, "_comment": FORMAT_COMMENT}}
        before = deepcopy(original)
        self.assertEqual({"proposal": {"action": "Hold"}}, normalize_role(original, "ExecutionReview"))
        self.assertEqual(before, original)
        semantic = {"plan": {"strategic_actions": "Wait for evidence.", "strategic_actions_note": "conditional"}}
        self.assertIn("conditional", normalize_role(semantic, "ResearchEvaluation")["plan"]["strategic_actions"])
        for extra in ("EPS should be 999", {"value": 999}):
            raw = {"proposal": {"_comment": extra, "stop_loss_note": extra}}
            self.assertEqual(raw, normalize_role(raw, "ExecutionReview"))
        raw = {"claims": [{"evidence_refs_note": None}, {"evidence_refs_note": "material source conflict"}]}
        self.assertEqual({"claims": [{}, {"evidence_refs_note": "material source conflict"}]}, normalize_role(raw, "InitialBrief"))

    def test_json_tail_gains_only_missing_closers(self):
        self.assertEqual('{"a":["x","y"]}', close_json_tail('{"a":["x","y"}'))
        self.assertEqual('{"a":{"b":["x"]}}', close_json_tail('{"a":{"b":["x"]}'))
        self.assertEqual('{"a":["x]}"]}', close_json_tail('{"a":["x]}"}'))
        for text in ('{"a":["x","y"]}', '{"a":["x","y"', '{"a":["x",}', '{"a":"x}', '{"a":["x"}]',
                     '{"a":["x"]}}', '{"a":["x"} tail', '["x"}', '{"a":1}}', ''):
            self.assertIsNone(close_json_tail(text), text)
        output = [{"content": '{"analysis":"A","invalidation_conditions":["X"}', "tool_calls": []}]
        self.assertEqual({"analysis": "A", "invalidation_conditions": ["X"]},
                         response_candidate(output, "RiskBrief", protocol_version=17))
        with self.assertRaises(json.JSONDecodeError):
            response_candidate(output, "RiskBrief", protocol_version=13)

    def test_text_citation_note_is_kept_verbatim_in_the_same_row(self):
        note = "SYNTHETIC：本项中的程序辅助计算未经人工复核。"
        raw = {"claims": [{"statement": "S", "uncertainty": "U", "evidence_refs_note": note},
                          {"statement": "T", "uncertainty": "V", "evidence_refs_note": None}]}
        before = deepcopy(raw)
        self.assertEqual({"claims": [{"statement": "S", "uncertainty": "U\n模型引用说明：" + note},
                                     {"statement": "T", "uncertainty": "V"}]}, normalize_role(raw, "InitialBrief"))
        self.assertEqual(before, raw)
        revision = {"updates": [{"reason": "R", "evidence_refs_note": note}],
                    "counter_responses": [{"reason": "C", "evidence_refs_note": note}]}
        self.assertEqual({"updates": [{"reason": "R\n模型引用说明：" + note}],
                          "counter_responses": [{"reason": "C\n模型引用说明：" + note}]}, normalize_role(revision, "RevisionBrief"))
        risk = {"analysis": "A", "evidence_refs": ["S01"], "evidence_refs_note": note}
        self.assertEqual({"analysis": "A\n模型引用说明：" + note, "evidence_refs": ["S01"]}, normalize_role(risk, "RiskBrief"))
        nested = {"scenarios": [{"revenue": {"value": 12.5, "reason": "R", "evidence_refs": [], "evidence_refs_note": note}}]}
        self.assertEqual({"scenarios": [{"revenue": {"value": 12.5, "reason": "R\n模型引用说明：" + note, "evidence_refs": []}}]},
                         normalize_role(nested, "UnderwritingDraft"))
        beliefs = {"beliefs": [{"uncertainty": "U", "evidence_refs_note": note}]}
        self.assertEqual({"beliefs": [{"uncertainty": "U\n模型引用说明：" + note}]}, normalize_role(beliefs, "IndependentAssessment"))
        for value in ("   ", {"text": note}, 7):
            refused = {"claims": [{"uncertainty": "U", "evidence_refs_note": value}]}
            self.assertEqual(refused, normalize_role(refused, "InitialBrief"))
        for kind, other in (("InitialBrief", {"claims": [{"uncertainty": None, "evidence_refs_note": note}]}),
                            ("RevisionBrief", {"updates": [{"evidence_refs_note": note}]}),
                            ("ResearchEvaluation", {"plan": {"rationale": "P", "evidence_refs_note": note}}),
                            ("FinalResearchReport", {"summary": {"text": "T", "reason": "R", "evidence_refs_note": note}}),
                            ("AnalystReport", {"observations": [{"statement": "O", "evidence_refs_note": note}]})):
            self.assertEqual(other, normalize_role(other, kind))

    def test_missing_reason_repair_cannot_change_other_content(self):
        before = {"updates": [{"claim_id": "B1", "status": "maintain", "amount": 12}], "counter_responses": []}
        paths = missing_reason_paths(before, "RevisionBrief")
        after = deepcopy(before)
        after["updates"][0]["reason"] = "SYNTHETIC evidence supports the stated mechanism."
        check_missing_repair(before, after, paths, "RevisionBrief")
        for key, value in (("amount", 999), ("claim_id", "B2"), ("status", "withdraw")):
            invalid = deepcopy(after)
            invalid["updates"][0][key] = value
            with self.assertRaisesRegex(ValueError, "CHANGED_EXISTING_CONTENT"):
                check_missing_repair(before, invalid, paths, "RevisionBrief")
        invalid = deepcopy(after)
        invalid["updates"][0]["reason"] = ""
        with self.assertRaises(ValueError):
            check_missing_repair(before, invalid, paths, "RevisionBrief")

    def test_policy_caps_are_shared_and_survive_reconstruction(self):
        state = RecoveryState()
        state.reserve("Bull", "Initial", {"run_id": "one"}, "EMPTY_RESPONSE", [], "max")
        restored = RecoveryState(state.snapshot())
        with self.assertRaisesRegex(ValueError, "EXHAUSTED"):
            restored.reserve("Bull", "Initial", {"run_id": "two"}, "EMPTY_RESPONSE", [], "max")
        state.reserve("Bear", "Initial", {"run_id": "three"}, "CONNECTION", [], "max")
        with self.assertRaisesRegex(ValueError, "EXHAUSTED"):
            RecoveryState(state.snapshot()).reserve("Trader", "Execution", {"run_id": "four"}, "CONNECTION", [], "max")

    def test_recovery_replay_binds_failed_input_and_only_missing_fields(self):
        value = {"updates": [{"claim_id": "B1"}], "counter_responses": []}
        first = saved_call("first", value)
        paths = missing_reason_paths(value, "RevisionBrief")
        base = [{"role": "system", "content": "SYNTHETIC fixed instructions"}, {"role": "user", "content": '{"node":"Bull Researcher"}'}]
        fixed = deepcopy(value)
        fixed["updates"][0]["reason"] = "SYNTHETIC substantiated reason."
        second = saved_call("second", fixed, prompt=repair_messages(base, value, paths), attempt=2)
        state = RecoveryState()
        entry = state.reserve("Bull Researcher", "RevisionBrief", first, "MISSING_REASON", paths, "max")
        entry["retry_run_id"] = "second"
        excluded, dependencies = validate_recoveries([first, second], state.snapshot(), complete=True)
        self.assertEqual({"first"}, excluded)
        self.assertEqual(first, dependencies["second"])
        bad = deepcopy(second)
        modified = deepcopy(fixed)
        modified["updates"][0]["claim_id"] = "B2"
        bad["output"][0]["content"] = json.dumps(modified)
        with self.assertRaisesRegex(ValueError, "CHANGED_EXISTING_CONTENT"):
            validate_recoveries([first, bad], state.snapshot(), complete=True)
        bad = deepcopy(second)
        bad["messages"][0][0]["content"] += " changed instructions"
        with self.assertRaisesRegex(ValueError, "INPUT_CHANGED"):
            validate_recoveries([first, bad], state.snapshot(), complete=True)

    def test_valid_answers_cannot_be_relabelled_as_empty_or_transport_failures(self):
        first = saved_call("first", {"rating": "Sell"})
        self.assertEqual((None, []), failure_reason(first))
        masquerading = {**first, "error_type": "APITimeoutError"}
        self.assertEqual((None, []), failure_reason(masquerading))
        state = RecoveryState()
        state.reserve("Bull Researcher", "RevisionBrief", first, "EMPTY_RESPONSE", [], "max")
        with self.assertRaisesRegex(ValueError, "FAILURE_NOT_PROVEN"):
            validate_recoveries([first], state.snapshot())

    def test_budget_checkpoint_keeps_unknown_usage_and_remaining_calls(self):
        first = ModelBudget(ceiling_cny=None, max_calls=2, max_output_tokens=100)
        first.reserve("SYNTHETIC timed out request")
        checkpoint = deepcopy(first.checkpoint())
        second = ModelBudget(ceiling_cny=None, max_calls=2, max_output_tokens=100)
        second.restore(checkpoint)
        self.assertEqual(first.receipt(), second.receipt())
        self.assertIsNone(second.receipt()["uncached_price_estimate_cny"])
        second.reserve("retry")
        second.record_usage({"input_tokens": 10, "output_tokens": 10})
        self.assertGreater(second.reserved, first.reserved)
        with self.assertRaisesRegex(ValueError, "MODEL_CALL_LIMIT"):
            second.reserve("third")
        with self.assertRaisesRegex(ValueError, "MISMATCH"):
            ModelBudget(ceiling_cny=None, max_calls=3, max_output_tokens=100).restore(checkpoint)

    def test_non_length_budget_block_cannot_be_cleared(self):
        budget = ModelBudget(max_output_tokens=100)
        budget.reserve("test")
        budget.record_usage({"output_tokens": 101}, truncated=False)
        self.assertFalse(budget.release_confirmed_truncation(confirmed_length=True))
        resumed = ModelBudget(max_output_tokens=100)
        resumed.restore(budget.checkpoint())
        with self.assertRaisesRegex(ValueError, "MODEL_OUTPUT_LIMIT_VIOLATION"):
            resumed.reserve("blocked")

    def test_checkpoint_cannot_erase_unknown_request_reservation(self):
        budget = ModelBudget(ceiling_cny=None, max_output_tokens=100)
        budget.reserve("SYNTHETIC request whose response was lost")
        checkpoint = budget.checkpoint()
        checkpoint["receipt"]["reserved_upper_cny"] = "0"
        with self.assertRaisesRegex(ValueError, "MODEL_RESUME_BUDGET_MISMATCH"):
            ModelBudget(ceiling_cny=None, max_output_tokens=100).restore(checkpoint)

    def test_only_transport_then_missing_reason_can_share_one_stage(self):
        for first in ['EMPTY_RESPONSE','LENGTH','MISSING_REASON']:
            state=RecoveryState()
            a=state.reserve('Bull','RevisionBrief',{'run_id':'a'},first,[],'max');a['retry_run_id']='b'
            self.assertFalse(state.may_reserve('Bull','RevisionBrief',{'run_id':'b'},'MISSING_REASON'))
        state=RecoveryState()
        a=state.reserve('Bull','RevisionBrief',{'run_id':'a'},'CONNECTION',[],'max');a['retry_run_id']='b'
        self.assertFalse(state.may_reserve('Bull','RevisionBrief',{'run_id':'b'},'CONNECTION'))
        self.assertFalse(state.may_reserve('Bull','RevisionBrief',{'run_id':'unrelated'},'MISSING_REASON'))
        self.assertTrue(state.may_reserve('Bull','RevisionBrief',{'run_id':'b'},'MISSING_REASON'))
        state.reserve('Bull','RevisionBrief',{'run_id':'b'},'MISSING_REASON',[['updates',0,'reason']],'max')
        restored=RecoveryState(state.snapshot())
        self.assertFalse(restored.may_reserve('Other','Any',{'run_id':'c'},'CONNECTION'))
