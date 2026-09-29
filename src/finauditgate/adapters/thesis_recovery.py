"""Bounded thesis-stage recovery and deterministic replay of its evidence.

This module performs no model or network I/O. A recoverable failure must be
visible in the saved call; a valid answer is never rejected for its conclusion.
"""

from copy import deepcopy
from decimal import Decimal
import json
import re

from finauditgate.core.artifacts import canonical_json_bytes


POLICY = "thesis-stage-recovery/v2"
REASONS = {"MISSING_REASON", "EMPTY_RESPONSE", "LENGTH", "READ_TIMEOUT", "CONNECTION"}
# v3 (protocol 18) adds proven format failures: an unparseable answer is asked
# again with the same prompt; an out-of-vocabulary basis label is repaired alone.
POLICY_V3 = "thesis-stage-recovery/v3"
REASONS_V3 = REASONS | {"UNPARSEABLE", "ENUM_INVALID"}
# v4 (protocol 19): any other schema error the validator proves is also asked again unchanged.
POLICY_V4 = "thesis-stage-recovery/v4"
REASONS_V4 = REASONS_V3 | {"SCHEMA_INVALID"}
# v5 (protocol 20): three extra calls per run; one NUMBER_REPAIR of the final report after
# any earlier final recovery; non-critical stages proven unusable are listed in "degraded".
POLICY_V5 = "thesis-stage-recovery/v5"
REASONS_V5 = REASONS_V4 | {"NUMBER_REPAIR"}
_FORMAT_REASONS = {POLICY: {"MISSING_REASON"}, POLICY_V3: {"MISSING_REASON", "UNPARSEABLE", "ENUM_INVALID"},
                   POLICY_V4: {"MISSING_REASON", "UNPARSEABLE", "ENUM_INVALID", "SCHEMA_INVALID"},
                   POLICY_V5: {"MISSING_REASON", "UNPARSEABLE", "ENUM_INVALID", "SCHEMA_INVALID"}}
_REASONS = {POLICY: REASONS, POLICY_V3: REASONS_V3, POLICY_V4: REASONS_V4, POLICY_V5: REASONS_V5}
_STATE_KEYS = {"policy", "max_extra_calls", "max_per_stage", "attempts", "halted", "retired_final_calls"}
FINAL = ("Portfolio Manager", "FinalResearchReport")
_ROW_KEYS = {"node", "kind", "reason", "missing_reason_paths", "failed_run_id", "retry_run_id", "retry_effort"}
_TRANSPORT_REASONS = {"CONNECTION", "READ_TIMEOUT"}
TRANSPORT = {"OpenAITimeoutError": "READ_TIMEOUT", "APITimeoutError": "READ_TIMEOUT", "ReadTimeout": "READ_TIMEOUT",
             "OpenAIConnectionError": "CONNECTION", "APIConnectionError": "CONNECTION",
             "RemoteProtocolError": "CONNECTION", "ConnectError": "CONNECTION", "ReadError": "CONNECTION"}
REASON_ROWS = {"RevisionBrief": {"updates", "counter_responses"}, "ResearchEvaluation": {"assessments"},
               "ForwardRevision": {"changes", "claim_assessments", "belief_updates"},
               "FinalResearchReport": {"scenario_assessments"}}
FORMAT_COMMENT = "schema不允许额外字段，此行仅为草稿标记，不输出"
# Research stages whose rows pair evidence_refs with prose; the final report binds evidence itself.
NOTE_KINDS = frozenset({"InitialBrief", "RevisionBrief", "RiskBrief", "IndependentAssessment",
                        "UnderwritingDraft", "ForwardRevision"})
NOTE_PROSE = ("uncertainty", "reason", "analysis")
NOTE_LABEL = "\n模型引用说明："


def _citation_notes(value):
    """Drop a null evidence_refs_note; keep a text note verbatim in the same row's prose."""
    if isinstance(value, list):
        for item in value:
            _citation_notes(item)
        return
    if not isinstance(value, dict):
        return
    if "evidence_refs_note" in value:
        note = value["evidence_refs_note"]
        target = next((key for key in NOTE_PROSE if isinstance(value.get(key), str)), None)
        if note is None:
            del value["evidence_refs_note"]
        elif isinstance(note, str) and note.strip() and target is not None:
            value[target] += NOTE_LABEL + note
            del value["evidence_refs_note"]
    for child in value.values():
        _citation_notes(child)


