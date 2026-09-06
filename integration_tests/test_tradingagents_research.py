"""Run with the separate integration Python; all data and model replies are synthetic."""

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import date

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import test_research_workflow as support
from finauditgate.adapters.tradingagents_research import TradingAgentsResearcher
from finauditgate.adapters.model_budget import ModelBudget
from finauditgate.application import FinResearchOps
from finauditgate.research import RunTradingBaseline


class SyntheticClient:
    def __init__(self, json_content=False):
        self.requests = []
        self.schemas = []
        self.json_content = json_content

    def with_structured_output(self, schema, **kwargs):
        return self

    def bind(self, **kwargs):
        if kwargs.get("response_format") != {"type": "json_object"} or not set(kwargs) <= {"response_format", "reasoning_effort"}:
            raise AssertionError("JSON_MODE_REQUIRED")
        return self

    def invoke(self, prompt):
        schema = json.loads(prompt.split("JSON schema:\n", 1)[1].split("\nEvidence and task:\n", 1)[0])
        self.schemas.append(schema)
        payload = json.loads(prompt.split("Evidence and task:\n", 1)[1])
        self.requests.append(payload)
        a, c, d = support.proposals(payload["evidence"])
        proposal = (a, c, d)[len(self.requests) - 1]
        raw = SimpleNamespace(content=json.dumps(proposal, ensure_ascii=False), tool_calls=[], invalid_tool_calls=[],
                              usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        return raw


class TradingAgentsGraphTest(unittest.TestCase):
    setUp = support.ResearchWorkflowTest.setUp

    def test_lookup_contract_exposes_exact_allowed_ids_to_the_model(self):
        client = SyntheticClient()
        runner = TradingAgentsResearcher(trace_root=self.root / "traces", client=client,
                                         budget=ModelBudget(max_calls=3))
        FinResearchOps(artifact_root=self.root, researcher=runner).handle(self.command)
        expected = [x["driver_id"] for x in client.requests[0]["evidence"]["analysis"]["drivers"][:6]]
        for schema in client.schemas[:2]:
            self.assertEqual(expected, schema["properties"]["requested_drivers"]["items"].get("enum"))
    def test_real_graph_has_isolated_initial_contexts_and_shared_new_evidence(self):
        client = SyntheticClient()
        runner = TradingAgentsResearcher(trace_root=self.root / "traces", client=client,
                                         budget=ModelBudget(max_calls=3))
        app = FinResearchOps(artifact_root=self.root, researcher=runner)
        view = app.handle(self.command)
        self.assertEqual(3, len(client.requests))
        self.assertEqual(client.requests[0], client.requests[1])
        self.assertNotIn("analysis", client.requests[1])
        self.assertNotIn("displayed_cash_effect", json.dumps(client.requests[0]))
        self.assertNotIn("document_sha256", json.dumps(client.requests[0]))
        self.assertIn("RECONCILIATION_COMPONENT_NOT_A_DIRECT_CASH_RECEIPT_OR_PAYMENT", json.dumps(client.requests[0]))
        self.assertTrue(client.requests[2]["evidence"]["steps"])
        self.assertNotIn("analysis", client.requests[2])
        self.assertNotIn("challenge", client.requests[2])
        self.assertTrue(client.requests[2]["required_evidence_ids"])
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))
        self.assertEqual(3, view.latest_report["result"]["budget"]["calls"])
        self.assertEqual(6, len(list((self.root / "traces").glob("*.json"))))

    def test_exact_json_content_can_be_used_without_an_extra_model_call(self):
        client = SyntheticClient(json_content=True)
        runner = TradingAgentsResearcher(trace_root=self.root / "traces", client=client,
                                         budget=ModelBudget(max_calls=3))
        view = FinResearchOps(artifact_root=self.root, researcher=runner).handle(self.command)
        self.assertEqual(3, len(client.requests))
        self.assertEqual("混合", view.latest_report["result"]["decision"]["outlook"])

    def test_native_graph_builds_with_private_configuration_without_network(self):
        state = {key: "Synthetic native-construction fixture." for key in (
            "fundamentals_report", "market_report", "investment_plan", "trader_investment_plan", "final_trade_decision")}
        seen = {}
        def propagate(graph, symbol, as_of):
            seen.update(graph.config)
            self.assertEqual(("fundamentals", "market"), graph.selected_analysts)
            return state, "REVIEW"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-construction-key"}), \
             patch("socket.socket.connect", side_effect=AssertionError("NO_NETWORK_IN_CONSTRUCTION_TEST")), \
             patch("tradingagents.graph.trading_graph.TradingAgentsGraph.propagate", propagate):
            runner = TradingAgentsResearcher(trace_root=self.root / "native-traces")
            view = FinResearchOps(artifact_root=self.root, researcher=runner).handle(RunTradingBaseline("AURORA", date(2026, 3, 2)))
        self.assertIsNone(seen["memory_log_path"])
        self.assertIsNone(seen["backend_url"])
        self.assertTrue(Path(seen["data_cache_dir"]).is_relative_to(self.root.resolve()))
        self.assertEqual(0, view.latest_report["budget"]["calls"])
        self.assertEqual(view, FinResearchOps(artifact_root=self.root).read_case(view.case_ref))

    def test_missing_credentials_reject_before_market_or_model_access(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
             patch("socket.socket.connect", side_effect=AssertionError("NO_NETWORK_BEFORE_CREDENTIALS")):
            runner = TradingAgentsResearcher(trace_root=self.root / "native-traces")
            with self.assertRaisesRegex(Exception, "DEEPSEEK_API_KEY_NOT_CONFIGURED"):
                FinResearchOps(artifact_root=self.root, researcher=runner).handle(RunTradingBaseline("AURORA", date(2026, 3, 2)))
        self.assertEqual(0, runner.budget.calls)


if __name__ == "__main__":
    unittest.main()
