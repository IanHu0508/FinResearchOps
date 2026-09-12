"""A slim LangGraph research route using the pinned TradingAgents model client.

Third-party imports are lazy. The deterministic core remains standard-library only.
"""

from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
from typing import TypedDict

from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.adapters.research_contract import (
    ANALYSIS_SCHEMA, DECISION_SCHEMA, UPDATE_SCHEMA, ROLES, SYSTEM, clone, evidence_ids, model_evidence_view, required_review_references, selected_drivers, validate_proposal, validate_update, normalize_schema_title,
)
from finauditgate.core.artifacts import canonical_json_bytes, write_once
from finauditgate.private_storage import require_private_storage_root, resolve_private_workspace_anchor


UPSTREAM_COMMIT = "2448d0a12576f9b2ddcd5980a0630833423d1e1b"


class _State(TypedDict, total=False):
    request: dict
    evidence: dict
    analysis: dict
    challenge: dict
    decision: dict


class TradingAgentsResearcher:
    def __init__(self, *, provider="deepseek", model="deepseek-flash", trace_root,
                 budget=None, client=None, reasoning_effort=None, synthesis_effort=None):
        self.provider, self.model = provider, model
        flash = provider == "deepseek" and model in ("deepseek-flash", "deepseek-v4-flash")
        self.budget = budget or ModelBudget(input_per_million="3" if flash else "9",
                                          output_per_million="9" if flash else "27")
        self.reasoning_effort = reasoning_effort
        self.synthesis_effort = synthesis_effort
        self.trace_root = Path(trace_root)
        anchor = resolve_private_workspace_anchor(self.trace_root, purpose="research-model-traces")
        self.trace_root = require_private_storage_root(self.trace_root, purpose="research-model-traces", anchor=anchor)
        self._client = client
        self._injected_client = client is not None
        self._http_client = None
        self._calls = []

    def _preflight(self):
        try:
            installed = version("tradingagents")
        except PackageNotFoundError as exc:
            raise ValueError("TRADINGAGENTS_INTEGRATION_ENV_REQUIRED") from exc
        if installed != "0.4.0":
            raise ValueError("TRADINGAGENTS_VERSION_MISMATCH")
        if not self._injected_client:
            if self.provider == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
                raise ValueError("DEEPSEEK_API_KEY_NOT_CONFIGURED")
            if self.provider not in ("deepseek", "ollama"):
                raise ValueError("RESEARCH_PROVIDER_NOT_CONFIGURED")

    def _get_client(self):
        self._preflight()
        if self._client is None:
            from finauditgate.adapters.model_http import model_http_client
            from tradingagents.llm_clients import create_llm_client
            self._http_client = model_http_client(self.provider, self.budget.max_output_tokens,
                trace_root=self.trace_root, reasoning_effort=self.reasoning_effort, start_index=self.budget.calls)
            self._client = create_llm_client(self.provider, self.model,
                timeout=120, max_retries=0, max_tokens=self.budget.max_output_tokens,
                http_client=self._http_client).get_llm()
        return self._client

    def _draft(self, stage, payload):
        schema = clone(UPDATE_SCHEMA if stage == "update" else DECISION_SCHEMA if stage == "synthesis" else ANALYSIS_SCHEMA)
        known_refs = sorted(evidence_ids(payload["evidence"]))
        if stage == "update":
            fields = schema["properties"]["items"]["items"]["properties"]
            fields["evidence_ids"]["items"]["enum"] = known_refs
            fields["prior_claim_id"]["enum"] = [c["claim_id"] for c in payload["prior_claims"]]
            fields["current_claim_ids"]["items"]["enum"] = [c["claim_id"] for c in payload["current_claims"]]
        else:
            schema["properties"]["claims"]["items"]["properties"]["evidence_ids"]["items"]["enum"] = known_refs
        if stage == "synthesis":
            schema["properties"]["counterevidence_response"]["items"]["properties"]["evidence_id"]["enum"] = known_refs
        if stage in ("analysis", "challenge"):
            allowed = [x["driver_id"] for x in payload["evidence"]["analysis"]["drivers"][:6]]
            selection = schema["properties"]["requested_drivers"]
            selection["description"] = "Choose exact driver_id values from this enum, never human-readable row labels; return [] if no follow-up is needed."
            if allowed:
                selection["items"]["enum"] = allowed
            else:
                selection["maxItems"] = 0
        system = SYSTEM
        if payload["evidence"]["source"].get("period_basis"):
            system = system.replace("annual", "half-year").replace("year-over-year", "same-half-year-over-year")
        prompt = (system + "\n" + ROLES[stage] + "\nJSON schema:\n"
                  + json.dumps(schema, ensure_ascii=False) + "\nEvidence and task:\n"
                  + json.dumps(payload, ensure_ascii=False, sort_keys=True))
        client = self._get_client()
        number = self.budget.reserve(prompt)
        request = {"schema_version": "finresearchops.research-model-request/v1",
                   "stage": stage, "provider": self.provider, "model": self.model,
                   "prompt": prompt, "max_output_tokens": self.budget.max_output_tokens}
        write_once(self.trace_root / f"call-{number:03d}-request.json", canonical_json_bytes(request))
        try:
            options = {"response_format": {"type": "json_object"}}
            effort = self.synthesis_effort if stage in ("synthesis", "update") and self.synthesis_effort else self.reasoning_effort
            if effort:
                options["reasoning_effort"] = effort
            raw = client.bind(**options).invoke(prompt)
            content = getattr(raw, "content", "")
            usage = getattr(raw, "usage_metadata", None) or {}
            metadata = getattr(raw, "response_metadata", None) or {}
            usage_ok = self.budget.record_usage(usage, truncated=metadata.get("finish_reason") == "length")
            try:
                proposal = json.loads(content)
            except (ValueError, TypeError):
                proposal = None
            proposal, normalizations = normalize_schema_title(proposal, schema)
            receipt = {"schema_version": "finresearchops.research-model-response/v4",
                       "stage": stage, "content": content, "proposal": proposal,
                       "tool_calls": getattr(raw, "tool_calls", []),
                       "invalid_tool_calls": getattr(raw, "invalid_tool_calls", []),
                       "usage": usage, "parse_route": "JSON_MODE_CONTENT",
                       "finish_reason": metadata.get("finish_reason"),
                       "parse_error": type(proposal) is not dict, "normalizations": normalizations}
            write_once(self.trace_root / f"call-{number:03d}-response.json", canonical_json_bytes(receipt))
            if not usage_ok:
                raise ValueError("MODEL_OUTPUT_LIMIT_OR_TRUNCATION")
            if receipt["parse_error"] or type(proposal) is not dict:
                raise ValueError("RESEARCH_MODEL_JSON_REQUIRED")
            self._calls.append({"stage": stage, "request": clone(payload), "proposal": clone(proposal)})
            return proposal
        except Exception as exc:
            write_once(self.trace_root / f"call-{number:03d}-error.json", canonical_json_bytes({
                "schema_version": "finresearchops.research-model-error/v1",
                "stage": stage, "error_type": type(exc).__name__, "budget": self.budget.receipt()}))
            raise

    def run(self, request, evidence, lookup):
        """Run two isolated drafts, one bounded lookup and a source-aware synthesis."""
        from langgraph.graph import END, START, StateGraph
        if self._calls or self.budget.calls:
            raise ValueError("RESEARCHER_INSTANCE_ALREADY_USED")

        def first(stage):
            def node(state):
                # The other draft and any old case are deliberately absent.
                payload = {"request": clone(state["request"]), "evidence": model_evidence_view(state["evidence"])}
                draft = self._draft(stage, payload)
                validate_proposal(draft, state["evidence"])
                return {stage: draft}
            return node

        def retrieve(state):
            drivers = selected_drivers(state["analysis"], state["challenge"], state["evidence"])
            return {"evidence": lookup(drivers) if drivers else state["evidence"]}

        def synthesize(state):
            payload = {"request": clone(state["request"]), "evidence": model_evidence_view(state["evidence"]),
                "required_evidence_ids": required_review_references(state["challenge"], state["evidence"])}
            decision = self._draft("synthesis", payload)
            validate_proposal(decision, state["evidence"], final=True, challenge=state["challenge"])
            return {"decision": decision}

        graph = StateGraph(_State)
        graph.add_node("analysis", first("analysis"))
        graph.add_node("challenge", first("challenge"))
        graph.add_node("lookup", retrieve)
        graph.add_node("synthesis", synthesize)
        # Sequential scheduling controls request load; isolation is enforced by
        # the node payload, not by claiming two copies of one model are independent.
        graph.add_edge(START, "analysis")
        graph.add_edge("analysis", "challenge")
        graph.add_edge("challenge", "lookup")
        graph.add_edge("lookup", "synthesis")
        graph.add_edge("synthesis", END)
        from langsmith import tracing_context
        try:
            with tracing_context(enabled=False):
                state = graph.compile().invoke({"request": clone(request), "evidence": clone(evidence)},
                                               config={"recursion_limit": 8})
        finally:
            if self._http_client is not None:
                self._http_client.close()
                self._http_client = None
                self._client = None
        return {"analysis": state["analysis"], "challenge": state["challenge"],
                "decision": state["decision"], "calls": clone(self._calls),
                "budget": self.budget.receipt(), "upstream_commit": UPSTREAM_COMMIT,
                "runtime_kind": "SCRIPTED_INTEGRATION" if self._injected_client else "TRADINGAGENTS_MODEL_CLIENT"}

    def explain_update(self, payload):
        if len(self._calls) != 3 or self.budget.calls != 3:
            raise ValueError("UPDATE_REQUIRES_SAVED_RESEARCH_CALLS")
        from langsmith import tracing_context
        try:
            with tracing_context(enabled=False):
                proposal = self._draft("update", clone(payload))
            validate_update(proposal, payload)
            return {"proposal": clone(proposal), "call": clone(self._calls[-1]), "budget": self.budget.receipt()}
        finally:
            if self._http_client is not None:
                self._http_client.close()
                self._http_client = None
                self._client = None

    def run_baseline(self, command, output_root):
        from finauditgate.adapters.tradingagents_baseline import run
        if self.budget.calls:
            raise ValueError("RESEARCHER_INSTANCE_ALREADY_USED")
        self._preflight()  # Reject missing credentials before any market lookup.
        return run(command, output_root, self)