def close_json_tail(text):
    """Insert only the closing brackets missing at the end of otherwise complete JSON.

    Every character before the trailing run of closers is kept; that run keeps
    its closers in order and gains only the missing ones. An open string, no
    closer at all, a dangling comma, text after the value or any mismatch is
    not repaired.
    """
    tail = len(text)
    while tail and text[tail - 1] in "]} \t\r\n":
        tail -= 1
    stack, in_string, escape = [], False, False
    for char in text[:tail]:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and (not stack or stack.pop() != char):
            return None
    needed = "".join(reversed(stack))
    given = [char for char in text[tail:] if char in "]}"]
    remaining = iter(needed)
    if in_string or not given or len(given) >= len(needed) or not all(char in remaining for char in given):
        return None
    repaired = text[:tail] + needed
    try:
        json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return repaired


def normalize_role(value, kind):
    """Only observed, explicitly defined projections; retain semantic text.

    In research stages a text evidence_refs_note is appended, labelled and
    unchanged, to the same row's uncertainty, reason or analysis. Blank or
    non-text notes, and rows without such prose, stay for the schema to refuse.
    """
    value = deepcopy(value)
    if not isinstance(value, dict):
        return value
    if kind in NOTE_KINDS:
        _citation_notes(value)
    if kind == "ExecutionReview" and isinstance(value.get("proposal"), dict):
        row = value["proposal"]
        if "stop_loss_note" in row and row["stop_loss_note"] is None:
            del row["stop_loss_note"]
        if row.get("_comment") == FORMAT_COMMENT:
            del row["_comment"]
    if kind == "ResearchEvaluation" and isinstance(value.get("plan"), dict):
        row = value["plan"]
        if row.get("strategic_actions_note") == "conditional" and isinstance(row.get("strategic_actions"), str):
            row["strategic_actions"] += "\n模型补充标记：conditional"
            del row["strategic_actions_note"]
    return value


def missing_reason_paths(candidate, kind):
    paths = []
    if isinstance(candidate, dict):
        for key in sorted(REASON_ROWS.get(kind, set())):
            rows = candidate.get(key)
            if isinstance(rows, list):
                paths.extend([key, i, "reason"] for i, row in enumerate(rows)
                             if isinstance(row, dict) and "reason" not in row)
    return paths


def check_missing_repair(before, after, paths, kind):
    if not paths or paths != missing_reason_paths(before, kind):
        raise ValueError("THESIS_REPAIR_PATHS_INVALID")
    restored = deepcopy(after)
    try:
        for key, i, field in paths:
            value = restored[key][i].pop(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("THESIS_REPAIR_REASON_REQUIRED")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("THESIS_REPAIR_REASON_REQUIRED") from exc
    # All numbers, ratings, claim IDs, list order and existing text are frozen.
    if canonical_json_bytes(restored) != canonical_json_bytes(before):
        raise ValueError("THESIS_REPAIR_CHANGED_EXISTING_CONTENT")


def repair_messages(base, candidate, paths, *, tail=False):
    """tail (protocol 21): append to the last message so the original prompt stays a cacheable prefix."""
    prompt = deepcopy(base)
    prompt[-1 if tail else 0]["content"] += ("\n本次仅补全上一响应缺失的reason。返回同一完整JSON对象；"
        "所有已有字段、列表顺序、数值、评级和论点逐字不变，不重新研究或改写。"
        "不能编造来源或用占位语句填空。缺项路径及原响应：\n"
        + canonical_json_bytes({"missing_reason_paths": paths, "previous_response": candidate}).decode())
    return prompt


def call_messages(call):
    return [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]}
            for m in call["messages"][0]]


def is_length(call):
    failure = call.get("failure_response") or {}
    return ((failure.get("truncated") is True and "length" in failure.get("finish_reasons", []))
            or any(o.get("finish_reason") == "length" for o in call.get("output", [])))


