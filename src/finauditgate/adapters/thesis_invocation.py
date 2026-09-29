"""Invoke a stage with a narrow transport-then-repair recovery chain."""

from copy import deepcopy
import re
from time import sleep

from .thesis_recovery import check_missing_repair, failure_reason, length_retry_effort, repair_messages, retained_length_outputs
from .thesis_format import check_enum_repair, enum_repair_messages, schema_errors
from .thesis_schemas_v18 import SCHEMAS
from .thesis_responses import response_candidate

# Protocol 21 reasoning effort of every stage's first attempt; the configured effort is not used.
# Analysts and the trader are non-critical context; the final report and revision reached the
# output cap at "max", so they start at "high".
EFFORT_V21 = {"AnalystReport": "low", "InitialBrief": "high", "RevisionBrief": "high", "ResearchEvaluation": "high",
              "ExecutionReview": "low", "RiskBrief": "high", "IndependentAssessment": "max", "UnderwritingDraft": "max",
              "ForwardRevision": "high", "FinalResearchReport": "high"}


def _parse(session, raw, kind):
    candidate = response_candidate([{"content": raw.content, "tool_calls": raw.tool_calls}], kind,
                                   protocol_version=max(16, session.protocol_version))
    if session.protocol_version >= 19 and kind in SCHEMAS and schema_errors(kind, candidate):
        # Pydantic accepts some spellings the frozen schema refuses (a date-time for a date, a
        # numeric string for a number). Refuse them here, as the reader would, so content and
        # citation checks only ever run on schema-valid answers.
        raise ValueError("THESIS_STAGE_SCHEMA_INVALID")
    value = session.types[kind].model_validate(candidate).model_dump(mode="json")
    if kind == "FinalResearchReport":
        from finauditgate.application.research_delivery import normalize_report
        value = normalize_report(value, session.bundle)
    else:
        from .thesis_protocol import check_refs, source_view
        check_refs(value, {s["id"] for s in source_view(session.bundle)["sources"]})
    return candidate, value


def _retry_input(session, kind, prompt, failed, entry):
    """The retry prompt and frozen previous answer, rebuilt from the saved failed call."""
    tail = session.protocol_version >= 21
    if entry["reason"] == "MISSING_REASON":
        previous = response_candidate(failed["output"], kind, protocol_version=max(16, session.protocol_version))
        return repair_messages(prompt, previous, entry["missing_reason_paths"], tail=tail), previous
    if entry["reason"] == "ENUM_INVALID":
        previous = response_candidate(failed["output"], kind, protocol_version=session.protocol_version)
        return enum_repair_messages(prompt, kind, previous, entry["enum_paths"], tail=tail), previous
    # Transport, length, empty and unparseable failures repeat the unchanged prompt.
    return deepcopy(prompt), None


def _check_repair(entry, previous, candidate, kind):
    if entry is None:
        return
    if entry["reason"] == "MISSING_REASON":
        check_missing_repair(previous, candidate, entry["missing_reason_paths"], kind)
    elif entry["reason"] == "ENUM_INVALID":
        check_enum_repair(previous, candidate, entry["enum_paths"], kind)


