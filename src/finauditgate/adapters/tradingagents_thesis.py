"""Native research graph with independent drafts and a nonblocking review Agent."""

from copy import deepcopy
from importlib.metadata import version
import json
from pathlib import Path

from finauditgate.adapters.thesis_protocol import ThesisSession, validate_sources
from finauditgate.adapters.thesis_responses import CompletedCalls
from finauditgate.adapters.tradingagents_native import (
    REPORT_FIELDS, UPSTREAM_COMMIT, _capture_handler, _topology,
)
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.research import thesis_request


class ThesisResearcher:
    def __init__(self, *, model="deepseek-flash", live=False, budget=None, reasoning_effort="high", resume_from=None, reassess_final=False):
        if live and budget is None:
            raise ValueError("THESIS_LIVE_BUDGET_REQUIRED")
        self.model, self.live, self.budget, self.effort = model, live, budget, reasoning_effort
        self.resume_from = resume_from
        self.reassess_final = reassess_final

    def run_thesis(self, command, output_root, *, save_main):
        from langsmith import tracing_context
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        if version("tradingagents") != "0.4.0":
            raise ValueError("NATIVE_UPSTREAM_VERSION_MISMATCH")
        root = Path(output_root)
        request = thesis_request(command)
        bundle = deepcopy(command.sources) if command.sources is not None else {
            "schema_version": "finresearchops.thesis-sources/v2", "symbol": request["symbol"],
            "as_of": request["as_of"], "identity": {}, "sources": []}
        if command.sources is not None:
            validate_sources(bundle, request)
        if self.resume_from is not None and command.sources is None:
            raise ValueError("THESIS_RESUME_REQUIRES_FROZEN_SOURCES")
        completed = CompletedCalls(self.resume_from, request, bundle, self.model, reassess_final=self.reassess_final)
        write_once(root / "request.json", canonical_json_bytes({"request": request, "sources": bundle}))
        trace_root = root / "model-traces"
        capture = _capture_handler(self.budget if self.live else None, trace_root)
        http = None
        if self.live:
            from finauditgate.adapters.model_http import model_http_client
            http = model_http_client("deepseek", self.budget.max_output_tokens, trace_root=trace_root,
                                     reasoning_effort=self.effort, timeout_seconds=600)
        config = deepcopy(DEFAULT_CONFIG)
        config.update({"llm_provider": "deepseek", "deep_think_llm": self.model,
            "quick_think_llm": self.model, "backend_url": None, "output_language": "简体中文",
            "llm_max_retries": 0, "max_tokens": self.budget.max_output_tokens if self.live else 8192,
            "max_recur_limit": 80, "max_debate_rounds": 2, "max_risk_discuss_rounds": 1,
            "checkpoint_enabled": False, "memory_log_path": None,
            "results_dir": str(root / "native-reports"), "data_cache_dir": str(root / "native-cache"),
            "data_vendors": {"core_stock_apis": "yfinance", "technical_indicators": "yfinance",
                             "fundamental_data": "yfinance", "news_data": "yfinance"}, "tool_vendors": {}})
        if self.live and command.sources is None:
            import yfinance as yf
            yf.set_tz_cache_location(str(root / "yfinance-cache"))
        live = self.live

        class ResearchGraph(TradingAgentsGraph):
            def _get_provider_kwargs(self):
                values = super()._get_provider_kwargs()
                return {**values, "http_client": http, "timeout": 600} if live else values

            def resolve_instrument_context(self, ticker, asset_type="stock"):
                if command.sources is not None:
                    from tradingagents.agents.utils.agent_utils import build_instrument_context
                    return build_instrument_context(ticker, asset_type, bundle["identity"])
                return super().resolve_instrument_context(ticker, asset_type)

        try:
            with tracing_context(enabled=False):
                graph = ResearchGraph(selected_analysts=["fundamentals", "market"], config=config, callbacks=[capture])
                if not live and not all(getattr(model, "native_offline", False) is True for model
                                       in (graph.quick_thinking_llm, graph.deep_thinking_llm)):
                    raise ValueError("NATIVE_OFFLINE_RUNTIME_REQUIRED")
                before = _topology(graph.graph)
                session = ThesisSession(request, bundle, graph.deep_thinking_llm, completed=completed, capture=capture)
                session.install(graph)
                if _topology(graph.graph) != before:
                    raise ValueError("THESIS_TOPOLOGY_CHANGED")
                graph.graph = graph.graph.with_config(callbacks=[capture], max_concurrency=1)
                state, upstream_signal = graph.propagate(command.symbol, command.as_of.isoformat())
                signal = session.final["decision"]["rating"]
                write_once(root / "native-signal.json", canonical_json_bytes({
                    "upstream_text_extraction": upstream_signal, "structured_final_rating": signal,
                    "selection": "STRUCTURED_FINAL_DECISION"}))
                validate_sources(session.bundle, request)
                record = {"schema_version": "finresearchops.thesis-case/v10", "request": request,
                    "sensitivity_policy": "DECLARED_SCENARIOS_REPORT_ONLY",
                    "status": "COMPLETED", "review_status": "AWAITING_REVIEW", "upstream_commit": UPSTREAM_COMMIT,
                    "runtime_kind": "REAL_MODEL" if live else "OFFLINE_SYNTHETIC", "model": self.model,
                    "source_bundle": session.bundle, "topology": before,
                    "reports": {key: state[key] for key in REPORT_FIELDS}, "signal": signal,
                    "initial": session.initial, "revisions": session.revisions,
                    "updated_claims": session.updated_claims(), "research_evaluation": session.research,
                    "execution_review": session.execution, "risk_briefs": session.risks,
                    "independent_assessment": session.independent,
                    "forward_draft": session.forward_draft, "forward_calculations": session.forward_calculations,
                    "rating_comparison": {"before": session.independent["decision"]["rating"],
                        "after": session.final["decision"]["rating"],
                        "changed": session.independent["decision"]["rating"] != session.final["decision"]["rating"]},
                    "final_assessment": session.final, "exchanges": session.exchanges,
                    "model_calls": capture.model_calls, "tool_calls": capture.tool_calls,
                    "budget": self.budget.receipt() if live else None,
                    "financial_gate": "NOT_REQUIRED", "automatic_trading": False}
                if completed.receipt is not None:
                    record["reused_calls"] = {**completed.receipt, "used_calls": completed.used}
                # Break every reference to the mutable session/callback lists
                # before the optional review makes another model request.
                record = json.loads(canonical_json_bytes(record))
                write_once(root / "main-result.json", canonical_json_bytes(record))
                saved_report = save_main(deepcopy(record))
                main_hash = sha256_hex(canonical_json_bytes(record))
                review = {"schema_version": "finresearchops.thesis-review/v1", "main_sha256": main_hash,
                          "status": "DEFERRED", "reason": "DISABLED", "findings": []}
                if command.review:
                    try:
                        result = session.review(deepcopy(record), {"callbacks": [capture],
                            "metadata": {"langgraph_node": "Data Review Agent"}}, saved_report)
                        review.update(status="COMPLETED", reason="MODEL_REVIEW_NOT_CERTIFICATION", **result,
                                      exchange=deepcopy(session.exchanges[-1]), model_call=deepcopy(capture.model_calls[-1]))
                    except Exception as exc:
                        review.update(status="PARTIAL", reason="REVIEW_FAILED", error_type=type(exc).__name__)
                if sha256_hex(canonical_json_bytes(record)) != main_hash:
                    raise ValueError("THESIS_REVIEW_CHANGED_MAIN")
                review["budget_total"] = deepcopy(self.budget.receipt()) if live else None
                write_once(root / "review-result.json", canonical_json_bytes(review))
                return record, review
        finally:
            if http is not None:
                http.close()
            write_once(root / "runtime-receipt.json", canonical_json_bytes({
                "schema_version": "finresearchops.thesis-runtime/v1",
                "budget": self.budget.receipt() if live else None,
                "reused_calls": {**completed.receipt, "used_calls": completed.used} if completed.receipt else None,
                "model_calls": capture.model_calls, "tool_calls": capture.tool_calls,
                "node_calls": capture.node_calls}))
