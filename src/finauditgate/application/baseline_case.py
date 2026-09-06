"""Private persistence for a native, explicitly unaudited TradingAgents report."""

import json
import re
from uuid import uuid4

from finauditgate.application.contracts import ApplicationError
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.research import TradingBaselineView


def render(record):
    parts = ["# TradingAgents 原生研究报告", "", "原生功能基线 · 尚未经过本项目财务审核 · 待人工复核", "",
             f"标的：{record['symbol']}；研究日期：{record['as_of']}；信号：{record['signal']}", ""]
    for key, title in (("fundamentals_report", "基本面"), ("market_report", "市场背景"),
                       ("investment_plan", "研究计划"), ("trader_investment_plan", "交易提案"),
                       ("final_trade_decision", "最终观点")):
        parts += [f"## {title}", "", record["reports"][key], ""]
    return "\n".join(parts).encode()


def run(application, command):
    application._require_private_artifact_root()
    if application._researcher is None:
        raise ApplicationError("RESEARCH_MODEL_REQUIRED")
    output = application._application_root / "native-executions" / uuid4().hex
    try:
        record = application._researcher.run_baseline(command, output)
        if record["symbol"] != command.symbol or record["as_of"] != command.as_of.isoformat():
            raise ValueError("BASELINE_TASK_MISMATCH")
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "TRADINGAGENTS_BASELINE_FAILED"
        raise ApplicationError(code) from exc
    raw = canonical_json_bytes(record)
    case_ref = "case-" + sha256_hex(raw)
    directory = application._application_root / "baseline-cases" / case_ref
    write_once(directory / "case.json", raw)
    write_once(directory / "report.md", render(record))
    return application.read_case(case_ref)


def load(application, case_ref):
    directory = application._application_root / "baseline-cases" / case_ref
    try:
        raw = (directory / "case.json").read_bytes()
        record = json.loads(raw)
        if (len(raw) > 4 * 1024 * 1024 or "case-" + sha256_hex(raw) != case_ref
                or record.get("schema_version") != "finresearchops.tradingagents-baseline/v1"
                or record.get("review_status") != "AWAITING_REVIEW"
                or record.get("financial_audit") != "NOT_APPLIED_NATIVE_BASELINE"
                or (directory / "report.md").read_bytes() != render(record)):
            raise ValueError("BASELINE_CASE_CHANGED")
        return TradingBaselineView(case_ref, "AWAITING_REVIEW", str(directory / "report.md"), record)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ApplicationError("BASELINE_CASE_INTEGRITY_FAILED") from exc
