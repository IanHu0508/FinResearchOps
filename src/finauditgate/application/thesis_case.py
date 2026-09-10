"""Persist the main thesis before optional data review; no core admission gate."""

import html
import json
import re
from uuid import uuid4

from finauditgate.adapters.thesis_protocol import RESEARCHERS, RISKS, validate_sources
from finauditgate.adapters.thesis_responses import response_candidate
from finauditgate.application.contracts import ApplicationError
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.research import ThesisCaseView


MAX_BYTES = 32 * 1024 * 1024
STATUS = {"maintain": "维持", "revise": "修改", "withdraw": "撤回", "unresolved": "未解决"}


def validate(record):
    """Check protocol receipt bindings without certifying the financial opinion."""
    if (not isinstance(record, dict) or record.get("schema_version") != "finresearchops.thesis-case/v1"
            or record.get("status") != "COMPLETED" or record.get("review_status") != "AWAITING_REVIEW"
            or record.get("financial_gate") != "NOT_REQUIRED" or record.get("automatic_trading") is not False):
        raise ValueError("THESIS_RECORD_INVALID")
    validate_sources(record["source_bundle"], record["request"])
    expected = ["Bull Researcher", "Bear Researcher", "Bull Researcher", "Bear Researcher",
                "Research Manager", "Trader", *RISKS, "Portfolio Manager"]
    if [e["node"] for e in record["exchanges"]] != expected:
        raise ValueError("THESIS_PROTOCOL_ORDER_INVALID")
    for exchange in record["exchanges"]:
        matches = [c for c in record["model_calls"] if c["node"] == exchange["node"]
                   and any(o.get("id") == exchange["response_id"] for o in c.get("output", []))]
        if len(matches) != 1:
            raise ValueError("THESIS_RESPONSE_BINDING_INVALID")
        actual = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]}
                  for m in matches[0]["messages"][0]]
        if actual != exchange["messages"]:
            raise ValueError("THESIS_INPUT_BINDING_INVALID")
        outputs = matches[0]["output"]
        candidate = response_candidate(outputs, exchange["kind"])
        def without_nulls(value):
            if isinstance(value, dict):
                return {key: without_nulls(child) for key, child in value.items() if child is not None}
            if isinstance(value, list):
                return [without_nulls(child) for child in value]
            return value
        # Optional null defaults can be inserted by Pydantic; they do not add
        # a financial assertion that was absent from the raw model response.
        if without_nulls(exchange["parsed"]) != without_nulls(candidate):
            raise ValueError("THESIS_OUTPUT_BINDING_INVALID")
        payload = json.loads(exchange["messages"][1]["content"])
        if payload["source_bundle"] != record["source_bundle"]:
            raise ValueError("THESIS_SOURCE_PROPAGATION_INVALID")
        if exchange["kind"] == "InitialBrief" and set(payload) != {"node", "request", "source_bundle"}:
            raise ValueError("THESIS_INITIAL_NOT_INDEPENDENT")
        if exchange["kind"] == "RevisionBrief":
            node = exchange["node"]
            other = RESEARCHERS[1] if node == RESEARCHERS[0] else RESEARCHERS[0]
            if (set(payload) != {"node", "request", "source_bundle", "own_initial", "opponent_initial"}
                    or payload["own_initial"] != record["initial"][node]
                    or payload["opponent_initial"] != record["initial"][other]):
                raise ValueError("THESIS_REVISION_NOT_SYMMETRIC")
        if exchange["kind"] == "FinalAssessment" and set(payload) != {
                "node", "request", "source_bundle", "updated_claims", "risk_briefs", "portfolio_context"}:
            raise ValueError("THESIS_FINAL_PRIOR_RATING_LEAK")
    by_kind = {e["kind"]: e for e in record["exchanges"]}
    for kind, field in (("ResearchEvaluation", "research_evaluation"), ("ExecutionReview", "execution_review"),
                        ("FinalAssessment", "final_assessment")):
        if by_kind[kind]["parsed"] != record[field]:
            raise ValueError("THESIS_DELIVERED_ASSESSMENT_CHANGED")
    for exchange in record["exchanges"]:
        node, value = exchange["node"], exchange["parsed"]
        if exchange["kind"] == "InitialBrief":
            prefix = "B" if node == RESEARCHERS[0] else "S"
            if record["initial"][node] != {"claims": [{"id": f"{prefix}{i}", **c} for i, c in enumerate(value["claims"], 1)]}:
                raise ValueError("THESIS_INITIAL_RECORD_CHANGED")
        if exchange["kind"] == "RevisionBrief" and record["revisions"][node] != value:
            raise ValueError("THESIS_REVISION_RECORD_CHANGED")
        if exchange["kind"] == "RiskBrief" and record["risk_briefs"][node] != value:
            raise ValueError("THESIS_RISK_RECORD_CHANGED")
    final = record["final_assessment"]["decision"]
    report = record["reports"]["final_trade_decision"]
    if (record["signal"] != final["rating"] or not report.startswith("**Rating**: " + final["rating"] + "\n")
            or any(final[k] not in report for k in ("executive_summary", "investment_thesis"))):
        raise ValueError("THESIS_FINAL_REPORT_BINDING_INVALID")


