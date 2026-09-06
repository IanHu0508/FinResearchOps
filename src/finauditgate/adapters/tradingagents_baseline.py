"""Unmodified upstream graph, bounded and captured as a functional baseline."""

import json

from finauditgate.core.artifacts import canonical_json_bytes, write_once


def run(command, output_root, owner):
    from finauditgate.adapters.model_http import model_http_client
    from langchain_core.callbacks import BaseCallbackHandler
    from langsmith import tracing_context
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    class Capture(BaseCallbackHandler):
        raise_error = True

        def __init__(self):
            self.numbers = {}

        def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            payload = [[{"role": message.type, "content": message.content,
                         "tool_calls": getattr(message, "tool_calls", [])} for message in group]
                       for group in messages]
            prompt = json.dumps(payload, ensure_ascii=False)
            number = owner.budget.reserve(prompt)
            self.numbers[str(run_id)] = number
            write_once(owner.trace_root / f"call-{number:03d}-request.json", canonical_json_bytes({
                "schema_version": "finresearchops.native-model-request/v1", "messages": payload}))

        def on_llm_end(self, response, *, run_id, **kwargs):
            number = self.numbers[str(run_id)]
            messages = [generation.message for row in response.generations for generation in row]
            usage = next((message.usage_metadata for message in messages if getattr(message, "usage_metadata", None)), {})
            truncated = any((getattr(message, "response_metadata", None) or {}).get("finish_reason") == "length" for message in messages)
            usage_ok = owner.budget.record_usage(usage, truncated=truncated)
            write_once(owner.trace_root / f"call-{number:03d}-response.json", canonical_json_bytes({
                "schema_version": "finresearchops.native-model-response/v2", "usage": usage,
                "messages": [{"content": message.content, "tool_calls": getattr(message, "tool_calls", []),
                              "finish_reason": (getattr(message, "response_metadata", None) or {}).get("finish_reason")}
                             for message in messages]}))
            if not usage_ok:
                raise ValueError("MODEL_OUTPUT_LIMIT_OR_TRUNCATION")

        def on_llm_error(self, error, *, run_id, **kwargs):
            number = self.numbers.get(str(run_id))
            if number is not None:
                write_once(owner.trace_root / f"call-{number:03d}-error.json", canonical_json_bytes({
                    "schema_version": "finresearchops.native-model-error/v1", "error_type": type(error).__name__}))

    config = DEFAULT_CONFIG.copy()
    config.update({"llm_provider": owner.provider, "deep_think_llm": owner.model,
        "quick_think_llm": owner.model, "output_language": "简体中文", "llm_max_retries": 0,
        "backend_url": None,
        "max_tokens": owner.budget.max_output_tokens, "max_recur_limit": 40,
        "max_debate_rounds": 1, "max_risk_discuss_rounds": 1,
        "checkpoint_enabled": False, "memory_log_path": None,
        "results_dir": str(output_root / "reports"), "data_cache_dir": str(output_root / "cache"),
        "data_vendors": {"core_stock_apis": "yfinance", "technical_indicators": "yfinance",
                         "fundamental_data": "yfinance", "news_data": "yfinance"}, "tool_vendors": {}})
    import yfinance as yf
    yf.set_tz_cache_location(str(output_root / "yfinance-cache"))
    with_client = model_http_client(owner.provider, owner.budget.max_output_tokens,
        trace_root=owner.trace_root, reasoning_effort=owner.reasoning_effort)

    class BoundedGraph(TradingAgentsGraph):
        def _get_provider_kwargs(self):
            return {**super()._get_provider_kwargs(), "timeout": 120, "http_client": with_client}

    try:
        with tracing_context(enabled=False):
            graph = BoundedGraph(selected_analysts=["fundamentals", "market"], config=config,
                                       callbacks=[Capture()])
            state, signal = graph.propagate(command.symbol, command.as_of.isoformat())
        reports = {key: state.get(key, "") for key in (
            "fundamentals_report", "market_report", "investment_plan",
            "trader_investment_plan", "final_trade_decision")}
        result = {"schema_version": "finresearchops.tradingagents-baseline/v1",
                  "symbol": command.symbol, "as_of": command.as_of.isoformat(), "reports": reports,
                  "signal": signal, "budget": owner.budget.receipt(), "provider": owner.provider,
                  "model": owner.model, "upstream_commit": "2448d0a12576f9b2ddcd5980a0630833423d1e1b",
                  "financial_audit": "NOT_APPLIED_NATIVE_BASELINE", "review_status": "AWAITING_REVIEW"}
        write_once(output_root / "result.json", canonical_json_bytes(result))
        return result
    except Exception as exc:
        write_once(output_root / "failure.json", canonical_json_bytes({
            "schema_version": "finresearchops.tradingagents-baseline-failure/v1",
            "symbol": command.symbol, "as_of": command.as_of.isoformat(),
            "error_type": type(exc).__name__, "budget": owner.budget.receipt()}))
        raise
    finally:
        with_client.close()