def is_empty(call):
    outputs = call.get("output", [])
    return (len(outputs) == 1 and isinstance(outputs[0].get("content"), str)
            and not outputs[0]["content"].strip() and not outputs[0].get("tool_calls"))


def retained_length_outputs(call):
    """Project a complete JSON answer already returned inside the SDK exception."""
    failure = call.get("failure_response") or {}
    if not (failure.get("truncated") is True and "length" in failure.get("finish_reasons", [])):
        outputs = call.get("output") or []
        if len(outputs) != 1 or outputs[0].get("finish_reason") != "length":
            return None
        if outputs[0].get("tool_calls"):
            raise ValueError("THESIS_LENGTH_TOOL_RESPONSE_REQUIRES_REVIEW")
        content = outputs[0].get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            json.loads(content)
        except json.JSONDecodeError:
            return None
        return deepcopy(outputs)
    provider = failure.get("provider_response") or {}
    choices = provider.get("choices") or []
    if len(choices) != 1:
        return None
    message = choices[0].get("message") or {}
    if message.get("tool_calls"):
        raise ValueError("THESIS_LENGTH_TOOL_RESPONSE_REQUIRES_REVIEW")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        json.loads(content)
    except json.JSONDecodeError:
        return None
    return [{"id": "retained-length-" + call["run_id"], "type": "ai", "name": None,
             "content": content, "tool_calls": [], "finish_reason": "length", "usage": failure.get("usage")}]


def successful_call(call):
    if call.get("retained_length") is not None:
        expected = retained_length_outputs(call)
        if call["retained_length"] is not True or expected is None or call.get("output") != expected:
            raise ValueError("THESIS_RETAINED_RESPONSE_CHANGED")
        return True
    return not call.get("error_type")


def validate_stage_tag(call, kind, dependencies):
    tag = call.get("thesis_stage")
    if tag is None:
        return  # Exact-message migration may reuse published v13 rows.
    prior = dependencies.get(call.get("run_id"))
    expected_attempt = prior["thesis_stage"]["attempt"] + 1 if prior is not None else 1
    if (not isinstance(tag, dict) or set(tag) != {"kind", "attempt", "reasoning_effort"}
            or tag["kind"] != kind or type(tag["attempt"]) is not int or tag["attempt"] != expected_attempt
            or tag["reasoning_effort"] not in ("low", "high", "max", "provider_default")):
        raise ValueError("THESIS_STAGE_TAG_INVALID")


def policy_for(protocol_version):
    return (POLICY_V5 if protocol_version >= 20 else POLICY_V4 if protocol_version >= 19
            else POLICY_V3 if protocol_version >= 18 else POLICY)


def length_retry_effort(first_effort, protocol_version):
    """A truncated answer is asked again with less reasoning: high, or from protocol 21 one level down."""
    if protocol_version < 21:
        return "high"
    return {"max": "high", "high": "low"}.get(first_effort, "low")


def extra_call_limit(policy):
    return 3 if policy == POLICY_V5 else 2


def failure_reason(call, candidate=None, errors=(), kind=None, *, protocol_version=16):
    if is_length(call):
        return "LENGTH", []
    choices = (call.get("failure_response") or {}).get("provider_response", {}).get("choices", [])
    captured_output = any((c.get("message") or {}).get("content")
        or (c.get("message") or {}).get("tool_calls") or (c.get("message") or {}).get("refusal") for c in choices)
    if call.get("error_type") in TRANSPORT and not call.get("output") and not captured_output:
        return TRANSPORT[call["error_type"]], []
    if is_empty(call) and not call.get("error_type"):
        return "EMPTY_RESPONSE", []
    if protocol_version >= 18:
        # Decided from the saved call alone, exactly as the Case reader does.
        from .thesis_format import format_failure
        return format_failure(call, kind, protocol_version) if kind is not None else (None, [])
    paths = missing_reason_paths(candidate, kind)
    if (paths and errors and not call.get("error_type") and all(e.get("type") == "missing" for e in errors)
            and {tuple(e["loc"]) for e in errors} == {tuple(p) for p in paths}):
        return "MISSING_REASON", paths
    return None, []


