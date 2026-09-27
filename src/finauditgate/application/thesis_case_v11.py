"""Validate v11 bindings without changing the published v10 reader."""

import json

from finauditgate.adapters.thesis_protocol import (
    RESEARCHERS, RISKS, belief_view, check_refs, claim_view, research_request_view,
    risk_view, source_view, validate_sources,
)
from finauditgate.adapters.thesis_correction import render_decision, valid_belief_updates
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
    full_analysts = record.get("schema_version") == "finresearchops.thesis-case/v17"
    selected = full_analysts or record.get("schema_version") == "finresearchops.thesis-case/v16"
    bound = selected or record.get("schema_version") == "finresearchops.thesis-case/v13"
    if (record.get("schema_version") not in ("finresearchops.thesis-case/v11", "finresearchops.thesis-case/v13", "finresearchops.thesis-case/v16", "finresearchops.thesis-case/v17")
            or record.get("status") not in (("COMPLETED", "PARTIAL") if selected else ("COMPLETED",)) or record.get("review_status") != "AWAITING_REVIEW"
            or record.get("financial_gate") != "NOT_REQUIRED" or record.get("automatic_trading") is not False
            or record.get("sensitivity_policy") != "DECLARED_SCENARIOS_REPORT_ONLY"):
        raise ValueError("THESIS_RECORD_INVALID")
    validate_sources(record["source_bundle"], record["request"])
    from finauditgate.adapters.thesis_analysts import main_stages
    stages = main_stages(17 if full_analysts else 16)
    expected_nodes, kinds = [s[0] for s in stages], [s[1] for s in stages]
    exchanges = record["exchanges"]
    if [e["kind"] for e in exchanges] != kinds or [e["node"] for e in exchanges] != expected_nodes:
        raise ValueError("THESIS_PROTOCOL_ORDER_INVALID")
    sources, request = source_view(record["source_bundle"]), research_request_view(record["request"])
    allowed_refs = {r["id"] for r in sources["sources"]}
    if full_analysts:
        from finauditgate.adapters.thesis_analysts import ANALYSTS, REPORT_FIELDS, expected_topology, validate_report, render_analyst
        if (set(record.get("analyst_reports", {})) != set(ANALYSTS)
                or set(record["reports"]) != set(REPORT_FIELDS)
                or record["topology"] != expected_topology()
                or request["data_mode"] != "FROZEN_SOURCES"):
            raise ValueError("THESIS_ANALYST_REPORTS_INCOMPLETE")
        for node, field in ANALYSTS.items():
            validate_report(record["analyst_reports"][node], allowed_refs)
            if record["reports"][field] != render_analyst(record["analyst_reports"][node], node):
                raise ValueError("THESIS_ANALYST_REPORT_CHANGED")
    excluded = set()
    if selected:
        from finauditgate.adapters.thesis_recovery import successful_call, validate_recoveries, validate_stage_tag
        excluded, dependencies = validate_recoveries(record["model_calls"], record["recovery"], complete=True)
    order = []
    for exchange in exchanges:
        matches = [(i, c) for i, c in enumerate(record["model_calls"]) if c["node"] == exchange["node"]
                   and any(o.get("id") == exchange["response_id"] for o in c.get("output", []))]
        if len(matches) != 1:
            raise ValueError("THESIS_RESPONSE_BINDING_INVALID")
        index, call = matches[0]
        if selected:
            validate_stage_tag(call, exchange["kind"], dependencies)
        if not (successful_call(call) if selected else not call.get("error_type")):
            raise ValueError("THESIS_FAILED_RESPONSE_REUSED")
        order.append(index)
        messages = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]} for m in call["messages"][0]]
        if messages != exchange["messages"]:
            raise ValueError("THESIS_INPUT_BINDING_INVALID")
        raw_parsed = response_candidate(call["output"], exchange["kind"], protocol_version=16 if selected else 13)
        selected_final = selected and exchange["kind"] == "FinalResearchReport"
        if selected_final:
            from finauditgate.application.research_delivery import normalize_report, final_source_view
            raw_parsed = normalize_report(raw_parsed, record["source_bundle"])
        if _without_nulls(raw_parsed) != _without_nulls(exchange["parsed"]):
            raise ValueError("THESIS_OUTPUT_BINDING_INVALID")
        if not selected_final:
            check_refs(exchange["parsed"], allowed_refs)
        payload = json.loads(messages[1]["content"])
        expected_sources = final_source_view(sources) if selected_final else sources
        if payload.get("request") != request or payload.get("source_bundle") != expected_sources:
            raise ValueError("THESIS_RESEARCH_REQUEST_BINDING_INVALID")
        base = {"node": exchange["node"], "request": request, "source_bundle": expected_sources}
        if full_analysts:
            if exchange["kind"] == "AnalystReport":
                if payload != base or record["analyst_reports"][exchange["node"]] != exchange["parsed"]:
                    raise ValueError("THESIS_ANALYST_REPORT_BINDING_INVALID")
            elif exchange["kind"] != "IndependentAssessment":
                base["analyst_reports"] = record["analyst_reports"]
                if payload.get("analyst_reports") != record["analyst_reports"]:
                    raise ValueError("THESIS_ANALYST_REPORTS_NOT_DELIVERED")
                if exchange["kind"] in ("ResearchEvaluation", "ExecutionReview", "RiskBrief"):
                    expected = {**base, "updated_claims": record["updated_claims"]}
                    if exchange["kind"] == "ExecutionReview":
                        expected["research_evaluation"] = record["research_evaluation"]
                    if payload != expected:
                        raise ValueError("THESIS_ANALYST_DOWNSTREAM_INPUT_CHANGED")
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
            if bound:
                from finauditgate.application.research_narrative import change_view
                base["change_context"] = change_view(record["applied_changes"])
            if selected:
                from finauditgate.application.research_changes import parameter_change_facts
                base["parameter_change_facts"] = parameter_change_facts(record["forward_draft"], record["forward_revision"]["changes"])
            if payload != base:
                raise ValueError("THESIS_EFFECTIVE_NUMBERS_NOT_DELIVERED")
    if order != sorted(set(order)):
        raise ValueError("THESIS_MODEL_CALL_ORDER_INVALID")
    if set(order) != {i for i, c in enumerate(record["model_calls"]) if c.get("output") and (successful_call(c) if selected else not c.get("error_type")) and c.get("run_id") not in excluded}:
        raise ValueError("THESIS_UNBOUND_MODEL_OUTPUT")
    if selected and set(order) | {i for i, c in enumerate(record["model_calls"]) if c.get("run_id") in excluded} != set(range(len(record["model_calls"]))):
        raise ValueError("THESIS_UNACCOUNTED_STAGE_CALL")
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
    if not valid_belief_updates(resolution["belief_updates"], allow_maintain_restatement=full_analysts):
        raise ValueError("THESIS_DECISION_UPDATE_INVALID")
    _coverage(resolution["claim_assessments"], [c["id"] for c in record["updated_claims"]], "claim_id")
    _coverage(report["scenario_assessments"], [s["scenario_id"] for s in draft["scenarios"]], "scenario_id")
    for block in (report["summary"], *report["financial_analysis"].values(), report["strongest_counterevidence"]):
        render_research_block(block, draft, calc)
    context = None
    if bound:
        from finauditgate.application.research_narrative import change_view, report_context
        if selected:
            from finauditgate.application.research_delivery import report_context
        context = report_context(report, draft, calc, sources, request,
                                 changes=change_view(record["applied_changes"]), beliefs=resolution["belief_updates"])
        if selected and (record.get("evidence_check") != context.evidence_check()
                         or record["status"] != context.evidence_check()["status"]):
            raise ValueError("THESIS_EVIDENCE_CHECK_CHANGED")
    before, after = record["independent_assessment"]["decision"]["rating"], report["rating"]
    if record["rating_comparison"] != {"before": before, "after": after, "changed": before != after}:
        raise ValueError("THESIS_RATING_COMPARISON_INVALID")
    if record["signal"] != after or record["reports"]["final_trade_decision"] != render_decision(report, draft, calc, context=context):
        raise ValueError("THESIS_FINAL_REPORT_BINDING_INVALID")
    attempts = record["final_generation"]
    if len(attempts) == 1 and attempts[0]["status"] == "REUSED":
        if attempts[0]["response_id"] != exchanges[-1]["response_id"]:
            raise ValueError("THESIS_FINAL_GENERATION_INVALID")
    else:
        if selected:
            final_id = record["model_calls"][order[-1]]["run_id"]
            recovery = []
            while final_id in dependencies:
                entry = next(r for r in record["recovery"]["attempts"] if r["retry_run_id"] == final_id)
                recovery.insert(0, entry)
                final_id = entry["failed_run_id"]
            final_status = "RETAINED_LENGTH" if record["model_calls"][order[-1]].get("retained_length") else "COMPLETED"
            expected_statuses = [*("TRUNCATED" if r["reason"] == "LENGTH" else "FAILED" for r in recovery), final_status]
            expected_efforts = ["max", *(r["retry_effort"] for r in recovery)]
        else:
            expected_statuses = [a["status"] for a in attempts]
            expected_efforts = ["max"] if len(attempts) == 1 else ["max", "high"]
        allowed_statuses = [["COMPLETED"], ["TRUNCATED", "COMPLETED"]]
        if selected:
            allowed_statuses += [["FAILED", "COMPLETED"], ["RETAINED_LENGTH"], ["TRUNCATED", "RETAINED_LENGTH"], ["FAILED", "RETAINED_LENGTH"], ["FAILED", "FAILED", "COMPLETED"], ["FAILED", "FAILED", "RETAINED_LENGTH"]]
        if ((selected and any(a.get("attempt") != i + 1 for i, a in enumerate(attempts)))
                or expected_statuses not in allowed_statuses
                or [a["status"] for a in attempts] != expected_statuses
                or attempts[-1]["response_id"] != exchanges[-1]["response_id"]
                or [a["reasoning_effort"] for a in attempts] != expected_efforts):
            raise ValueError("THESIS_FINAL_GENERATION_INVALID")