def render(record):
    final = record["final_assessment"]
    parts = ["# 投研观点与反证更新", "", f"{record['request']['symbol']} · {record['request']['as_of']} · {record['request']['horizon_months']}个月", "",
             "研究草稿，待人工复核；评级由本次模型分析产生。未连接交易执行。", "",
             f"资料模式：{record['request']['data_mode']}；冻结资料不等于研究日新抓取数据。", "",
             record["reports"]["final_trade_decision"], "", "## 最强反证如何影响结论", "",
             final["strongest_counterevidence"], "", final["why_it_changes_or_does_not_change_the_view"], "",
             "## 估值依据和缺口", "", final["valuation_basis_and_gaps"], "", "## 旧论点为何维持或改变", ""]
    assessments = {a["claim_id"]: a for a in final["assessments"]}
    for node in RESEARCHERS:
        initial = {c["id"]: c for c in record["initial"][node]["claims"]}
        for update in record["revisions"][node]["updates"]:
            claim = initial[update["claim_id"]]
            parts += [f"### {claim['id']} · {STATUS[update['status']]}", "", "原论点：" + claim["statement"], "",
                      "更新理由：" + update["reason"], ""]
            if update["updated_statement"]:
                parts += ["新表述：" + update["updated_statement"], ""]
            parts += ["何种观察会改变判断：" + update["would_change_mind"], "",
                      "来源：" + (", ".join(update["evidence_refs"]) or "尚缺直接资料"), "",
                      "最终评估：" + assessments[claim["id"]]["disposition"] + " — " + assessments[claim["id"]]["reason"], ""]
    parts += ["## 下一步观察", "", *["- " + value for value in final["next_observations"]], "",
              "## 执行条件与缺项", "", *["- " + value for key in ("feasibility_conditions", "missing_portfolio_inputs")
                                          for value in record["execution_review"][key]], "",
              "## 资料目录", "", "目录和消息检查只确认来源与传递关系，不证明资料正确、历史可得或结论无偏。", ""]
    parts += [f"- {s['id']}：{s['origin']}；{s['availability_note']}" for s in record["source_bundle"]["sources"]]
    for field, title in (("fundamentals_report", "基本面资料整理"), ("market_report", "市场资料整理"),
                         ("investment_plan", "研究经理计划"), ("trader_investment_plan", "交易员提案")):
        parts += ["", "## " + title, "", record["reports"][field]]
    return "\n".join(parts).encode()


def render_review(review):
    parts = ["# 独立轻量资料复核", "", "这是复核 Agent 的意见，不是认证，也不改写主报告。", "",
             "状态：" + review["status"], "", review.get("coverage_and_limits", review.get("reason", "")), ""]
    for i, finding in enumerate(review["findings"], 1):
        parts += [f"## {i}. {finding['issue']}", "", "来源：" + (", ".join(finding["evidence_refs"]) or "待取得"),
                  "", "下一步：" + finding["next_check"], ""]
    return "\n".join(parts).encode()