def _chained(policy):
    """Reasons that may follow one transport retry of the same stage."""
    return _FORMAT_REASONS[policy]


class RecoveryState:
    def __init__(self, value=None, *, policy=POLICY):
        self.value = deepcopy(value) if value is not None else {
            "policy": policy, "max_extra_calls": extra_call_limit(policy), "max_per_stage": 2, "attempts": [], "halted": None,
            "retired_final_calls": [], **({"degraded": []} if policy == POLICY_V5 else {})}
        validate_state(self.value)
        if self.value["policy"] != policy:
            raise ValueError("THESIS_RECOVERY_STATE_INVALID")

    def assert_open(self, node, kind):
        halted = self.value["halted"]
        if halted is not None and (halted["node"], halted["kind"]) == (node, kind):
            raise ValueError("THESIS_STAGE_RECOVERY_EXHAUSTED")

    def may_reserve(self, node, kind, failed_call, reason):
        attempts = self.value["attempts"]
        if len(attempts) >= extra_call_limit(self.value["policy"]):
            return False
        if reason == "NUMBER_REPAIR":
            return (self.value["policy"] == POLICY_V5 and (node, kind) == FINAL
                    and not any(a["reason"] == "NUMBER_REPAIR" for a in attempts))
        prior = [a for a in attempts if (a["node"], a["kind"]) == (node, kind)]
        return (not prior or (
            len(prior) == 1 and prior[0]["reason"] in _TRANSPORT_REASONS
            and reason in _chained(self.value["policy"]) and prior[0]["retry_run_id"] == failed_call["run_id"]))

    def reserve(self, node, kind, failed_call, reason, paths, first_effort, *, length_effort="high"):
        attempts = self.value["attempts"]
        if not self.may_reserve(node, kind, failed_call, reason):
            self.halt(node, kind, "THESIS_STAGE_RECOVERY_EXHAUSTED")
            raise ValueError("THESIS_STAGE_RECOVERY_EXHAUSTED")
        entry = {"node": node, "kind": kind, "reason": reason, "missing_reason_paths": deepcopy(paths),
                 "failed_run_id": failed_call["run_id"], "retry_run_id": None,
                 "retry_effort": length_effort if reason == "LENGTH" else "high" if reason == "NUMBER_REPAIR" else first_effort}
        if self.value["policy"] != POLICY:
            entry["missing_reason_paths"] = deepcopy(paths) if reason == "MISSING_REASON" else []
            entry["enum_paths"] = deepcopy(paths) if reason == "ENUM_INVALID" else []
        if self.value["policy"] in (POLICY_V4, POLICY_V5):
            entry["schema_errors"] = deepcopy(paths) if reason == "SCHEMA_INVALID" else []
        if self.value["policy"] == POLICY_V5:
            entry["number_sentences"] = deepcopy(paths) if reason == "NUMBER_REPAIR" else []
        attempts.append(entry)
        return entry

    def degrade(self, node, kind, reason, run_ids):
        """Record a non-critical stage left out after its saved answers were proven unusable."""
        if self.value["policy"] != POLICY_V5:
            raise ValueError("THESIS_RECOVERY_STATE_INVALID")
        if self.value["halted"] is not None and (self.value["halted"]["node"], self.value["halted"]["kind"]) == (node, kind):
            self.value["halted"] = None
        self.value["degraded"].append({"node": node, "kind": kind, "reason": reason, "run_ids": list(run_ids)})
        validate_state(self.value)

    def halt(self, node, kind, reason):
        self.value["halted"] = {"node": node, "kind": kind, "reason": reason}

    def snapshot(self):
        return deepcopy(self.value)


