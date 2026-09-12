"""Validate v11 bindings without changing the published v10 reader."""

import json

from finauditgate.adapters.thesis_protocol import (
    RESEARCHERS, RISKS, belief_view, check_refs, claim_view, research_request_view,
    risk_view, source_view, validate_sources,
)
from finauditgate.adapters.thesis_correction import render_decision
from finauditgate.adapters.thesis_responses import response_candidate
from finauditgate.application.research_numbers import render_research_block
from finauditgate.core.forward_revision import apply_forward_revision
from finauditgate.core.forward_scenarios import calculate_forward, valuation_date_for


def _without_nulls(value):
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def _coverage(rows, ids, key):
    if len(rows) != len(ids) or {r[key] for r in rows} != set(ids):
        raise ValueError("THESIS_CLAIM_COVERAGE_INVALID")


def validate(record):
    if (record.get("schema_version") != "finresearchops.thesis-case/v11"
            or record.get("status") != "COMPLETED" or record.get("review_status") != "AWAITING_REVIEW"
            or record.get("financial_gate") != "NOT_REQUIRED" or record.get("automatic_trading") is not False
            or record.get("sensitivity_policy") != "DECLARED_SCENARIOS_REPORT_ONLY"):
        raise ValueError("THESIS_RECORD_INVALID")
    validate_sources(record["source_bundle"], record["request"])
    expected_nodes = [*RESEARCHERS, *RESEARCHERS, "Research Manager", "Trader", *RISKS, *(["Portfolio Manager"] * 4)]
    kinds = ["InitialBrief", "InitialBrief", "RevisionBrief", "RevisionBrief", "ResearchEvaluation", "ExecutionReview",
             "RiskBrief", "RiskBrief", "RiskBrief", "IndependentAssessment", "UnderwritingDraft", "ForwardRevision", "FinalResearchReport"]
    exchanges = record["exchanges"]
    if [e["kind"] for e in exchanges] != kinds or [e["node"] for e in exchanges] != expected_nodes:
        raise ValueError("THESIS_PROTOCOL_ORDER_INVALID")
    sources, request = source_view(record["source_bundle"]), research_request_view(record["request"])
    allowed_refs = {r["id"] for r in sources["sources"]}
    order = []
    for exchange in exchanges:
        matches = [(i, c) for i, c in enumerate(record["model_calls"]) if c["node"] == exchange["node"]
                   and any(o.get("id") == exchange["response_id"] for o in c.get("output", []))]
        if len(matches) != 1:
            raise ValueError("THESIS_RESPONSE_BINDING_INVALID")
        index, call = matches[0]
        if call.get("error_type"):
            raise ValueError("THESIS_FAILED_RESPONSE_REUSED")
        order.append(index)
        messages = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]} for m in call["messages"][0]]
        if messages != exchange["messages"]:
            raise ValueError("THESIS_INPUT_BINDING_INVALID")
        if _without_nulls(response_candidate(call["output"], exchange["kind"])) != _without_nulls(exchange["parsed"]):
            raise ValueError("THESIS_OUTPUT_BINDING_INVALID")
        check_refs(exchange["parsed"], allowed_refs)
        payload = json.loads(messages[1]["content"])
        if payload.get("request") != request or payload.get("source_bundle") != sources:
            raise ValueError("THESIS_RESEARCH_REQUEST_BINDING_INVALID")
        base = {"node": exchange["node"], "request": request, "source_bundle": sources}
        if exchange["kind"] == "InitialBrief" and payload != base:
            raise ValueError("THESIS_INITIAL_NOT_INDEPENDENT")
        if exchange["kind"] == "RevisionBrief":
            other = RESEARCHERS[1] if exchange["node"] == RESEARCHERS[0] else RESEARCHERS[0]
            base.update(own_initial=record["initial"][exchange["node"]], opponent_initial=record["initial"][other])
            if payload != base:
                raise ValueError("THESIS_REVISION_NOT_SYMMETRIC")
        if exchange["kind"] == "IndependentAssessment":
            if set(payload) != {"node", "request", "source_bundle", "portfolio_context"}:
                raise ValueError("THESIS_INDEPENDENT_INPUT_LEAK")
        if exchange["kind"] == "UnderwritingDraft":
            base.update(updated_claims=claim_view(record["updated_claims"]), risk_briefs=risk_view(record["risk_briefs"]))
            if payload != base:
                raise ValueError("THESIS_FORWARD_INPUT_LEAK")
        if exchange["kind"] == "ForwardRevision":
            base.update(independent_beliefs=belief_view(record["independent_assessment"]),
                        updated_claims=claim_view(record["updated_claims"]), risk_briefs=risk_view(record["risk_briefs"]),
                        forward_draft=record["forward_draft"], forward_calculations=record["forward_calculations"])
            if payload != base:
                raise ValueError("THESIS_REVISION_INPUT_BINDING_INVALID")
        if exchange["kind"] == "FinalResearchReport":
            base.update(effective_forward_draft=record["effective_forward_draft"],
                        effective_forward_calculations=record["effective_forward_calculations"],
                        research_resolution={k: record["forward_revision"][k] for k in
                            ("claim_assessments", "belief_updates", "unresolved_issues")})
            if payload != base:
                raise ValueError("THESIS_EFFECTIVE_NUMBERS_NOT_DELIVERED")
    if order != sorted(set(order)):
        raise ValueError("THESIS_MODEL_CALL_ORDER_INVALID")
    if set(order) != {i for i, c in enumerate(record["model_calls"]) if c.get("output") and not c.get("error_type")}:
        raise ValueError("THESIS_UNBOUND_MODEL_OUTPUT")
    for exchange in exchanges:
        node, kind, parsed = exchange["node"], exchange["kind"], exchange["parsed"]
        if kind == "InitialBrief":
            prefix = "B" if node == RESEARCHERS[0] else "S"
            if record["initial"][node] != {"claims": [{"id": f"{prefix}{i}", **c} for i, c in enumerate(parsed["claims"], 1)]}:
                raise ValueError("THESIS_INITIAL_RECORD_CHANGED")
        if kind == "RevisionBrief" and record["revisions"][node] != parsed:
            raise ValueError("THESIS_REVISION_RECORD_CHANGED")
        if kind == "RiskBrief" and record["risk_briefs"][node] != parsed:
            raise ValueError("THESIS_RISK_RECORD_CHANGED")
    rebuilt = []
    for node in RESEARCHERS:
        claims = {c["id"]: c for c in record["initial"][node]["claims"]}
        revision = record["revisions"][node]
        _coverage(revision["updates"], list(claims), "claim_id")
        other = RESEARCHERS[1] if node == RESEARCHERS[0] else RESEARCHERS[0]
        _coverage(revision["counter_responses"], [c["id"] for c in record["initial"][other]["claims"]], "claim_id")
        for update in revision["updates"]:
            old = claims[update["claim_id"]]
            rebuilt.append({"id": old["id"], "status": update["status"],
                "statement": update["updated_statement"] if update["status"] == "revise" else None if update["status"] == "withdraw" else old["statement"],
                "reason": update["reason"], "evidence_refs": update["evidence_refs"], "would_change_mind": update["would_change_mind"]})
    if rebuilt != record["updated_claims"]:
        raise ValueError("THESIS_UPDATED_CLAIMS_CHANGED")
    by_kind = {e["kind"]: e["parsed"] for e in exchanges}
    for kind, field in (("ResearchEvaluation", "research_evaluation"), ("ExecutionReview", "execution_review"),
                        ("IndependentAssessment", "independent_assessment"), ("UnderwritingDraft", "forward_draft"),
                        ("ForwardRevision", "forward_revision"), ("FinalResearchReport", "final_report")):
        if by_kind[kind] != record[field]:
            raise ValueError("THESIS_DELIVERED_ASSESSMENT_CHANGED")
    if calculate_forward(record["forward_draft"]) != record["forward_calculations"]:
        raise ValueError("THESIS_FORWARD_CALCULATION_MISMATCH")
    effective = apply_forward_revision(record["forward_draft"], record["forward_revision"]["changes"])
    if any(record[k] != effective[k] for k in effective):
        raise ValueError("THESIS_EFFECTIVE_REVISION_MISMATCH")
    draft, calc = record["effective_forward_draft"], record["effective_forward_calculations"]
    if draft["valuation_date"] != valuation_date_for(request["as_of"], request["horizon_months"]):
        raise ValueError("THESIS_FORWARD_HORIZON_MISMATCH")
    if draft["market_price_date"] is not None and draft["market_price_date"] > request["as_of"]:
        raise ValueError("THESIS_FORWARD_FUTURE_MARKET_PRICE")
    resolution, report = record["forward_revision"], record["final_report"]
    ids = [b["belief_id"] for b in record["independent_assessment"]["beliefs"]]
    _coverage(resolution["belief_updates"], ids, "belief_id")
    if any((u["status"] == "revise") != bool(u["new_statement"]) for u in resolution["belief_updates"]):
        raise ValueError("THESIS_DECISION_UPDATE_INVALID")
    _coverage(resolution["claim_assessments"], [c["id"] for c in record["updated_claims"]], "claim_id")
    _coverage(report["scenario_assessments"], [s["scenario_id"] for s in draft["scenarios"]], "scenario_id")
    for block in (report["summary"], *report["financial_analysis"].values(), report["strongest_counterevidence"]):
        render_research_block(block, draft, calc)
    before, after = record["independent_assessment"]["decision"]["rating"], report["rating"]
    if record["rating_comparison"] != {"before": before, "after": after, "changed": before != after}:
        raise ValueError("THESIS_RATING_COMPARISON_INVALID")
    if record["signal"] != after or record["reports"]["final_trade_decision"] != render_decision(report, draft, calc):
        raise ValueError("THESIS_FINAL_REPORT_BINDING_INVALID")
    attempts = record["final_generation"]
    if len(attempts) == 1 and attempts[0]["status"] == "REUSED":
        if attempts[0]["response_id"] != exchanges[-1]["response_id"]:
            raise ValueError("THESIS_FINAL_GENERATION_INVALID")
    else:
        if ([a["status"] for a in attempts] not in (["COMPLETED"], ["TRUNCATED", "COMPLETED"])
                or attempts[-1]["response_id"] != exchanges[-1]["response_id"]
                or [a["reasoning_effort"] for a in attempts] != (["max"] if len(attempts) == 1 else ["max", "high"])):
            raise ValueError("THESIS_FINAL_GENERATION_INVALID")
