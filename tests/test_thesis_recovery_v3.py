"""Policy v3: a retry is granted only for a failure its saved call proves again."""

from copy import deepcopy
import json
import unittest

from test_thesis_format import forward_revision, revision
from finauditgate.adapters.thesis_format import enum_repair_messages
from finauditgate.adapters.thesis_recovery import (
    POLICY, POLICY_V3, RecoveryState, missing_reason_paths, repair_messages, validate_recoveries, validate_state,
)
from finauditgate.adapters.thesis_responses import response_candidate


BASE = [{"role": "system", "content": "SYNTHETIC fixed instructions"}, {"role": "user", "content": '{"node":"SYNTHETIC"}'}]
LABEL = [["changes", 0, "replacement", "basis_type"]]


def call(identity, content, *, kind="RevisionBrief", node="Bull Researcher", prompt=BASE, attempt=1, effort="max", **extra):
    return {"run_id": identity, "node": node,
            "messages": [[{"type": "human" if m["role"] == "user" else m["role"], "content": m["content"]} for m in prompt]],
            "thesis_stage": {"kind": kind, "attempt": attempt, "reasoning_effort": effort},
            "output": [] if content is None else [{"id": "response-" + identity, "content": content, "tool_calls": [],
                                                   "finish_reason": "stop"}], **extra}


def retry_state(failed, reason, paths, retry_id, *, kind="RevisionBrief", node="Bull Researcher"):
    state = RecoveryState(policy=POLICY_V3)
    entry = state.reserve(node, kind, failed, reason, paths, "max")
    entry["retry_run_id"] = retry_id
    return state.snapshot()


class RecoveryStateV3Test(unittest.TestCase):
    def test_policy_is_fixed_by_the_protocol(self):
        v2, v3 = RecoveryState().snapshot(), RecoveryState(policy=POLICY_V3).snapshot()
        self.assertEqual(POLICY, v2["policy"])
        with self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
            RecoveryState(v2, policy=POLICY_V3)
        with self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
            RecoveryState(v3)
        failed = call("a", "{")
        for protocol, state in ((18, v2), (17, v3), (16, v3)):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
                validate_recoveries([failed], state, protocol_version=protocol)

    def test_rows_keep_each_path_list_with_its_own_reason(self):
        state = RecoveryState(policy=POLICY_V3)
        entry = state.reserve("Portfolio Manager", "ForwardRevision", {"run_id": "a"}, "ENUM_INVALID", LABEL, "max")
        self.assertEqual(([], LABEL), (entry["missing_reason_paths"], entry["enum_paths"]))
        for mutate in (lambda row: row.update(missing_reason_paths=LABEL), lambda row: row.pop("enum_paths"),
                       lambda row: row.update(reason="SCHEMA_INVALID"), lambda row: row.update(enum_paths=None)):
            broken = state.snapshot()
            mutate(broken["attempts"][0])
            with self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
                validate_state(broken)
        legacy = RecoveryState()
        legacy.reserve("Bull", "RevisionBrief", {"run_id": "a"}, "EMPTY_RESPONSE", [], "max")
        for mutate in (lambda row: row.update(reason="UNPARSEABLE"), lambda row: row.update(enum_paths=[])):
            broken = legacy.snapshot()
            mutate(broken["attempts"][0])
            with self.assertRaisesRegex(ValueError, "RECOVERY_STATE_INVALID"):
                validate_state(broken)

    def test_one_format_retry_per_stage_after_at_most_one_transport_retry(self):
        for reason in ("UNPARSEABLE", "ENUM_INVALID", "MISSING_REASON"):
            state = RecoveryState(policy=POLICY_V3)
            state.reserve("Bull", "RevisionBrief", {"run_id": "a"}, "CONNECTION", [], "max")["retry_run_id"] = "b"
            self.assertTrue(state.may_reserve("Bull", "RevisionBrief", {"run_id": "b"}, reason))
            self.assertFalse(state.may_reserve("Bull", "RevisionBrief", {"run_id": "other"}, reason))
            legacy = RecoveryState()
            legacy.reserve("Bull", "RevisionBrief", {"run_id": "a"}, "CONNECTION", [], "max")["retry_run_id"] = "b"
            self.assertEqual(reason == "MISSING_REASON", legacy.may_reserve("Bull", "RevisionBrief", {"run_id": "b"}, reason))
        for first in ("UNPARSEABLE", "ENUM_INVALID", "LENGTH", "EMPTY_RESPONSE"):
            state = RecoveryState(policy=POLICY_V3)
            state.reserve("Bull", "RevisionBrief", {"run_id": "a"}, first, [], "max")["retry_run_id"] = "b"
            for second in ("UNPARSEABLE", "ENUM_INVALID", "MISSING_REASON", "CONNECTION"):
                self.assertFalse(state.may_reserve("Bull", "RevisionBrief", {"run_id": "b"}, second))
        state = RecoveryState(policy=POLICY_V3)
        state.reserve("Bull", "RevisionBrief", {"run_id": "a"}, "UNPARSEABLE", [], "max")
        state.reserve("Bear", "RevisionBrief", {"run_id": "b"}, "UNPARSEABLE", [], "max")
        with self.assertRaisesRegex(ValueError, "EXHAUSTED"):
            state.reserve("Trader", "ExecutionReview", {"run_id": "c"}, "UNPARSEABLE", [], "max")