def run(application, command):
    application._require_private_artifact_root()
    if application._researcher is None:
        raise ApplicationError("THESIS_RESEARCHER_REQUIRED")
    root = application._application_root / "thesis-executions" / uuid4().hex
    saved = []

    def save_main(record):
        validate(record)
        if record["request"] != {"symbol": command.symbol, "as_of": command.as_of.isoformat(),
                "question": command.question, "horizon_months": command.horizon_months,
                "data_mode": "FROZEN_SOURCES" if command.sources is not None else "LIVE_VENDOR"}:
            raise ValueError("THESIS_TASK_MISMATCH")
        raw = canonical_json_bytes(record)
        if len(raw) > MAX_BYTES:
            raise ValueError("THESIS_CASE_SIZE_LIMIT")
        ref = "case-" + sha256_hex(raw)
        directory = application._application_root / "thesis-cases" / ref
        write_once(directory / "case.json", raw)
        report = render(record)
        write_once(directory / "report.md", report)
        write_once(directory / "report.html", ("<!doctype html><meta charset=utf-8><title>投研观点与反证更新</title>"
            "<style>body{max-width:960px;margin:48px auto;padding:0 24px;background:#faf9f6;color:#17242d;font:17px/1.75 system-ui}"
            "pre{white-space:pre-wrap;font:inherit}</style><pre>" + html.escape(report.decode()) + "</pre>").encode())
        saved.append((ref, directory))
        return report.decode()

    try:
        _, review = application._researcher.run_thesis(command, root, save_main=save_main)
    except Exception as exc:
        if not saved:
            causes, seen, error = [], set(), exc
            while error is not None and id(error) not in seen and len(causes) < 6:
                seen.add(id(error))
                causes.append(type(error).__name__)
                error = error.__cause__ or error.__context__
            code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_]{1,120}", str(exc)) else None
            write_once(root / "failure.json", canonical_json_bytes({
                "schema_version": "finresearchops.thesis-failure/v1",
                "error_code": code, "cause_types": causes}))
            raise ApplicationError("THESIS_RESEARCH_FAILED") from exc
        review = {"schema_version": "finresearchops.thesis-review/v1", "main_sha256": saved[0][0][5:],
                  "status": "PARTIAL", "reason": "REVIEW_INTERRUPTED_AFTER_MAIN_SAVED", "error_type": type(exc).__name__, "findings": []}
    ref, directory = saved[0]
    write_once(directory / "review.json", canonical_json_bytes(review))
    write_once(directory / "review.md", render_review(review))
    return application.read_case(ref)


def load(application, case_ref):
    directory = application._application_root / "thesis-cases" / case_ref
    try:
        raw = (directory / "case.json").read_bytes()
        if len(raw) > MAX_BYTES or "case-" + sha256_hex(raw) != case_ref:
            raise ValueError("THESIS_CASE_CHANGED")
        record = json.loads(raw)
        validate(record)
        if (directory / "report.md").read_bytes() != render(record):
            raise ValueError("THESIS_REPORT_CHANGED")
        review = {"schema_version": "finresearchops.thesis-review/v1", "main_sha256": case_ref[5:],
                  "status": "PARTIAL", "reason": "REVIEW_NOT_SAVED", "findings": []}
        if (directory / "review.json").exists():
            try:
                optional = json.loads((directory / "review.json").read_bytes())
                if (not isinstance(optional, dict) or optional.get("schema_version") != "finresearchops.thesis-review/v1"
                        or optional.get("main_sha256") != case_ref[5:]
                        or optional.get("status") not in ("COMPLETED", "PARTIAL", "DEFERRED")
                        or (directory / "review.md").read_bytes() != render_review(optional)):
                    raise ValueError("THESIS_REVIEW_BINDING_INVALID")
                review = optional
            except (OSError, ValueError, KeyError, TypeError):
                review["reason"] = "REVIEW_INTEGRITY_FAILED"
        return ThesisCaseView(case_ref, "AWAITING_REVIEW", str(directory / "report.md"), record, review)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ApplicationError("THESIS_CASE_INTEGRITY_FAILED") from exc
