"""Synthetic model/data seams for executing the pinned native TradingAgents graph.

These helpers replace external providers only. GraphSetup, analyst factories,
ToolNode, conditional routing, structured renderers and propagate remain real.
Model replies deliberately omit input evidence so propagation assertions cannot
pass merely because a synthetic analyst copied an audit packet into its report.
"""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import date, timedelta
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from pydantic import Field


class NativeSyntheticLLM(BaseChatModel):
    """Chat-model fake with shared capture across tool/schema bindings.

    ``requests`` records every actual model input (including the complete
    instrument context), inferred native node, bound tools and structured schema.
    ``tool_requests`` records requested calls; use the data fixture's ``calls``
    to prove that ToolNode actually executed them.
    """

    symbol: str = "AURORA"
    as_of: str = "2026-03-02"
    native_offline: bool = True
    requests: list[dict] = Field(default_factory=list)
    tool_requests: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "native-synthetic-no-network"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tuple(tools), **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return self.bind(synthetic_schema=schema) | RunnableLambda(
            lambda message: schema.model_validate_json(message.content))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = "\n".join(str(message.content) for message in messages)
        names = [value.name for value in kwargs.get("tools", ())]
        schema = kwargs.get("synthetic_schema")
        schema_name = schema.__name__ if schema is not None else None
        node = self._node(text, names, schema_name)
        self.requests.append({
            "node": node,
            "messages": [message.model_dump(mode="json") for message in messages],
            "text": text,
            "bound_tools": names,
            "schema": schema_name,
        })
        if schema is not None:
            return self._result(json.dumps(self._structured(schema_name)))
        if names:
            completed = {message.name for message in messages if message.type == "tool"}
            schedule = ("get_fundamentals", "get_balance_sheet", "get_cashflow",
                        "get_income_statement") if node == "Fundamentals Analyst" else (
                            "get_stock_data", "get_indicators", "get_verified_market_snapshot")
            for name in schedule:
                if name not in names:
                    raise AssertionError(f"NATIVE_TOOL_BINDING_MISSING:{name}")
                if name not in completed:
                    call = {"name": name, "args": self._tool_args(name),
                            "id": f"synthetic-call-{len(self.tool_requests) + 1}",
                            "type": "tool_call"}
                    self.tool_requests.append(deepcopy(call))
                    return self._result("", tool_calls=[call])
        return self._result(f"Synthetic {node} response. Further review required.")

    @staticmethod
    def _result(content, **kwargs):
        message = AIMessage(content=content, usage_metadata={"input_tokens": 100,
                            "output_tokens": 50, "total_tokens": 150}, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def _node(text, names, schema_name):
        structured = {"ResearchPlan": "Research Manager", "TraderProposal": "Trader",
                      "PortfolioDecision": "Portfolio Manager"}
        if schema_name in structured:
            return structured[schema_name]
        if "get_fundamentals" in names:
            return "Fundamentals Analyst"
        if "get_stock_data" in names:
            return "Market Analyst"
        for marker, node in (
            ("You are a Bull Analyst", "Bull Researcher"),
            ("You are a Bear Analyst", "Bear Researcher"),
            ("As the Aggressive Risk Analyst", "Aggressive Analyst"),
            ("As the Conservative Risk Analyst", "Conservative Analyst"),
            ("As the Neutral Risk Analyst", "Neutral Analyst"),
        ):
            if marker in text:
                return node
        raise AssertionError(f"UNSUPPORTED_NATIVE_SYNTHETIC_NODE:{schema_name}")

    def _structured(self, schema_name):
        values = {
            "ResearchPlan": {"recommendation": "Hold", "rationale": "Synthetic balanced evidence.",
                             "strategic_actions": "Require review before any position change."},
            "TraderProposal": {"action": "Hold", "reasoning": "Synthetic proposal requires review."},
            "PortfolioDecision": {"rating": "Hold", "executive_summary": "Synthetic draft; await review.",
                                  "investment_thesis": "Synthetic evidence is insufficient for execution."},
        }
        if schema_name not in values:
            raise AssertionError(f"UNSUPPORTED_NATIVE_SYNTHETIC_SCHEMA:{schema_name}")
        return values[schema_name]

    def _tool_args(self, name):
        if name == "get_stock_data":
            return {"symbol": self.symbol,
                    "start_date": str(date.fromisoformat(self.as_of) - timedelta(days=30)),
                    "end_date": self.as_of}
        if name == "get_indicators":
            return {"symbol": self.symbol, "indicator": "rsi", "curr_date": self.as_of}
        if name == "get_verified_market_snapshot":
            return {"symbol": self.symbol, "curr_date": self.as_of}
        result = {"ticker": self.symbol, "curr_date": self.as_of}
        if name != "get_fundamentals":
            result["freq"] = "annual"
        return result


def synthetic_native_tools():
    """Return ``SimpleNamespace(tools={name: tool}, calls=[])``.

    Tool signatures match the pinned upstream. Every output is explicitly
    synthetic; nothing reads market providers, filesystem data or API keys.
    """
    calls = []

    def response(name, arguments, payload):
        calls.append({"name": name, "arguments": deepcopy(arguments)})
        return json.dumps({"fixture": "SYNTHETIC_ONLY", **payload}, sort_keys=True)

    @tool
    def get_fundamentals(ticker: str, curr_date: str) -> str:
        """Return synthetic company metadata for native graph tests."""
        return response("get_fundamentals", {"ticker": ticker, "curr_date": curr_date}, {"ticker": ticker,
                        "as_of": curr_date, "company_name": "Aurora Synthetic Company"})

    @tool
    def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        """Return synthetic balance-sheet data for native graph tests."""
        return response("get_balance_sheet", {"ticker": ticker, "freq": freq, "curr_date": curr_date}, {"ticker": ticker,
                        "as_of": curr_date, "currency": "CNY", "assets": "150000"})

    @tool
    def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        """Return synthetic operating cash-flow data for native graph tests."""
        return response("get_cashflow", {"ticker": ticker, "freq": freq, "curr_date": curr_date}, {"ticker": ticker,
                        "as_of": curr_date, "currency": "CNY", "operating_cash_flow": "12500"})

    @tool
    def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        """Return synthetic net-income data for native graph tests."""
        return response("get_income_statement", {"ticker": ticker, "freq": freq, "curr_date": curr_date}, {"ticker": ticker,
                        "as_of": curr_date, "currency": "CNY", "net_income": "10000"})

    @tool
    def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
        """Return synthetic OHLCV data for native graph tests."""
        return response("get_stock_data", {"symbol": symbol, "start_date": start_date, "end_date": end_date}, {"symbol": symbol, "currency": "USD",
                        "date": end_date, "open": "20", "high": "21", "low": "19",
                        "close": "20.5", "volume": "10000"})

    @tool
    def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
        """Return a synthetic technical indicator for native graph tests."""
        return response("get_indicators", {"symbol": symbol, "indicator": indicator, "curr_date": curr_date,
                        "look_back_days": look_back_days}, {"symbol": symbol, "date": curr_date,
                        "indicator": indicator, "value": "50"})

    @tool
    def get_verified_market_snapshot(symbol: str, curr_date: str, look_back_days: int = 30) -> str:
        """Return a synthetic verification snapshot for native graph tests."""
        return response("get_verified_market_snapshot", {"symbol": symbol, "curr_date": curr_date,
                        "look_back_days": look_back_days}, {"symbol": symbol,
                        "as_of": curr_date, "latest_date": curr_date,
                        "currency": "USD", "close": "20.5", "rsi": "50"})

    values = (get_fundamentals, get_balance_sheet, get_cashflow, get_income_statement,
              get_stock_data, get_indicators, get_verified_market_snapshot)
    return SimpleNamespace(tools={value.name: value for value in values}, calls=calls)


@contextmanager
def patched_native_runtime(llm, data=None):
    """Patch external seams and reject network; yield the synthetic data capture.

    Construct and execute the real graph *inside* this context. The caller must
    supply temporary cache/results paths and disable persistent memory and
    checkpointing in its normal native graph configuration.
    """
    data = synthetic_native_tools() if data is None else data
    modules = [importlib.import_module(name) for name in (
        "tradingagents.graph.trading_graph",
        "tradingagents.agents.utils.agent_utils",
        "tradingagents.agents.analysts.fundamentals_analyst",
        "tradingagents.agents.analysts.market_analyst",
    )]
    with ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "httpx.Client.send",
                       "requests.sessions.Session.request", "curl_cffi.requests.Session.request"):
            stack.enter_context(patch(target, side_effect=AssertionError("NATIVE_TEST_NETWORK_FORBIDDEN")))
        def create_client(**kwargs):
            callbacks = list(llm.callbacks or ())
            for callback in kwargs.get("callbacks", ()):
                if callback not in callbacks:
                    callbacks.append(callback)
            llm.callbacks = callbacks
            return SimpleNamespace(get_llm=lambda: llm)

        stack.enter_context(patch.object(modules[0], "create_llm_client", side_effect=create_client))
        identity = {"company_name": "Aurora Synthetic Company", "sector": "Synthetic sector",
                    "industry": "Synthetic industry", "exchange": "SYNTHETIC", "quote_type": "EQUITY"}
        for module in modules:
            if hasattr(module, "resolve_instrument_identity"):
                stack.enter_context(patch.object(module, "resolve_instrument_identity",
                                                 return_value=deepcopy(identity)))
            for name, value in data.tools.items():
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, value))
        yield data