class ValidateRecoveriesV3Test(unittest.TestCase):
    def test_unparseable_answer_is_asked_again_with_the_unchanged_prompt(self):
        text = json.dumps(revision())
        first = call("first", text.replace("SYNTHETIC reason", 'SYNTHETIC "quoted" reason'))
        second = call("second", text, attempt=2)
        state = retry_state(first, "UNPARSEABLE", [], "second")
        excluded, dependencies = validate_recoveries([first, second], state, complete=True, protocol_version=18)
        self.assertEqual({"first"}, excluded)
        self.assertEqual(first, dependencies["second"])
        changed = deepcopy(second)
        changed["messages"][0][0]["content"] += " 请换一种答案"
        with self.assertRaisesRegex(ValueError, "INPUT_CHANGED"):
            validate_recoveries([first, changed], state, complete=True, protocol_version=18)
        parseable = call("first", text)
        with self.assertRaisesRegex(ValueError, "FAILURE_NOT_PROVEN"):
            validate_recoveries([parseable, second], state, complete=True, protocol_version=18)
        # A second unparseable answer halts the stage; its paid call stays in the runtime receipt.
        failed_again = call("second", "{", attempt=2)
        halted = {**deepcopy(state), "halted": {"node": "Bull Researcher", "kind": "RevisionBrief", "reason": "JSONDecodeError"}}
        validate_recoveries([first, failed_again], halted, protocol_version=18)
        with self.assertRaisesRegex(ValueError, "HALTED_CASE"):
            validate_recoveries([first, failed_again], halted, complete=True, protocol_version=18)

    def test_transport_then_unparseable_is_one_bounded_chain(self):
        text = json.dumps(revision())
        lost = call("lost", None, error_type="APITimeoutError")
        broken = call("broken", "{", attempt=2)
        fixed = call("fixed", text, attempt=3)
        state = RecoveryState(policy=POLICY_V3)
        state.reserve("Bull Researcher", "RevisionBrief", lost, "READ_TIMEOUT", [], "max")["retry_run_id"] = "broken"
        state.reserve("Bull Researcher", "RevisionBrief", broken, "UNPARSEABLE", [], "max")["retry_run_id"] = "fixed"
        excluded, dependencies = validate_recoveries([lost, broken, fixed], state.snapshot(), complete=True, protocol_version=18)
        self.assertEqual({"lost", "broken"}, excluded)
        self.assertEqual(broken, dependencies["fixed"])

    def test_label_repair_binds_its_prompt_and_frozen_content(self):
        kind, node = "ForwardRevision", "Portfolio Manager"
        before = forward_revision("accounting_correction")
        first = call("first", json.dumps(before), kind=kind, node=node)
        prompt = enum_repair_messages(BASE, kind, response_candidate(first["output"], kind, protocol_version=18), LABEL)
        second = call("second", json.dumps(forward_revision("reported")), kind=kind, node=node, prompt=prompt, attempt=2)
        state = retry_state(first, "ENUM_INVALID", LABEL, "second", kind=kind, node=node)
        self.assertEqual(({"first"}, {"second": first}),
                         validate_recoveries([first, second], state, complete=True, protocol_version=18))
        unrepaired = call("second", json.dumps(forward_revision("reported")), kind=kind, node=node, attempt=2)
        with self.assertRaisesRegex(ValueError, "INPUT_CHANGED"):
            validate_recoveries([first, unrepaired], state, complete=True, protocol_version=18)
        moved = forward_revision("reported")
        moved["changes"][0]["replacement"]["value"] = 80.0
        tampered = deepcopy(second)
        tampered["output"][0]["content"] = json.dumps(moved)
        with self.assertRaisesRegex(ValueError, "CHANGED_EXISTING_CONTENT"):
            validate_recoveries([first, tampered], state, complete=True, protocol_version=18)
        excluded, _ = validate_recoveries([first, tampered], state, protocol_version=18)
        self.assertEqual({"first", "second"}, excluded)
        wrong = deepcopy(state)
        wrong["attempts"][0]["enum_paths"] = [["changes", 0, "correction_basis"]]
        with self.assertRaisesRegex(ValueError, "FAILURE_NOT_PROVEN"):
            validate_recoveries([first, second], wrong, complete=True, protocol_version=18)

    def test_missing_reason_is_reproven_by_the_standard_library(self):
        value = revision()
        del value["updates"][0]["reason"]
        first = call("first", json.dumps(value))
        paths = missing_reason_paths(value, "RevisionBrief")
        second = call("second", json.dumps(revision()), prompt=repair_messages(BASE, value, paths), attempt=2)
        state = retry_state(first, "MISSING_REASON", paths, "second")
        self.assertEqual({"first"}, validate_recoveries([first, second], state, complete=True, protocol_version=18)[0])
        value["updates"][0]["extra"] = "SYNTHETIC"
        mixed = call("first", json.dumps(value))
        with self.assertRaisesRegex(ValueError, "FAILURE_NOT_PROVEN"):
            validate_recoveries([mixed, second], state, complete=True, protocol_version=18)


if __name__ == "__main__":
    unittest.main()
