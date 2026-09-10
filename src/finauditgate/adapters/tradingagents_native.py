"""Run the pinned native graph with source-bound evidence and captured I/O.

The upstream graph, agent factories, routing and propagate method stay intact.
Live execution is explicit; synthetic execution requires an offline marker.
"""

from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
from threading import Lock

from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once


UPSTREAM_COMMIT = "2448d0a12576f9b2ddcd5980a0630833423d1e1b"
MAX_MODEL_CALLS = 24
MAX_TOOL_CALLS = 48
MAX_NODE_CALLS = 96
MAX_PACKET_BYTES = 256 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_TRACE_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 40 * 1024 * 1024
REPORT_FIELDS = ("fundamentals_report", "market_report", "investment_plan",
                 "trader_investment_plan", "final_trade_decision")


def build_audit_block(packet: dict) -> str:
    """Render the exact data block used for both injection and verification."""
    if not isinstance(packet, dict):
        raise ValueError("NATIVE_AUDIT_PACKET_INVALID")
    raw = canonical_json_bytes(packet)
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError("NATIVE_AUDIT_PACKET_TOO_LARGE")
    return "BEGIN_FIN_AUDIT\n" + raw.decode("utf-8") + "\nEND_FIN_AUDIT"


def _json_safe(value):
    """Copy data and messages without serializing clients or their settings."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("NATIVE_TRACE_KEY_INVALID")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]

    from langchain_core.messages import BaseMessage

    if isinstance(value, BaseMessage):
        result = {"type": value.type, "content": _json_safe(value.content),
                  "tool_calls": _json_safe(getattr(value, "tool_calls", [])),
                  "id": value.id, "name": value.name}
        usage = getattr(value, "usage_metadata", None)
        if usage is not None:
            result["usage"] = _json_safe(usage)
        finish_reason = (value.response_metadata or {}).get("finish_reason")
        if finish_reason is not None:
            result["finish_reason"] = _json_safe(finish_reason)
        tool_call_id = getattr(value, "tool_call_id", None)
        if tool_call_id is not None:
            result["tool_call_id"] = tool_call_id
            result["status"] = _json_safe(value.status)
        return result
    raise ValueError("NATIVE_TRACE_TYPE_INVALID")


def _topology(graph):
    drawable = graph.get_graph()
    return {
        "nodes": sorted(drawable.nodes),
        "edges": [{"source": source, "target": target, "conditional": conditional}
                  for source, target, conditional in sorted(
                      (edge.source, edge.target, bool(edge.conditional))
                      for edge in drawable.edges)],
    }


def _capture_handler(budget=None, trace_root=None):
    from langchain_core.callbacks import BaseCallbackHandler

    class Capture(BaseCallbackHandler):
        raise_error = True

        def __init__(self):
            self.model_calls = []
            self.tool_calls = []
            self.node_calls = []
            self._models = {}
            self._tools = {}
            self._nodes = {}
            self._sizes = {}
            self._total_bytes = 0
            self._lock = Lock()
            self._numbers = {}

        def _start(self, rows, index, limit, run_id, row):
            with self._lock:
                key = str(run_id)
                if len(rows) >= limit or key in index:
                    raise ValueError("NATIVE_TRACE_CALL_LIMIT")
                snapshot = _json_safe({"run_id": key, **row})
                size = len(canonical_json_bytes(snapshot))
                if size > MAX_RECORD_BYTES or self._total_bytes + size > MAX_TRACE_BYTES:
                    raise ValueError("NATIVE_TRACE_SIZE_LIMIT")
                index[key] = snapshot
                self._sizes[key] = size
                self._total_bytes += size
                rows.append(snapshot)

        def _end(self, index, run_id, **fields):
            with self._lock:
                key = str(run_id)
                if key not in index:
                    return
                row = index[key]
                updated = {**row, **_json_safe(fields)}
                size = len(canonical_json_bytes(updated))
                total = self._total_bytes - self._sizes[key] + size
                if size > MAX_RECORD_BYTES or total > MAX_TRACE_BYTES:
                    raise ValueError("NATIVE_TRACE_SIZE_LIMIT")
                row.update(updated)
                self._sizes[key] = size
                self._total_bytes = total

        def on_chat_model_start(self, serialized, messages, *, run_id,
                                metadata=None, **kwargs):
            node = (metadata or {}).get("langgraph_node")
            if not isinstance(node, str):
                raise ValueError("NATIVE_MODEL_NODE_MISSING")
            if budget is not None:
                payload = _json_safe(messages)
                number = budget.reserve(canonical_json_bytes(payload).decode())
                self._numbers[str(run_id)] = number
                write_once(trace_root / f"call-{number:03d}-request.json", canonical_json_bytes({
                    "schema_version":"finresearchops.native-live-request/v1", "node":node,"messages":payload}))
            self._start(self.model_calls, self._models, MAX_MODEL_CALLS, run_id,
                        {"node": node, "messages": messages})

        def on_llm_end(self, response, *, run_id, **kwargs):
            output = [generation.message for group in response.generations
                      for generation in group]
            self._end(self._models, run_id, output=output)
            if budget is not None:
                usage = next((m.usage_metadata for m in output if getattr(m,'usage_metadata',None)), {})
                truncated = any((m.response_metadata or {}).get('finish_reason') == 'length' for m in output)
                ok = budget.record_usage(usage, truncated=truncated)
                write_once(trace_root / f"call-{self._numbers[str(run_id)]:03d}-response.json", canonical_json_bytes({
                    "schema_version":"finresearchops.native-live-response/v1", "messages":_json_safe(output),
                    "usage":_json_safe(usage),"budget":budget.receipt()}))
                if not ok:
                    raise ValueError("NATIVE_MODEL_OUTPUT_LIMIT_OR_TRUNCATION")

        def on_llm_error(self, error, *, run_id, **kwargs):
            self._end(self._models, run_id, error_type=type(error).__name__)

        def on_tool_start(self, serialized, input_str, *, run_id, metadata=None,
                          inputs=None, **kwargs):
            self._start(self.tool_calls, self._tools, MAX_TOOL_CALLS, run_id,
                        {"node": (metadata or {}).get("langgraph_node"),
                         "name": (serialized or {}).get("name"),
                         "input": inputs if inputs is not None else input_str})

        def on_tool_end(self, output, *, run_id, **kwargs):
            self._end(self._tools, run_id, output=output)

        def on_tool_error(self, error, *, run_id, **kwargs):
            self._end(self._tools, run_id, error_type=type(error).__name__)

        def on_chain_start(self, serialized, inputs, *, run_id, metadata=None,
                           name=None, **kwargs):
            node = (metadata or {}).get("langgraph_node")
            if node is not None and name == node:
                self._start(self.node_calls, self._nodes, MAX_NODE_CALLS, run_id,
                            {"node": node, "input": inputs})

        def on_chain_end(self, outputs, *, run_id, **kwargs):
            self._end(self._nodes, run_id, output=outputs)

        def on_chain_error(self, error, *, run_id, **kwargs):
            self._end(self._nodes, run_id, error_type=type(error).__name__)

    return Capture()


class NativeAuditAdapter:
    """Execute the native graph in a synthetic or explicitly enabled live runtime."""

    def __init__(self, config=None, *, live=False, budget=None):
        self._config = deepcopy(config or {})
        self._live = live
        self._budget = budget
        if live and budget is None:
            raise ValueError("NATIVE_LIVE_BUDGET_REQUIRED")

    def run_native(self, request: dict, packet: dict, output_root: Path, *, judgment=None) -> dict:
        from langsmith import tracing_context
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        if version("tradingagents") != "0.4.0":
            raise ValueError("NATIVE_UPSTREAM_VERSION_MISMATCH")
        packet_snapshot = deepcopy(packet)
        typed=packet_snapshot.get('schema_version')=='finresearchops.native-audit-packet/v5'
        if typed != (judgment is not None):raise ValueError('NATIVE_JUDGMENT_REVIEWER_REQUIRED')
        if self._live and packet_snapshot.get('schema_version') not in ('finresearchops.native-audit-packet/v2','finresearchops.native-audit-packet/v3','finresearchops.native-audit-packet/v4','finresearchops.native-audit-packet/v5'):
            raise ValueError("NATIVE_LIVE_SECURITY_MARKET_REQUIRED")
        block = build_audit_block(packet_snapshot)
        request_snapshot = _json_safe(request)
        root = Path(output_root)
        config = deepcopy(DEFAULT_CONFIG)
        config.update({"llm_provider": "deepseek", "deep_think_llm": "deepseek-v4-pro",
                       "quick_think_llm": "deepseek-v4-pro", "backend_url": None})
        config.update(self._config)
        config.update({
            "output_language": "简体中文", "llm_max_retries": 0,
            "max_tokens": 8192, "max_recur_limit": 40,
            "max_debate_rounds": 1, "max_risk_discuss_rounds": 1,
            "checkpoint_enabled": False, "memory_log_path": None,
            "results_dir": str(root / "native-reports"),
            "data_cache_dir": str(root / "native-cache"),
        })
        if self._live:
            config.update(max_tokens=self._budget.max_output_tokens,max_recur_limit=80,
                data_vendors={"core_stock_apis":"yfinance","technical_indicators":"yfinance",
                              "fundamental_data":"yfinance","news_data":"yfinance"},tool_vendors={})
            import yfinance as yf
            yf.set_tz_cache_location(str(root/'yfinance-cache'))
        trace_root = root/'model-traces'
        capture = _capture_handler(self._budget if self._live else None, trace_root)
        http = None
        live = self._live
        filtered = packet_snapshot.get('schema_version') in ('finresearchops.native-audit-packet/v3','finresearchops.native-audit-packet/v4','finresearchops.native-audit-packet/v5')
        vendor_observations = []
        vendor_lock = Lock()
        if live:
            from finauditgate.adapters.model_http import model_http_client
            http = model_http_client('deepseek',self._budget.max_output_tokens,trace_root=trace_root,
                                     reasoning_effort=self._config.get('reasoning_effort','high'),timeout_seconds=600)

        class AuditedGraph(TradingAgentsGraph):
            def _create_tool_nodes(self):
                nodes = super()._create_tool_nodes()
                if filtered:
                    from langgraph.prebuilt import ToolNode
                    from finauditgate.adapters.native_contract import fundamental_tool_view
                    def guarded(original):
                        def invoke(**arguments):
                            # Validate scope before any external query. Keep the native
                            # tool schema and raw return, but expose checked fields only.
                            fundamental_tool_view(original.name,arguments,packet_snapshot,'0'*64)
                            raw = original.func(**arguments)
                            if not isinstance(raw,str) or not raw.strip():
                                raise ValueError('NATIVE_VENDOR_RESPONSE_INVALID')
                            digest = sha256_hex(raw.encode())
                            projected = fundamental_tool_view(original.name,arguments,packet_snapshot,digest)
                            row = {'name':original.name,'input':_json_safe(arguments),'raw_output':raw,
                                   'raw_sha256':digest,'model_output':projected}
                            with vendor_lock:
                                if len(vendor_observations) >= MAX_TOOL_CALLS:
                                    raise ValueError('NATIVE_VENDOR_CALL_LIMIT')
                                write_once(root/'vendor-observations'/f'{len(vendor_observations)+1:03d}.json',canonical_json_bytes(row))
                                vendor_observations.append(row)
                            return projected
                        return original.model_copy(update={'func':invoke,'coroutine':None})
                    nodes['fundamentals'] = ToolNode([guarded(t) for t in nodes['fundamentals'].tools_by_name.values()])
                return nodes

            def _get_provider_kwargs(self):
                values = super()._get_provider_kwargs()
                return {**values,'http_client':http,'timeout':600} if live else values

            def resolve_instrument_context(self, ticker, asset_type="stock"):
                if not live and not all(getattr(model, "native_offline", False) is True for model
                           in (self.quick_thinking_llm, self.deep_thinking_llm)):
                    raise ValueError("NATIVE_OFFLINE_RUNTIME_REQUIRED")
                if live:
                    from tradingagents.agents.utils.agent_utils import build_instrument_context
                    identity = packet_snapshot['security_market']['identity']
                    if ticker != identity['symbol']:
                        raise ValueError('NATIVE_SECURITY_SYMBOL_MISMATCH')
                    original = build_instrument_context(ticker,asset_type,{'company_name':identity['issuer'],
                        'exchange':identity['exchange'],'quote_type':'EQUITY'})
                else:
                    original = super().resolve_instrument_context(ticker, asset_type)
                return original + "\n\n" + block

        try:
            with tracing_context(enabled=False):
                graph = AuditedGraph(selected_analysts=["fundamentals", "market"], config=config, callbacks=[capture])
                if not live and not all(getattr(model, "native_offline", False) is True for model
                           in (graph.quick_thinking_llm, graph.deep_thinking_llm)):
                    raise ValueError("NATIVE_OFFLINE_RUNTIME_REQUIRED")
                before = _topology(graph.graph)
                if typed:
                    from finauditgate.adapters.native_judgment import install
                    install(graph,packet_snapshot,judgment)
                    if _topology(graph.graph)!=before:raise ValueError('NATIVE_JUDGMENT_TOPOLOGY_CHANGED')
                # Vendor tools share yfinance's SQLite and CSV caches. Preserve
                # every native call, but avoid concurrent first-use cache writes.
                graph.graph = graph.graph.with_config(callbacks=[capture],max_concurrency=1)
                state, signal = graph.propagate(request_snapshot["symbol"], request_snapshot["as_of"])
                after = _topology(graph.graph)
        finally:
            if http is not None:
                http.close()
            if live:
                write_once(root/'runtime-receipt.json',canonical_json_bytes({'schema_version':'finresearchops.native-live-runtime/v2',
                    'budget':self._budget.receipt(),'model_calls':capture.model_calls,'tool_calls':capture.tool_calls,
                    'node_calls':capture.node_calls,'vendor_observations':vendor_observations}))
        if before != after:
            raise ValueError("NATIVE_TOPOLOGY_CHANGED")

        result = {
            "upstream_commit": UPSTREAM_COMMIT,
            "runtime_kind": "LIVE_NATIVE_AUDITED" if live else "OFFLINE_SYNTHETIC_NATIVE",
            "request": request_snapshot,
            "packet_sha256": sha256_hex(canonical_json_bytes(packet_snapshot)),
            "topology_before": before,
            "topology_after": after,
            "model_calls": capture.model_calls,
            "tool_calls": capture.tool_calls,
            "node_calls": capture.node_calls,
            "reports": {key: state.get(key, "") for key in REPORT_FIELDS},
            "signal": signal,
            "final_instrument_context": state.get("instrument_context", ""),
        }
        if live:
            result.update(budget=self._budget.receipt(),provider='deepseek',model=config['deep_think_llm'])
        if filtered:
            result['vendor_observations'] = vendor_observations
        if typed:
            result['judgment_reviews']=judgment.rows
        if len(canonical_json_bytes(result)) > MAX_RESULT_BYTES:
            raise ValueError("NATIVE_RESULT_SIZE_LIMIT")
        # Keep the actual topology and final state binding even if Application
        # validation subsequently rejects the result. Never repeat paid calls
        # merely because a receipt-validation repair is needed.
        write_once(root/'native-result.json',canonical_json_bytes(result))
        return result
