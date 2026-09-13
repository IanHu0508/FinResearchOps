"""Run the current research-thesis workflow with synthetic responses, without APIs."""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "integration_tests"))

from native_support import patched_native_runtime
from test_thesis_correction import correction_sources
from test_thesis_narrative import NarrativeLLM
from finauditgate.adapters.tradingagents_thesis import ThesisResearcher
from finauditgate.application import ApplicationError, FinResearchOps
from finauditgate.core.artifacts import canonical_json_bytes, write_once
from finauditgate.private_storage import require_private_storage_root
from finauditgate.research import ResearchThesis


def _offline(*args, **kwargs):
    raise AssertionError("OFFLINE_DEMO_NETWORK_DISABLED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--bad-prose", action="store_true", help="Demonstrate rejection of an unbound EPS claim.")
    args = parser.parse_args()
    root = require_private_storage_root(Path(os.path.abspath(args.artifact_root)), purpose="thesis-offline-demo")
    model = NarrativeLLM(prose="F1 的预测 EPS 为 999 USD。" if args.bad_prose else
        "少数股东盈利须从合并净利润扣减。修正后每股盈利为 {{metric:F1:eps_per_traded_unit}}；"
        "经营现金仍从合并净利润起算，为 {{metric:F1:operating_cash_flow}}。")
    command = ResearchThesis("AURORA", date(2026, 3, 2), "合成演示：盈利归属修正如何影响每股盈利和现金？",
        sources=correction_sources(), review=False,
        user_view="演示用户期待：希望买入。该字段只记录，不进入主研究请求。")
    app = FinResearchOps(artifact_root=root, researcher=ThesisResearcher())
    with patched_native_runtime(model), patch("finauditgate.adapters.model_http.model_http_client",
            return_value=SimpleNamespace(close=lambda: None)), \
            patch.object(socket.socket, "connect", _offline), patch.object(socket, "create_connection", _offline):
        try:
            view = app.handle(command)
        except ApplicationError as exc:
            if not args.bad_prose or str(exc.__cause__) != "UNBOUND_RESEARCH_NUMBER":
                raise
            result = {"schema_version": "finresearchops.thesis-offline-demo/v1", "status": "COMPLETED", "expected_rejection": "UNBOUND_RESEARCH_NUMBER",
                      "successful_case_saved": False, "real_model_requests": 0,
                      "scope": "SYNTHETIC_NEGATIVE_CONTROL_NOT_FINANCIAL_EVALUATION"}
            assert not list(root.glob("application/thesis-cases/*/case.json"))
            write_once(root / "demo-receipt.json", canonical_json_bytes(result))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
    if args.bad_prose:
        raise AssertionError("BAD_PROSE_WAS_NOT_REJECTED")
    assert app.read_case(view.case_ref) == view
    record = view.latest_report
    before = record["forward_calculations"]["scenario_results"][0]
    after = record["effective_forward_calculations"]["scenario_results"][0]
    assert (before["eps_per_traded_unit"], after["eps_per_traded_unit"]) == (1.7, 1.3)
    assert before["operating_cash_flow"] == after["operating_cash_flow"] == 16
    assert len(record["exchanges"]) == len(model.requests) == 13
    assert all(command.user_view not in request["text"] for request in model.requests)
    result = {"schema_version": "finresearchops.thesis-offline-demo/v1", "status": "COMPLETED", "case_ref": view.case_ref, "report": view.report_path,
              "case_schema_version": record["schema_version"], "graph_nodes": len(record["topology"]["nodes"]),
              "graph_edges": len(record["topology"]["edges"]), "model_requests_simulated": len(model.requests),
              "eps_before": before["eps_per_traded_unit"], "eps_after": after["eps_per_traded_unit"],
              "cfo_before": before["operating_cash_flow"], "cfo_after": after["operating_cash_flow"],
              "case_reopens": True, "real_model_requests": 0, "model_cost_cny": "0",
              "scope": "SCRIPTED_ENGINEERING_DEMO_NOT_MODEL_QUALITY_OR_BIAS_EVALUATION"}
    write_once(root / "demo-receipt.json", canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