def validate_state(state):
    if (not isinstance(state, dict) or state.get("policy") not in _REASONS
            or set(state) != (_STATE_KEYS | {"degraded"} if state["policy"] == POLICY_V5 else _STATE_KEYS)
            or type(state["max_extra_calls"]) is not int or state["max_extra_calls"] != extra_call_limit(state["policy"])
            or type(state["max_per_stage"]) is not int or state["max_per_stage"] != 2
            or not isinstance(state["attempts"], list) or len(state["attempts"]) > extra_call_limit(state["policy"])
            or not isinstance(state["retired_final_calls"], list) or len(state["retired_final_calls"]) not in (0, 2, 3)):
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    current = state["policy"] != POLICY
    seen = {}
    for row in state["attempts"]:
        keys = {POLICY: _ROW_KEYS, POLICY_V3: _ROW_KEYS | {"enum_paths"}, POLICY_V4: _ROW_KEYS | {"enum_paths", "schema_errors"},
                POLICY_V5: _ROW_KEYS | {"enum_paths", "schema_errors", "number_sentences"}}[state["policy"]]
        if (not isinstance(row, dict) or set(row) != keys
                or any(not isinstance(row[k], str) or not row[k] for k in ("node", "kind", "failed_run_id", "retry_effort"))
                or row["reason"] not in _REASONS[state["policy"]]
                or not isinstance(row["missing_reason_paths"], list)
                or (row["retry_run_id"] is not None and not isinstance(row["retry_run_id"], str))
                or (current and (not isinstance(row["enum_paths"], list)
                    or (row["reason"] != "MISSING_REASON" and row["missing_reason_paths"])
                    or (row["reason"] != "ENUM_INVALID" and row["enum_paths"])))
                or (state["policy"] in (POLICY_V4, POLICY_V5) and (not isinstance(row["schema_errors"], list)
                    or bool(row["schema_errors"]) != (row["reason"] == "SCHEMA_INVALID")))
                or (state["policy"] == POLICY_V5 and (not isinstance(row["number_sentences"], list)
                    or bool(row["number_sentences"]) != (row["reason"] == "NUMBER_REPAIR")
                    or (row["reason"] == "NUMBER_REPAIR" and ((row["node"], row["kind"]) != FINAL or row["retry_effort"] != "high"))))
                ):
            raise ValueError("THESIS_RECOVERY_STATE_INVALID")
        key = (row["node"], row["kind"])
        if row["reason"] == "NUMBER_REPAIR":
            if "NUMBER_REPAIR" in seen:
                raise ValueError("THESIS_RECOVERY_STATE_INVALID")
            seen["NUMBER_REPAIR"] = row
            continue
        if key in seen:
            previous = seen[key]
            if (previous["reason"] not in _TRANSPORT_REASONS or row["reason"] not in _chained(state["policy"])
                    or previous["retry_run_id"] is None or previous["retry_run_id"] != row["failed_run_id"]):
                raise ValueError("THESIS_RECOVERY_STATE_INVALID")
        seen[key] = row
    if "NUMBER_REPAIR" in seen and state["attempts"][-1]["reason"] != "NUMBER_REPAIR":
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    halted = state["halted"]
    if halted is not None and (not isinstance(halted, dict) or set(halted) != {"node", "kind", "reason"}
            or any(not isinstance(v, str) or not v for v in halted.values())):
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    if state["policy"] == POLICY_V5:
        _validate_degraded(state)
    retired = state["retired_final_calls"]
    if retired:
        # A sentence repair belongs to the new final report, never to the retired one.
        entries = [r for r in state["attempts"] if r["node"] == "Portfolio Manager" and r["kind"] == "FinalResearchReport"
                   and r["reason"] != "NUMBER_REPAIR"]
        ids = list(dict.fromkeys(i for row in entries for i in (row["failed_run_id"], row["retry_run_id"])))
        if (not entries or any(not isinstance(c, dict) for c in retired)
                or None in ids or [c.get("run_id") for c in retired] != ids):
            raise ValueError("THESIS_RECOVERY_RETIRED_FINAL_INVALID")


def _validate_degraded(state):
    from .thesis_degrade import DEGRADABLE
    rows, keys = state["degraded"], set()
    if not isinstance(rows, list):
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"node", "kind", "reason", "run_ids"}
                or (row["node"], row["kind"]) not in DEGRADABLE or (row["node"], row["kind"]) in keys
                or not isinstance(row["reason"], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,120}", row["reason"])
                or not isinstance(row["run_ids"], list) or not row["run_ids"]
                or any(not isinstance(i, str) or not i for i in row["run_ids"])
                or len(set(row["run_ids"])) != len(row["run_ids"])
                or (state["halted"] is not None and (state["halted"]["node"], state["halted"]["kind"]) == (row["node"], row["kind"]))):
            raise ValueError("THESIS_RECOVERY_STATE_INVALID")
        keys.add((row["node"], row["kind"]))