def invoke_stage(session, node, kind, prompt, legacy_prompt, config):
    completed, capture, policy = session.completed, session.capture, session.recovery
    if completed:
        completed.carry_halted_calls(node, kind, capture)
    policy.assert_open(node, kind)
    reused = completed.take(node, (legacy_prompt, prompt), capture) if completed else None
    if reused is not None:
        raw, actual = reused
        _, parsed = _parse(session, raw, kind)
        if kind == "FinalResearchReport":
            session.final_generation = [{"status": "REUSED", "response_id": raw.id,
                "prior_final_generation": deepcopy(completed.receipt.get("prior_final_generation"))}]
        return raw, actual, parsed
    if (completed and completed.receipt and completed.receipt.get("presentation_replay")
            and not completed.receipt["presentation_replay"].get("new_primary_calls_allowed", False)):
        raise ValueError("THESIS_PRESENTATION_REPLAY_REQUIRES_CAPTURED_CALL")
    model = session.model
    forced = (EFFORT_V21.get(kind) if session.protocol_version >= 21 else
              "max" if kind in {"IndependentAssessment", "UnderwritingDraft", "ForwardRevision", "FinalResearchReport", "DataReview"}
              else None)
    if forced and hasattr(model, "reasoning_effort"):
        model = model.model_copy(update={"reasoning_effort": forced})
    first_effort = (getattr(model, "reasoning_effort", None) or session.configured_effort or "provider_default")
    actual, entry, previous = deepcopy(prompt), None, None
    next_attempt = 1
    pending = next((r for r in policy.value["attempts"] if (r["node"], r["kind"]) == (node, kind)
                    and r["retry_run_id"] is None), None)
    if pending is not None:
        if completed is None:
            raise ValueError("THESIS_PENDING_RECOVERY_INPUT_MISMATCH")
        failed = completed.carry_pending_call(pending, (legacy_prompt, prompt), capture)
        entry = pending
        first_effort = failed["thesis_stage"]["reasoning_effort"]
        next_attempt = failed["thesis_stage"]["attempt"] + 1
        if kind == "FinalResearchReport":
            generations = deepcopy(completed.receipt.get("prior_final_generation") or [])
            entries = [r for r in policy.value["attempts"] if (r["node"], r["kind"]) == (node, kind)]
            if (len(generations) != next_attempt - 1 or len(entries) != len(generations)
                    or any(g.get("attempt") != i + 1
                        or g.get("status") != ("TRUNCATED" if e["reason"] == "LENGTH" else "FAILED")
                        or g.get("reasoning_effort") != first_effort
                        for i, (g, e) in enumerate(zip(generations, entries)))):
                raise ValueError("THESIS_PENDING_FINAL_GENERATION_INVALID")
            session.final_generation = generations
        actual, previous = _retry_input(session, kind, prompt, failed, entry)
        if entry["reason"] == "LENGTH" and session.budget is not None:
            session.budget.release_confirmed_truncation(confirmed_length=True)
    for attempt in range(next_attempt, 4):
        effort = first_effort if entry is None else entry["retry_effort"]
        active = model.model_copy(update={"reasoning_effort": effort}) if hasattr(model, "reasoning_effort") and effort != "provider_default" else model
        stage_config = {**config, "metadata": {**config.get("metadata", {}),
            "thesis_stage": {"kind": kind, "attempt": attempt, "reasoning_effort": effort}}}
        start = len(capture.model_calls)
        raw, candidate = None, None
        try:
            response = active.with_structured_output(session.types[kind], method="json_mode", include_raw=True).invoke(actual, config=stage_config)
            if not isinstance(response, dict) or response.get("raw") is None:
                raise ValueError("THESIS_STRUCTURED_RESPONSE_REQUIRED")
            raw = response["raw"]
            candidate = response_candidate([{"content": raw.content, "tool_calls": raw.tool_calls}], kind,
                                           protocol_version=max(16, session.protocol_version))
            _check_repair(entry, previous, candidate, kind)
            _, parsed = _parse(session, raw, kind)
            if entry is not None:
                entry["retry_run_id"] = capture.model_calls[-1]["run_id"]
            if kind == "FinalResearchReport":
                session.final_generation.append({"attempt": attempt, "reasoning_effort": effort,
                    "status": "COMPLETED", "response_id": raw.id})
            return raw, actual, parsed
        except Exception as exc:
            call = capture.model_calls[-1] if len(capture.model_calls) > start else None
            if entry is not None and call is not None:
                entry["retry_run_id"] = call["run_id"]
            retained = retained_length_outputs(call or {})
            if retained is not None:
                from langchain_core.messages import AIMessage
                value = retained[0]
                usage = value["usage"]
                raw = AIMessage(content=value["content"], id=value["id"], tool_calls=[],
                    usage_metadata=usage if isinstance(usage, dict) and {"input_tokens", "output_tokens", "total_tokens"} <= usage.keys() else None)
                candidate, parsed = _parse(session, raw, kind)
                _check_repair(entry, previous, candidate, kind)
                capture.retain_complete_length(call["run_id"], retained)
                if session.budget is not None:
                    if not session.budget.release_confirmed_truncation(confirmed_length=True):
                        raise ValueError("THESIS_TRUNCATION_RECOVERY_NOT_PERMITTED") from exc
                if kind == "FinalResearchReport":
                    session.final_generation.append({"attempt": attempt, "reasoning_effort": effort,
                        "status": "RETAINED_LENGTH", "response_id": raw.id})
                return raw, actual, parsed
            errors = exc.errors(include_url=False) if hasattr(exc, "errors") else []
            reason, paths = failure_reason(call or {}, candidate, errors, kind, protocol_version=session.protocol_version)
            if kind == "FinalResearchReport":
                session.final_generation.append({"attempt": attempt, "reasoning_effort": effort,
                    "status": "TRUNCATED" if reason == "LENGTH" else "FAILED", "error_type": type(exc).__name__})
            if reason is None or call is None or not policy.may_reserve(node, kind, call, reason):
                code = str(exc) if re.fullmatch(r"[A-Z0-9_]{1,120}", str(exc)) else type(exc).__name__
                policy.halt(node, kind, code)
                raise
            entry = policy.reserve(node, kind, call, reason, paths, first_effort,
                                   length_effort=length_retry_effort(first_effort, session.protocol_version))
            if reason == "LENGTH" and session.budget is not None:
                if not session.budget.release_confirmed_truncation(confirmed_length=True):
                    policy.halt(node, kind, "THESIS_TRUNCATION_RECOVERY_NOT_PERMITTED")
                    raise ValueError("THESIS_TRUNCATION_RECOVERY_NOT_PERMITTED") from exc
            if session.protocol_version >= 18:
                actual, previous = _retry_input(session, kind, prompt, call, entry)
            elif reason == "MISSING_REASON":
                previous = deepcopy(candidate)
                actual = repair_messages(prompt, candidate, paths)
            if reason in {"READ_TIMEOUT", "CONNECTION"}:
                sleep(2)
    raise AssertionError("UNREACHABLE_THESIS_RECOVERY")
