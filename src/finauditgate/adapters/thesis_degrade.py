"""Protocol 20: a non-critical stage may be left out when every saved answer is proven unusable.

The four analysts and the trader feed context, not calculations, to later
stages. When their answers fail, the run continues with a visible placeholder
and the Case is saved PARTIAL. The same standard-library proof runs at run
time and on reopen: every saved answer of the stage must fail its own checks.
"""

from copy import deepcopy


DEGRADABLE = (("Fundamentals Analyst", "AnalystReport"), ("Market Analyst", "AnalystReport"),
              ("News Analyst", "AnalystReport"), ("Sentiment Analyst", "AnalystReport"), ("Trader", "ExecutionReview"))
NOTE = ("本角色本次输出未通过程序校验，已按降级规则省略；不代表资料中没有相关信息，也不代表中性观点。"
        "原始回答与失败原因保留在过程记录中。")
# Stops that end a run: a limit, a refused truncation retry, or a stage already halted before a resume.
_STOPS = ("THESIS_TRUNCATION_RECOVERY_NOT_PERMITTED", "THESIS_STAGE_RECOVERY_EXHAUSTED")


def is_stop(code):
    return code.startswith("NATIVE_") or code in _STOPS


def placeholder(node, kind, reason):
    return {"degraded": True, "node": node, "kind": kind, "reason": reason, "note": NOTE}


def is_placeholder(value):
    return isinstance(value, dict) and value.get("degraded") is True


def stage_calls(calls, node, kind):
    return [c for c in calls if c.get("node") == node and (c.get("thesis_stage") or {}).get("kind") == kind]


def usable(call, kind, protocol_version, allowed_refs):
    """Whether this saved answer passes every acceptance check of its stage."""
    from .thesis_analysts import validate_report
    from .thesis_format import schema_errors
    from .thesis_protocol import check_refs
    from .thesis_recovery import is_length
    from .thesis_responses import response_candidate
    if call.get("retained_length") is not True and (call.get("error_type") or is_length(call)):
        return False
    outputs = call.get("output") or []
    if not outputs:
        return False
    try:
        candidate = response_candidate(outputs, kind, protocol_version=protocol_version)
        if schema_errors(kind, candidate, protocol_version):
            return False
        check_refs(candidate, allowed_refs)
        if kind == "AnalystReport":
            validate_report(deepcopy(candidate), allowed_refs)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    return True


def proven(calls, node, kind, protocol_version, allowed_refs):
    """The saved calls of one degradable stage, when every one of them is unusable; else None."""
    if (node, kind) not in DEGRADABLE:
        return None
    rows = stage_calls(calls, node, kind)
    if not rows or any(usable(c, kind, protocol_version, allowed_refs) for c in rows):
        return None
    return rows


def check_degraded(calls, state, protocol_version, allowed_refs):
    """Re-prove every recorded degradation from the saved calls and recovery rows alone.

    Every saved answer must fail its own checks, the reason must not be a stop,
    and the last failure must have had no retry left under the recorded policy.
    """
    from .thesis_recovery import RecoveryState, failure_reason, retained_length_outputs
    index = {c["run_id"]: i for i, c in enumerate(calls)}
    for entry in state["degraded"]:
        rows = proven(calls, entry["node"], entry["kind"], protocol_version, allowed_refs)
        if is_stop(entry["reason"]) or rows is None or [c["run_id"] for c in rows] != entry["run_ids"]:
            raise ValueError("THESIS_DEGRADED_STAGE_NOT_PROVEN")
        last = rows[-1]
        reason, _ = failure_reason(last, kind=entry["kind"], protocol_version=protocol_version)
        try:
            # A complete answer returned at the output limit is kept or refused, never asked again.
            final_answer = retained_length_outputs(last) is not None
        except ValueError:
            final_answer = True  # the run stopped on it before a retry could be reserved
        if reason is not None and not final_answer:
            then = RecoveryState(policy=state["policy"])
            then.value["attempts"] = deepcopy([a for a in state["attempts"] if index.get(a["failed_run_id"], len(calls)) < index[last["run_id"]]])
            if then.may_reserve(entry["node"], entry["kind"], last, reason):
                raise ValueError("THESIS_DEGRADED_STAGE_NOT_PROVEN")


def case_status(degraded, evidence_status, masked=False):
    """A Case with any degraded stage, or with hidden final-report numbers (protocol 24), is PARTIAL."""
    return "PARTIAL" if degraded or masked else evidence_status
