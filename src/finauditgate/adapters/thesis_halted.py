"""Protocol 23: a run that stops still delivers what it completed, with a conclusion when one exists.

The halted record keeps every completed stage output, every saved call and the
recovery state at the stop. No model is called to build it. The Case reader proves
it the way it proves a complete Case and recomputes the rule's reference rating and
the conclusion; a record the reader refuses is not delivered, and the stop is
reported as before.
"""

from copy import deepcopy
import json

from finauditgate.core.artifacts import canonical_json_bytes


SCHEMA = "finresearchops.thesis-halted-case/v1"
# Where each stage's delivered output lives; a stage keyed by node fills one entry of a mapping.
STAGE_FIELDS = {"ResearchEvaluation": ("research_evaluation",), "ExecutionReview": ("execution_review",),
                "IndependentAssessment": ("independent_assessment",),
                "UnderwritingDraft": ("forward_draft", "forward_calculations"),
                "ForwardRevision": ("forward_revision", "effective_forward_draft", "effective_forward_calculations",
                                    "applied_changes")}
NODE_FIELDS = {"AnalystReport": "analyst_reports", "InitialBrief": "initial", "RevisionBrief": "revisions",
               "RiskBrief": "risk_briefs"}
# Stops that say the saved inputs of a resumed run cannot be trusted, and interruptions by the
# user (recorded as a stop even when a resume meets them later), are never delivered.
_INTEGRITY = ("THESIS_RESUME_", "THESIS_PENDING_", "THESIS_PRESENTATION_REPLAY")
_INTERRUPTS = ("KeyboardInterrupt", "SystemExit", "GeneratorExit")


def deliverable_reason(reason):
    return not reason.startswith(_INTEGRITY) and reason not in _INTERRUPTS


def deliverable(session, error):
    """Whether a stop of this protocol 23 session should be delivered as a halted Case."""
    if session is None or session.protocol_version < 23 or not isinstance(error, Exception):
        return False
    halted = session.recovery.value["halted"]
    return halted is not None and deliverable_reason(halted["reason"])


def conclusion(rule, research_evaluation):
    """The rule's rating when the scenarios allow it, otherwise the research manager's, otherwise none."""
    if rule is not None and rule["status"] == "COMPUTED":
        return {"rating": rule["rating"], "source": "RULE"}
    if research_evaluation is not None:
        return {"rating": research_evaluation["plan"]["recommendation"], "source": "RESEARCH_MANAGER"}
    return {"rating": None, "source": None}


def scenario_calculations(record):
    """The latest recomputed scenarios: after the forward revision when it completed."""
    effective = record.get("effective_forward_calculations")
    return effective if effective is not None else record.get("forward_calculations")


def halted_record(session, capture, *, request, model, runtime_kind, topology, upstream_commit, budget, reused):
    from finauditgate.core.rating_rule import rule_rating
    stages = {"forward_revision": getattr(session, "forward_revision", None),
              "effective_forward_draft": getattr(session, "effective_forward_draft", None),
              "effective_forward_calculations": getattr(session, "effective_forward_calculations", None),
              "applied_changes": getattr(session, "applied_changes", None)}
    record = {"schema_version": SCHEMA, "protocol_version": session.protocol_version, "status": "HALTED",
              "review_status": "AWAITING_REVIEW", "financial_gate": "NOT_REQUIRED", "automatic_trading": False,
              "sensitivity_policy": "DECLARED_SCENARIOS_REPORT_ONLY", "upstream_commit": upstream_commit,
              "runtime_kind": runtime_kind, "model": model, "request": request, "source_bundle": session.bundle,
              "topology": topology, "halted": session.recovery.value["halted"],
              "analyst_reports": session.analyst_reports, "initial": session.initial, "revisions": session.revisions,
              "updated_claims": session.updated_claims() if len(session.revisions) == 2 else None,
              "research_evaluation": session.research, "execution_review": session.execution,
              "risk_briefs": session.risks, "independent_assessment": session.independent,
              "forward_draft": session.forward_draft, "forward_calculations": session.forward_calculations, **stages,
              "exchanges": session.exchanges, "model_calls": capture.model_calls, "tool_calls": capture.tool_calls,
              "recovery": session.recovery.snapshot(), "budget": budget}
    if reused is not None:
        record["reused_calls"] = reused
    stopped = (record["halted"]["node"], record["halted"]["kind"])
    if session.exchanges and (session.exchanges[-1]["node"], session.exchanges[-1]["kind"]) == stopped:
        # The stopped stage's answer failed a check after it was parsed: it stays in the saved
        # exchanges and calls for review, but it is not delivered as a completed stage.
        record = {**record, **{field: None for field in STAGE_FIELDS.get(stopped[1], ())}}
        if stopped[1] in NODE_FIELDS:
            field = NODE_FIELDS[stopped[1]]
            record[field] = {node: value for node, value in record[field].items() if node != stopped[0]}
    if len(record["revisions"]) != 2:
        record["updated_claims"] = None
    calculations = scenario_calculations(record)
    record["rule_rating"] = rule_rating(calculations, request["horizon_months"]) if calculations is not None else None
    record["conclusion"] = conclusion(record["rule_rating"], record["research_evaluation"])
    record["signal"] = record["conclusion"]["rating"]
    # Break every reference to the live session before the record is validated and saved.
    return json.loads(canonical_json_bytes(deepcopy(record)))
