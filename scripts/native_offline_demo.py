"""Exercise native nodes through Application with synthetic providers only.

Requires the separate pinned integration environment and a repository checkout.
No credentials are loaded. External network paths are rejected by the fixture.
"""

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "integration_tests"))

from native_support import NativeSyntheticLLM, patched_native_runtime
from finauditgate import FrozenDocumentPackage
from finauditgate.application import FinResearchOps, ReplayRun
from finauditgate.cashflow import CashflowTask
from finauditgate.adapters.tradingagents_native import NativeAuditAdapter
from finauditgate.core.artifacts import canonical_json_bytes, write_once
from finauditgate.private_storage import require_private_storage_root
from finauditgate.research import RunAuditedNativeResearch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--partial-evidence", action="store_true", help="Include a synthetic missing comparative adjustment.")
    args = parser.parse_args()
    root = require_private_storage_root(args.artifact_root, purpose="native-offline-demo")
    data = (REPOSITORY / "fixtures/synthetic/cashflow_investigation.html").read_bytes()
    if args.partial_evidence:
        data = re.sub(rb'<ix:nonFraction id="dep24".*?</ix:nonFraction>', b'\xe2\x80\x94', data)
    task = CashflowTask("synthetic-native-audit", FrozenDocumentPackage("Aurora synthetic", "synthetic.html", data, date(2026, 3, 1)),
        "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000001/synthetic.htm",
        "0001111111-26-000001", "1111111", date(2025, 12, 31), date(2024, 12, 31), date(2026, 3, 2), "USD")
    model = NativeSyntheticLLM()
    with patched_native_runtime(model) as external:
        app = FinResearchOps(artifact_root=root, researcher=NativeAuditAdapter())
        view = app.handle(RunAuditedNativeResearch(task, "AURORA", "合成示例：盈利与现金流变化有哪些尚未解决的限制？"))
    reopened = FinResearchOps(artifact_root=root).read_case(view.case_ref)
    replay = FinResearchOps(artifact_root=root).handle(ReplayRun(view.run_ref))
    result = view.latest_report["result"]
    assert reopened == view and replay.consistent
    assert len(model.requests) == len(result["model_calls"])
    assert len(external.calls) == len(result["tool_calls"])
    receipt = {"schema_version": "finresearchops.native-offline-demo/v1", "case_ref": view.case_ref,
        "report": view.report_path, "core_run_id": view.run_ref.run_id,
        "model_requests_simulated": len(model.requests), "tools_executed_synthetic": len(external.calls),
        "decision_node_types": len({c["node"] for c in result["model_calls"]}),
        "work_node_types": len({c["node"] for c in result["node_calls"]}),
        "graph_nodes": len(result["topology_before"]["nodes"]), "graph_edges": len(result["topology_before"]["edges"]),
        "current_core_replay": replay.consistent, "case_reopens": True,
        "network_policy": "MODEL_AND_TOOL_EXTERNAL_NETWORK_PATHS_BLOCKED_BY_TEST_FIXTURE",
        "real_model_requests": 0, "model_cost_cny": "0", "partial_evidence": args.partial_evidence,
        "scope": "NATIVE_AUDIT_INPUT_PROPAGATION_NOT_FINANCIAL_QUALITY_OR_BIAS_EVALUATION"}
    write_once(root / "receipts" / (view.case_ref + ".json"), canonical_json_bytes(receipt))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