def _validate_number_repair(row, by_id, retired, excluded, complete, protocol_version):
    """The repair call follows the accepted final report and asks only for its refused sentences."""
    from .thesis_repair import KIND, repair_messages
    if row["failed_run_id"] not in by_id or row["failed_run_id"] in excluded:
        raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
    first_i, failed = by_id[row["failed_run_id"]]
    if (first_i < retired or failed["node"] != row["node"] or (failed.get("thesis_stage") or {}).get("kind") != row["kind"]
            or not successful_call(failed) or not failed.get("output")):
        raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
    retry_id = row["retry_run_id"]
    if retry_id is None:
        if complete:
            raise ValueError("THESIS_RECOVERY_INCOMPLETE")
        return
    if retry_id not in by_id:
        raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
    retry_i, retry = by_id[retry_id]
    if retry_i != first_i + 1 or retry["node"] != row["node"]:
        raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
    if retry.get("thesis_stage") != {"kind": KIND, "attempt": 1, "reasoning_effort": "high"}:
        raise ValueError("THESIS_RECOVERY_EFFORT_INVALID")
    if call_messages(retry) != repair_messages(call_messages(failed), row["number_sentences"], tail=protocol_version >= 21):
        raise ValueError("THESIS_RECOVERY_INPUT_CHANGED")
    if complete and (not successful_call(retry) or not retry.get("output")):
        raise ValueError("THESIS_RECOVERY_INCOMPLETE")
    excluded.add(retry_id)


def validate_recoveries(calls, state, *, complete=False, protocol_version=16):
    """Bind each recovery to the failed and retried raw calls, without Pydantic."""
    from .thesis_responses import response_candidate
    validate_state(state)
    if state["policy"] != policy_for(protocol_version):
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    if protocol_version >= 18:
        return _validate_v3(calls, state, complete, protocol_version)
    if complete and state["halted"] is not None:
        raise ValueError("THESIS_RECOVERY_HALTED_CASE")
    combined = [*state["retired_final_calls"], *calls]
    by_id = {c["run_id"]: (i, c) for i, c in enumerate(combined)}
    if len(by_id) != len(combined):
        raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
    excluded, dependencies = set(), {}
    for row in state["attempts"]:
        if row["failed_run_id"] not in by_id:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        first_i, failed = by_id[row["failed_run_id"]]
        if failed["node"] != row["node"]:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        stage = failed.get("thesis_stage")
        expected_attempt = dependencies[row["failed_run_id"]]["thesis_stage"]["attempt"] + 1 if row["failed_run_id"] in dependencies else 1
        if (not isinstance(stage, dict) or stage.get("kind") != row["kind"] or stage.get("attempt") != expected_attempt
                or row["retry_effort"] != ("high" if row["reason"] == "LENGTH" else stage.get("reasoning_effort"))):
            raise ValueError("THESIS_RECOVERY_EFFORT_INVALID")
        previous = None
        if row["reason"] == "MISSING_REASON":
            previous = response_candidate(failed["output"], row["kind"], protocol_version=16)
            if not row["missing_reason_paths"] or missing_reason_paths(previous, row["kind"]) != row["missing_reason_paths"]:
                raise ValueError("THESIS_REPAIR_PATHS_INVALID")
        else:
            actual, _ = failure_reason(failed)
            if actual != row["reason"] or row["missing_reason_paths"]:
                raise ValueError("THESIS_RECOVERY_FAILURE_NOT_PROVEN")
            if row["reason"] == "LENGTH" and retained_length_outputs(failed) is not None:
                raise ValueError("THESIS_COMPLETE_ANSWER_CANNOT_BE_RETRIED")
        excluded.add(row["failed_run_id"])
        retry_id = row["retry_run_id"]
        if retry_id is None:
            if complete:
                raise ValueError("THESIS_RECOVERY_INCOMPLETE")
            continue
        if retry_id not in by_id:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        retry_i, retry = by_id[retry_id]
        if retry_i != first_i + 1 or retry["node"] != row["node"]:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        if retry.get("thesis_stage") != {"kind": row["kind"], "attempt": expected_attempt + 1, "reasoning_effort": row["retry_effort"]}:
            raise ValueError("THESIS_RECOVERY_EFFORT_INVALID")
        expected = call_messages(failed)
        if previous is not None:
            expected = repair_messages(expected, previous, row["missing_reason_paths"])
        if call_messages(retry) != expected:
            raise ValueError("THESIS_RECOVERY_INPUT_CHANGED")
        if successful_call(retry) and retry.get("output") and previous is not None:
            try:
                after = response_candidate(retry["output"], row["kind"], protocol_version=16)
                check_missing_repair(previous, after, row["missing_reason_paths"], row["kind"])
            except (ValueError, TypeError, KeyError):
                if complete:
                    raise
                excluded.add(retry_id)
        if complete and (not successful_call(retry) or not retry.get("output")):
            raise ValueError("THESIS_RECOVERY_INCOMPLETE")
        dependencies[retry_id] = failed
    return excluded, dependencies


