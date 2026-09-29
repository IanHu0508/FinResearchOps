"""Protocol 22: content failures a program can prove from one saved answer and its own prompt.

Two narrow checks, answered by one more request at the same stage with the proven
problems appended after the unchanged prompt:

- FORWARD_INCONSISTENT: the forward draft fails a check the program runs before
  calculating: scenario order, a price date after the cutoff, the fixed valuation date,
  or any rule of the calculator itself (period, earnings basis, value ranges);
- UNKNOWN_SOURCE_REFERENCE: a critical research stage cites a source the prompt never gave it
  (an empty string included).

Both failures stopped a protocol 21 run, so the extra request never turns a delivered
run into a stopped one. The request is the stage's only recovery (after at most one
transport retry), within the run's three extra calls; otherwise the stage stops as in
protocol 21. The final report is not checked here: a source ID in its prose is not an
evidence block, so it stays a finding and the report is delivered as before. The four
analysts and the trader keep the protocol 20 rule and are left out, not asked again.
An answer kept after its output hit the length limit is never asked again.

Nothing here judges financial content. The Case reader proves each recorded check
again from the saved call and rebuilds the request that followed it.
"""

from finauditgate.core.artifacts import canonical_json_bytes


CODES = ("FORWARD_INCONSISTENT", "UNKNOWN_SOURCE_REFERENCE")
_NOT_CHECKED = ("FinalResearchReport", "AnalystReport", "ExecutionReview")
_NOTES = {
    "FORWARD_INCONSISTENT": "上一回答未通过程序在复算前执行的检查：请修正检查结果指出的字段，其余判断可以保持。",
    "UNKNOWN_SOURCE_REFERENCE": "上一回答的evidence_refs含有本次资料中不存在的来源编号："
        "evidence_refs只能使用source_bundle中给出的来源编号。",
}


def forward_error(draft, request):
    """Every check a forward draft fails before its numbers are used, as one text; None when it passes.

    The three checks of the stage are independent; the calculator reports its first error.
    """
    from finauditgate.core.forward_scenarios import calculate_forward, valuation_date_for
    problems = []
    ids = [s["scenario_id"] for s in draft["scenarios"]]
    if ids != [f"F{i}" for i in range(1, len(ids) + 1)]:
        problems.append("THESIS_FORWARD_SCENARIO_IDS_INVALID: scenario_id must run F1, F2, ... in order, got "
                        + ", ".join(ids))
    if draft["market_price_date"] is not None and draft["market_price_date"] > request["as_of"]:
        problems.append(f"THESIS_FORWARD_FUTURE_MARKET_PRICE: market_price_date must not be after as_of "
                        f"{request['as_of']}, got {draft['market_price_date']}")
    expected = valuation_date_for(request["as_of"], request["horizon_months"])
    if draft["valuation_date"] != expected:
        problems.append(f"THESIS_FORWARD_HORIZON_MISMATCH: valuation_date must be {expected} for this horizon, "
                        f"got {draft['valuation_date']}")
    try:
        calculate_forward(draft)
    except ValueError as exc:
        problems.append(str(exc))
    return "; ".join(problems) or None


def _refs(value, found):
    if isinstance(value, dict):
        if isinstance(value.get("evidence_refs"), list):
            found.extend(ref for ref in value["evidence_refs"] if isinstance(ref, str))
        for child in value.values():
            _refs(child, found)
    elif isinstance(value, list):
        for child in value:
            _refs(child, found)
    return found


def check(kind, candidate, payload):
    """Every proven content failure of a schema-valid answer, as [{"code", "detail"}] in CODES order."""
    if kind in _NOT_CHECKED:
        return []
    found = []
    if kind == "UnderwritingDraft":
        error = forward_error(candidate, payload["request"])
        if error is not None:
            found.append({"code": "FORWARD_INCONSISTENT", "detail": error})
    allowed = {source["id"] for source in payload["source_bundle"]["sources"]}
    unknown = sorted(set(_refs(candidate, [])) - allowed)
    if unknown:
        found.append({"code": "UNKNOWN_SOURCE_REFERENCE", "detail": unknown})
    return found


def content_failure(call, kind, protocol_version):
    """(CONTENT_CHECK, checks) for a saved schema-valid answer the checks refuse; (None, []) otherwise."""
    from .thesis_format import schema_errors
    from .thesis_protocol import payload_of
    from .thesis_recovery import call_messages
    from .thesis_responses import response_candidate
    from .thesis_schemas_v18 import SCHEMAS
    if (protocol_version < 22 or kind in _NOT_CHECKED or kind not in SCHEMAS
            or call.get("error_type") or not call.get("output")):
        return None, []
    try:
        candidate = response_candidate(call["output"], kind, protocol_version=protocol_version)
        if schema_errors(kind, candidate):
            return None, []
        found = check(kind, candidate, payload_of(call_messages(call)[1]["content"], protocol_version))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None, []
    return ("CONTENT_CHECK", found) if found else (None, [])


def content_repair_messages(base, found):
    """The unchanged prompt, then the proven problems; the whole answer is asked again."""
    from copy import deepcopy
    prompt = deepcopy(base)
    prompt[-1]["content"] += ("\n【程序检查未通过】" + "".join(_NOTES[f["code"]] for f in found)
                              + "其余要求不变，重新给出完整JSON。检查结果：\n" + canonical_json_bytes(found).decode())
    return prompt
