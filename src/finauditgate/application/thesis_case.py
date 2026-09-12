"""Persist the main thesis before optional data review; no core admission gate."""

import html
import json
import re
from uuid import uuid4

from finauditgate.adapters.thesis_protocol import RESEARCHERS, RISKS, validate_sources, source_view, check_refs, belief_view, claim_view, risk_view, research_request_view
from finauditgate.adapters.thesis_responses import response_candidate
from finauditgate.application.contracts import ApplicationError
from finauditgate.application.forward_report import render_forward
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex, write_once
from finauditgate.core.forward_scenarios import calculate_forward, valuation_date_for
from finauditgate.research import ThesisCaseView, thesis_request


MAX_BYTES = 32 * 1024 * 1024
STATUS = {"maintain": "维持", "revise": "修改", "withdraw": "撤回", "unresolved": "未解决"}


def validate(record):
    """Check protocol receipt bindings without certifying the financial opinion."""
    if isinstance(record, dict) and record.get("schema_version") == "finresearchops.thesis-case/v11":
        from finauditgate.application.thesis_case_v11 import validate as validate_v11
        return validate_v11(record)
    if (not isinstance(record, dict) or record.get("schema_version") not in ("finresearchops.thesis-case/v1", "finresearchops.thesis-case/v2", "finresearchops.thesis-case/v10")
            or record.get("status") != "COMPLETED" or record.get("review_status") != "AWAITING_REVIEW"
            or record.get("financial_gate") != "NOT_REQUIRED" or record.get("automatic_trading") is not False):
        raise ValueError("THESIS_RECORD_INVALID")
    current = record["schema_version"] == "finresearchops.thesis-case/v10"
    if current and record.get("sensitivity_policy") != "DECLARED_SCENARIOS_REPORT_ONLY":
        raise ValueError("THESIS_SENSITIVITY_POLICY_INVALID")
    if record["schema_version"] != "finresearchops.thesis-case/v1":
        finance = record.get("final_assessment", {}).get("financial_analysis", {})
        refs = finance.get("evidence_refs")
        if (set(finance) != {"operating_performance", "earnings_quality", "cash_and_capital_allocation", "valuation_and_price_requirements", "evidence_refs"}
                or any(not isinstance(finance[k], str) or not finance[k].strip() for k in finance if k != "evidence_refs")
                or (not current and (not isinstance(refs, list) or not refs))
                or (refs is not None and (not isinstance(refs, list)
                    or not set(refs) <= {s["id"] for s in record["source_bundle"]["sources"]}))):
            raise ValueError("THESIS_FINANCIAL_ANALYSIS_REQUIRED")
    validate_sources(record["source_bundle"], record["request"])
    expected = ["Bull Researcher", "Bear Researcher", "Bull Researcher", "Bear Researcher",
                "Research Manager", "Trader", *RISKS, "Portfolio Manager"]
    if current:
        expected.extend(["Portfolio Manager", "Portfolio Manager"])
    if [e["node"] for e in record["exchanges"]] != expected:
        raise ValueError("THESIS_PROTOCOL_ORDER_INVALID")
    kinds = ["InitialBrief", "InitialBrief", "RevisionBrief", "RevisionBrief", "ResearchEvaluation",
             "ExecutionReview", "RiskBrief", "RiskBrief", "RiskBrief"]
    kinds += ["IndependentAssessment", "UnderwritingDraft", "FinalAssessment"] if current else ["FinalAssessment"]
    if [e["kind"] for e in record["exchanges"]] != kinds:
        raise ValueError("THESIS_PROTOCOL_ORDER_INVALID")
    call_order = []
    for exchange in record["exchanges"]:
        matches = [c for c in record["model_calls"] if c["node"] == exchange["node"]
                   and any(o.get("id") == exchange["response_id"] for o in c.get("output", []))]
        if len(matches) != 1:
            raise ValueError("THESIS_RESPONSE_BINDING_INVALID")
        call_order.append(record["model_calls"].index(matches[0]))
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
        if current and payload.get("request") != research_request_view(record["request"]):
            raise ValueError("THESIS_RESEARCH_REQUEST_BINDING_INVALID")
        expected_sources = (source_view(record["source_bundle"])
                            if current else record["source_bundle"])
        if payload["source_bundle"] != expected_sources:
            raise ValueError("THESIS_SOURCE_PROPAGATION_INVALID")
        if current:
            check_refs(exchange["parsed"], {s["id"] for s in expected_sources["sources"]})
        if exchange["kind"] == "InitialBrief" and set(payload) != {"node", "request", "source_bundle"}:
            raise ValueError("THESIS_INITIAL_NOT_INDEPENDENT")
        if exchange["kind"] == "RevisionBrief":
            node = exchange["node"]
            other = RESEARCHERS[1] if node == RESEARCHERS[0] else RESEARCHERS[0]
            if (set(payload) != {"node", "request", "source_bundle", "own_initial", "opponent_initial"}
                    or payload["own_initial"] != record["initial"][node]
                    or payload["opponent_initial"] != record["initial"][other]):
                raise ValueError("THESIS_REVISION_NOT_SYMMETRIC")
        if exchange["kind"] == "IndependentAssessment":
            if not current or set(payload) != {"node", "request", "source_bundle", "portfolio_context"}:
                raise ValueError("THESIS_INDEPENDENT_INPUT_LEAK")
        if exchange["kind"] == "UnderwritingDraft":
            if (not current or set(payload) != {"node", "request", "source_bundle", "updated_claims", "risk_briefs"}
                    or payload["updated_claims"] != claim_view(record["updated_claims"])
                    or payload["risk_briefs"] != risk_view(record["risk_briefs"])):
                raise ValueError("THESIS_FORWARD_INPUT_LEAK")
        if exchange["kind"] == "FinalAssessment":
            allowed = {"node", "request", "source_bundle", "updated_claims", "risk_briefs", "portfolio_context"}
            if current:
                allowed.update({"independent_beliefs", "forward_draft", "forward_calculations"})
            if set(payload) != allowed:
                raise ValueError("THESIS_FINAL_PRIOR_RATING_LEAK")
            if current and (payload["independent_beliefs"] != belief_view(record["independent_assessment"])
                    or payload["updated_claims"] != claim_view(record["updated_claims"])
                    or payload["risk_briefs"] != risk_view(record["risk_briefs"])):
                raise ValueError("THESIS_COUNTEREVIDENCE_BINDING_INVALID")
            if current and (payload["forward_draft"] != record["forward_draft"]
                    or payload["forward_calculations"] != record["forward_calculations"]):
                raise ValueError("THESIS_FORWARD_CALCULATIONS_NOT_DELIVERED")
    if current and call_order != sorted(set(call_order)):
        raise ValueError("THESIS_MODEL_CALL_ORDER_INVALID")
    by_kind = {e["kind"]: e for e in record["exchanges"]}
    for kind, field in (("ResearchEvaluation", "research_evaluation"), ("ExecutionReview", "execution_review"),
                        ("FinalAssessment", "final_assessment")):
        if by_kind[kind]["parsed"] != record[field]:
            raise ValueError("THESIS_DELIVERED_ASSESSMENT_CHANGED")
    if current:
        if by_kind.get("UnderwritingDraft", {}).get("parsed") != record.get("forward_draft"):
            raise ValueError("THESIS_FORWARD_DRAFT_CHANGED")
        if record.get("forward_calculations") != calculate_forward(record["forward_draft"]):
            raise ValueError("THESIS_FORWARD_CALCULATION_MISMATCH")
        price_date = record["forward_draft"]["market_price_date"]
        if price_date is not None and price_date > record["request"]["as_of"]:
            raise ValueError("THESIS_FORWARD_FUTURE_MARKET_PRICE")
        if record["forward_draft"]["valuation_date"] != valuation_date_for(record["request"]["as_of"], record["request"]["horizon_months"]):
            raise ValueError("THESIS_FORWARD_HORIZON_MISMATCH")
        forward_ids = [s["scenario_id"] for s in record["forward_draft"]["scenarios"]]
        forward_assessments = record["final_assessment"]["forward_assessment"]["scenario_assessments"]
        if (forward_ids != [f"F{i}" for i in range(1, len(forward_ids) + 1)]
                or len(forward_assessments) != len(forward_ids)
                or {a["scenario_id"] for a in forward_assessments} != set(forward_ids)):
            raise ValueError("THESIS_FORWARD_ASSESSMENT_COVERAGE_INVALID")
        if by_kind.get("IndependentAssessment", {}).get("parsed") != record["independent_assessment"]:
            raise ValueError("THESIS_INDEPENDENT_ASSESSMENT_CHANGED")
        independent = record["independent_assessment"]
        ids = [b["belief_id"] for b in independent["beliefs"]]
        update = record["final_assessment"]["decision_update"]
        comparison = {"before": independent["decision"]["rating"], "after": record["final_assessment"]["decision"]["rating"],
            "changed": independent["decision"]["rating"] != record["final_assessment"]["decision"]["rating"]}
        if record.get("rating_comparison") != comparison:
            raise ValueError("THESIS_RATING_COMPARISON_INVALID")
        if (ids != [f"D{i}" for i in range(1, len(ids) + 1)]
                or len(update["belief_updates"]) != len(ids)
                or {b["belief_id"] for b in update["belief_updates"]} != set(ids)
                or any((b["status"] == "revise") != bool(b["new_statement"]) for b in update["belief_updates"])):
            raise ValueError("THESIS_DECISION_UPDATE_INVALID")
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
    if record["schema_version"] == "finresearchops.thesis-case/v11":
        from finauditgate.application.thesis_report_v11 import render as render_v11
        return render_v11(record)
    final = record["final_assessment"]
    parts = ["# 投研观点与反证更新", "", f"{record['request']['symbol']} · {record['request']['as_of']} · {record['request']['horizon_months']}个月", "",
             "研究草稿，待人工复核；评级由本次模型分析产生。未连接交易执行。", "",
             f"资料模式：{record['request']['data_mode']}；冻结资料不等于研究日新抓取数据。", "",
             record["reports"]["final_trade_decision"], "", "## 最强反证如何影响结论", "",
             final["strongest_counterevidence"], "", final["why_it_changes_or_does_not_change_the_view"], "",
             "## 估值依据和缺口", "", final["valuation_basis_and_gaps"], "", "## 旧论点为何维持或改变", ""]
    if record["schema_version"] == "finresearchops.thesis-case/v10":
        request = record["request"]
        context = ["研究问题：" + request["question"], ""]
        for title, key in (("待检验假设（不是已验证事实）", "hypotheses"), ("研究约束", "research_constraints")):
            if request[key]:
                context += [title + "：", "", *["> " + value.replace("\n", "\n> ") for value in request[key]], ""]
        if request["user_view"] is not None:
            context += ["用户原始期待（仅记录；未进入主研究请求）：", "", "> " + request["user_view"].replace("\n", "\n> "), ""]
        parts[8:8] = context
    if record["schema_version"] != "finresearchops.thesis-case/v1":
        finance = final["financial_analysis"]
        insert = parts.index("## 最强反证如何影响结论")
        financial_sections = []
        for key, title in (("operating_performance", "经营表现与持续性"), ("earnings_quality", "盈利质量与归母勾稽"),
                           ("cash_and_capital_allocation", "现金创造与资本配置"), ("valuation_and_price_requirements", "估值与当前价格要求")):
            financial_sections += ["## " + title, "", finance[key], ""]
        financial_sections += [("财务分析引用：" + ", ".join(finance["evidence_refs"]) if finance.get("evidence_refs")
            else "未单列章节级引用汇总；参见逐条论点、信念更新与采用口径的引用。未自动补写引用。"), ""]
        parts[insert:insert] = financial_sections
    if record["schema_version"] == "finresearchops.thesis-case/v10":
        independent = record["independent_assessment"]
        update = final["decision_update"]
        section = ["## 独立初判如何被反证更新", "",
            "这是同一研究时点内、接触同伴意见前后的比较，不是历史评级。独立初判读取研究资料；终判不读取初判评级、摘要及触发阈值，只读取待核对的信念与研究提案。前后评级由程序事后比较；这仍不能消除模型自身偏好。", "",
            f"独立初判：{independent['decision']['rating']}；反证后最终判断：{final['decision']['rating']}。", "",
            "独立初判理由：" + independent["decision"]["executive_summary"], "",
            "最终评级依据：" + update["basis_for_rating"], ""]
        originals = {b["belief_id"]: b for b in independent["beliefs"]}
        for belief in update["belief_updates"]:
            section += [f"### {belief['belief_id']} · {STATUS[belief['status']]}", "",
                "独立信念：" + originals[belief["belief_id"]]["statement"], "",
                "更新类型：" + belief["update_basis"], "", "依据：" + belief["reason"], "",
                "对盈利、现金或估值的影响：" + belief["financial_implication"], "",
                "引用：" + (", ".join(belief["evidence_refs"]) or "尚缺直接依据"), ""]
            if belief["new_statement"]:
                section += ["新表述：" + belief["new_statement"], ""]
        section += ["### 最终实际采用的研究假设", "", "以下支持程度由模型自述，不是程序对假设真实性的认证。", ""]
        for assumption in update["adopted_assumptions"]:
            section += ["- " + assumption["statement"] + f"（来源：{assumption['origin']}；依据：{assumption['basis']}）",
                "  " + assumption["why_appropriate"], "  若不成立：" + assumption["consequence_if_false"], ""]
        if not update["adopted_assumptions"]:
            section += ["模型未列出采用的自设假设；不代表已经证明没有隐含假设。", ""]
        forward_section = render_forward(record["forward_draft"], record["forward_calculations"], final["forward_assessment"])
        insert = parts.index("## 最强反证如何影响结论")
        parts[insert:insert] = [*forward_section, *section]
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
    if record["schema_version"] == "finresearchops.thesis-case/v10" and record["source_bundle"]["schema_version"] == "finresearchops.thesis-sources/v2":
        for source in record["source_bundle"]["sources"]:
            if source["use"] == "sensitivity":
                parts += ["", "## 情景附录：" + source["id"], "",
                    "以下是编排者的条件情景，未提供给独立初判或反证终判；它不是最终评级的证据、目标价或公允价值。", "",
                    source["content"]]
    text = "\n".join(parts)
    if record["schema_version"] == "finresearchops.thesis-case/v10":
        for old, new in (
            ("## 旧论点为何维持或改变", "## 本轮研究者初稿与更新（过程记录）"),
            ("## 执行条件与缺项", "## 过程附录：交易员提出的条件与缺项\n\n这些是过程提案，不是用户已批准的条件或最终评级门槛。"),
            ("## 研究经理计划", "## 过程附录：研究经理计划\n\n形成于最终判断之前，其中评级不能替代本页最终评级。"),
            ("## 交易员提案", "## 过程附录：交易员提案\n\n保留原始过程输出；不是另一份最终建议，也未执行交易。")):
            text = text.replace(old, new)
    return text.encode()


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
        if record["request"] != thesis_request(command):
            raise ValueError("THESIS_TASK_MISMATCH")
        raw = canonical_json_bytes(record)
        if len(raw) > MAX_BYTES:
            raise ValueError("THESIS_CASE_SIZE_LIMIT")
        ref = "case-" + sha256_hex(raw)
        directory = application._application_root / "thesis-cases" / ref
        write_once(directory / "case.json", raw)
        report = render(record)
        write_once(directory / "report.md", report)
        if record["schema_version"] == "finresearchops.thesis-case/v11":
            from finauditgate.application.thesis_report_v11 import render_process
            write_once(directory / "process-record.md", render_process(record))
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
        if record["schema_version"] == "finresearchops.thesis-case/v11":
            from finauditgate.application.thesis_report_v11 import render_process
            if (directory / "process-record.md").read_bytes() != render_process(record):
                raise ValueError("THESIS_PROCESS_RECORD_CHANGED")
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