def _validate_v3(calls, state, complete, protocol_version):
    """Policies v3/v4: every failure is re-proven from its saved call by the live rule."""
    from .thesis_format import check_enum_repair, enum_repair_messages
    from .thesis_responses import response_candidate
    if complete and state["halted"] is not None:
        raise ValueError("THESIS_RECOVERY_HALTED_CASE")
    combined = [*state["retired_final_calls"], *calls]
    by_id = {c["run_id"]: (i, c) for i, c in enumerate(combined)}
    if len(by_id) != len(combined):
        raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
    excluded, dependencies = set(), {}
    # v5: a degraded stage keeps every saved call as evidence; none of them is used.
    degraded = {(d["node"], d["kind"]) for d in state.get("degraded", [])}
    for row in state["attempts"]:
        if row["reason"] == "NUMBER_REPAIR":
            _validate_number_repair(row, by_id, len(state["retired_final_calls"]), excluded, complete, protocol_version)
            continue
        if row["failed_run_id"] not in by_id:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        first_i, failed = by_id[row["failed_run_id"]]
        if failed["node"] != row["node"]:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        stage = failed.get("thesis_stage")
        expected_attempt = dependencies[row["failed_run_id"]]["thesis_stage"]["attempt"] + 1 if row["failed_run_id"] in dependencies else 1
        if (not isinstance(stage, dict) or stage.get("kind") != row["kind"] or stage.get("attempt") != expected_attempt
                or (row["reason"] != "LENGTH" and row["retry_effort"] != stage.get("reasoning_effort"))):
            raise ValueError("THESIS_RECOVERY_EFFORT_INVALID")
        left_out = (row["node"], row["kind"]) in degraded
        if row["reason"] == "LENGTH" and row["retry_effort"] != length_retry_effort(stage.get("reasoning_effort"), protocol_version):
            raise ValueError("THESIS_RECOVERY_EFFORT_INVALID")
        actual, paths = failure_reason(failed, kind=row["kind"], protocol_version=protocol_version)
        if actual != row["reason"] or paths != (row["missing_reason_paths"] or row["enum_paths"] or row.get("schema_errors") or []):
            raise ValueError("THESIS_RECOVERY_FAILURE_NOT_PROVEN")
        if row["reason"] == "LENGTH" and retained_length_outputs(failed) is not None:
            raise ValueError("THESIS_COMPLETE_ANSWER_CANNOT_BE_RETRIED")
        # Unparseable and other schema-invalid answers are asked again with the unchanged prompt.
        expected, previous = call_messages(failed), None
        if row["reason"] == "MISSING_REASON":
            previous = response_candidate(failed["output"], row["kind"], protocol_version=protocol_version)
            expected = repair_messages(expected, previous, paths, tail=protocol_version >= 21)
        elif row["reason"] == "ENUM_INVALID":
            previous = response_candidate(failed["output"], row["kind"], protocol_version=protocol_version)
            expected = enum_repair_messages(expected, row["kind"], previous, paths, tail=protocol_version >= 21)
        excluded.add(row["failed_run_id"])
        retry_id = row["retry_run_id"]
        if retry_id is None:
            if complete or left_out:
                raise ValueError("THESIS_RECOVERY_INCOMPLETE")
            continue
        if retry_id not in by_id:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        retry_i, retry = by_id[retry_id]
        if retry_i != first_i + 1 or retry["node"] != row["node"]:
            raise ValueError("THESIS_RECOVERY_CALL_BINDING_INVALID")
        if retry.get("thesis_stage") != {"kind": row["kind"], "attempt": expected_attempt + 1, "reasoning_effort": row["retry_effort"]}:
            raise ValueError("THESIS_RECOVERY_EFFORT_INVALID")
        if call_messages(retry) != expected:
            raise ValueError("THESIS_RECOVERY_INPUT_CHANGED")
        if successful_call(retry) and retry.get("output") and previous is not None and not left_out:
            try:
                after = response_candidate(retry["output"], row["kind"], protocol_version=protocol_version)
                if row["reason"] == "MISSING_REASON":
                    check_missing_repair(previous, after, paths, row["kind"])
                else:
                    check_enum_repair(previous, after, paths, row["kind"])
            except (ValueError, TypeError, KeyError):
                if complete:
                    raise
                excluded.add(retry_id)
        if complete and not left_out and (not successful_call(retry) or not retry.get("output")):
            raise ValueError("THESIS_RECOVERY_INCOMPLETE")
        dependencies[retry_id] = failed
    for entry in state.get("degraded", []):
        ids = [c["run_id"] for c in calls if c["node"] == entry["node"]
               and (c.get("thesis_stage") or {}).get("kind") == entry["kind"]]
        if ids != entry["run_ids"]:
            raise ValueError("THESIS_DEGRADED_STAGE_INVALID")
        excluded.update(ids)
    return excluded, dependencies


