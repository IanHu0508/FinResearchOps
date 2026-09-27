"""Bounded thesis-stage recovery and deterministic replay of its evidence.

This module performs no model or network I/O. A recoverable failure must be
visible in the saved call; a valid answer is never rejected for its conclusion.
"""

from copy import deepcopy
from decimal import Decimal
import json

from finauditgate.core.artifacts import canonical_json_bytes


POLICY = "thesis-stage-recovery/v2"
REASONS = {"MISSING_REASON", "EMPTY_RESPONSE", "LENGTH", "READ_TIMEOUT", "CONNECTION"}
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


def repair_messages(base, candidate, paths):
    prompt = deepcopy(base)
    prompt[0]["content"] += ("\n本次仅补全上一响应缺失的reason。返回同一完整JSON对象；"
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


def failure_reason(call, candidate=None, errors=(), kind=None):
    if is_length(call):
        return "LENGTH", []
    choices = (call.get("failure_response") or {}).get("provider_response", {}).get("choices", [])
    captured_output = any((c.get("message") or {}).get("content")
        or (c.get("message") or {}).get("tool_calls") or (c.get("message") or {}).get("refusal") for c in choices)
    if call.get("error_type") in TRANSPORT and not call.get("output") and not captured_output:
        return TRANSPORT[call["error_type"]], []
    if is_empty(call) and not call.get("error_type"):
        return "EMPTY_RESPONSE", []
    paths = missing_reason_paths(candidate, kind)
    if (paths and errors and not call.get("error_type") and all(e.get("type") == "missing" for e in errors)
            and {tuple(e["loc"]) for e in errors} == {tuple(p) for p in paths}):
        return "MISSING_REASON", paths
    return None, []


class RecoveryState:
    def __init__(self, value=None):
        self.value = deepcopy(value) if value is not None else {
            "policy": POLICY, "max_extra_calls": 2, "max_per_stage": 2, "attempts": [], "halted": None,
            "retired_final_calls": []}
        validate_state(self.value)

    def assert_open(self, node, kind):
        halted = self.value["halted"]
        if halted is not None and (halted["node"], halted["kind"]) == (node, kind):
            raise ValueError("THESIS_STAGE_RECOVERY_EXHAUSTED")

    def may_reserve(self, node, kind, failed_call, reason):
        attempts = self.value["attempts"]
        prior = [a for a in attempts if (a["node"], a["kind"]) == (node, kind)]
        return len(attempts) < 2 and (not prior or (
            len(prior) == 1 and prior[0]["reason"] in {"CONNECTION", "READ_TIMEOUT"}
            and reason == "MISSING_REASON" and prior[0]["retry_run_id"] == failed_call["run_id"]))

    def reserve(self, node, kind, failed_call, reason, paths, first_effort):
        attempts = self.value["attempts"]
        if not self.may_reserve(node, kind, failed_call, reason):
            self.halt(node, kind, "THESIS_STAGE_RECOVERY_EXHAUSTED")
            raise ValueError("THESIS_STAGE_RECOVERY_EXHAUSTED")
        entry = {"node": node, "kind": kind, "reason": reason, "missing_reason_paths": deepcopy(paths),
                 "failed_run_id": failed_call["run_id"], "retry_run_id": None,
                 "retry_effort": "high" if reason == "LENGTH" else first_effort}
        attempts.append(entry)
        return entry

    def halt(self, node, kind, reason):
        self.value["halted"] = {"node": node, "kind": kind, "reason": reason}

    def snapshot(self):
        return deepcopy(self.value)


def validate_state(state):
    if (not isinstance(state, dict) or set(state) != {"policy", "max_extra_calls", "max_per_stage", "attempts", "halted", "retired_final_calls"}
            or state["policy"] != POLICY or type(state["max_extra_calls"]) is not int or state["max_extra_calls"] != 2
            or type(state["max_per_stage"]) is not int or state["max_per_stage"] != 2
            or not isinstance(state["attempts"], list) or len(state["attempts"]) > 2
            or not isinstance(state["retired_final_calls"], list) or len(state["retired_final_calls"]) not in (0, 2, 3)):
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    seen = {}
    for row in state["attempts"]:
        if (not isinstance(row, dict) or set(row) != {"node", "kind", "reason", "missing_reason_paths", "failed_run_id", "retry_run_id", "retry_effort"}
                or any(not isinstance(row[k], str) or not row[k] for k in ("node", "kind", "failed_run_id", "retry_effort"))
                or row["reason"] not in REASONS or not isinstance(row["missing_reason_paths"], list)
                or (row["retry_run_id"] is not None and not isinstance(row["retry_run_id"], str))
                ):
            raise ValueError("THESIS_RECOVERY_STATE_INVALID")
        key = (row["node"], row["kind"])
        if key in seen:
            previous = seen[key]
            if (previous["reason"] not in {"CONNECTION", "READ_TIMEOUT"} or row["reason"] != "MISSING_REASON"
                    or previous["retry_run_id"] is None or previous["retry_run_id"] != row["failed_run_id"]):
                raise ValueError("THESIS_RECOVERY_STATE_INVALID")
        seen[key] = row
    halted = state["halted"]
    if halted is not None and (not isinstance(halted, dict) or set(halted) != {"node", "kind", "reason"}
            or any(not isinstance(v, str) or not v for v in halted.values())):
        raise ValueError("THESIS_RECOVERY_STATE_INVALID")
    retired = state["retired_final_calls"]
    if retired:
        entries = [r for r in state["attempts"] if r["node"] == "Portfolio Manager" and r["kind"] == "FinalResearchReport"]
        ids = list(dict.fromkeys(i for row in entries for i in (row["failed_run_id"], row["retry_run_id"])))
        if (not entries or any(not isinstance(c, dict) for c in retired)
                or None in ids or [c.get("run_id") for c in retired] != ids):
            raise ValueError("THESIS_RECOVERY_RETIRED_FINAL_INVALID")


def validate_recoveries(calls, state, *, complete=False):
    """Bind each recovery to the failed and retried raw calls, without Pydantic."""
    from .thesis_responses import response_candidate
    validate_state(state)
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
    if prior_reuse and prior_reuse.get("budget_origin") in ("SAME_V16_FLOW", "SAME_V17_FLOW"):
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