def validate_budget_reservations(checkpoint, calls, state, prior_reuse=None):
    """Check an independent reserve lower bound from the saved request bodies."""
    if checkpoint is None:
        return
    receipt, limits = checkpoint["receipt"], checkpoint["limits"]
    rate_in, rate_out = Decimal(receipt["input_per_million_cny"]), Decimal(receipt["output_per_million_cny"])
    maximum = limits["max_output_tokens"]
    base = (Decimal(8192) * rate_in + Decimal(maximum) * rate_out) / Decimal(1000000)
    # v16 mode is fixed on resume. Published reused rows have no thesis_stage
    # marker and belong to the separately disclosed prior experiment budget.
    known = [c for c in [*state["retired_final_calls"], *calls] if c.get("thesis_stage") is not None]
    if type(receipt["calls"]) is not int or receipt["calls"] < len(known):
        raise ValueError("THESIS_RESUME_RESERVATION_MISMATCH")
    def reserve(call):
        size = len(canonical_json_bytes(call["messages"]))
        return (Decimal(size + 8192) * rate_in + Decimal(maximum) * rate_out) / Decimal(1000000)
    lower = sum((reserve(c) for c in known), Decimal(0)) + (receipt["calls"] - len(known)) * base
    if prior_reuse and prior_reuse.get("budget_origin") in ("SAME_V16_FLOW", "SAME_V17_FLOW", "SAME_V18_FLOW", "SAME_V19_FLOW",
                                                             "SAME_V20_FLOW", "SAME_V21_FLOW"):
        prior = prior_reuse["prior_budget"]
        previous_ids = set(prior_reuse["prior_model_run_ids"])
        new = [c for c in known if c["run_id"] not in previous_ids]
        extra = receipt["calls"] - prior["calls"]
        if extra < len(new) or receipt["usage"][:len(prior["usage"])] != prior["usage"]:
            raise ValueError("THESIS_RESUME_RESERVATION_MISMATCH")
        lower = max(lower, Decimal(prior["reserved_upper_cny"]) + sum((reserve(c) for c in new), Decimal(0))
                    + (extra - len(new)) * base)
    if Decimal(receipt["reserved_upper_cny"]) < lower:
        raise ValueError("THESIS_RESUME_RESERVATION_MISMATCH")
