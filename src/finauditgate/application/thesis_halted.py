"""Protocol 23: prove and render a halted delivery (finresearchops.thesis-halted-case/v1).

A halted Case holds the stages a stopped run completed. It is proved the way a
complete Case is: every exchange binds to its one saved call, prompt and answer;
recoveries and degradations are proved from the saved calls; each delivered stage
equals its answer; only the stopped stage may leave calls without an exchange.
The rule's reference rating and the conclusion are recomputed. Standard library only.
"""

from finauditgate.adapters.thesis_halted import (
    NODE_FIELDS, SCHEMA, STAGE_FIELDS, conclusion, deliverable_reason, scenario_calculations,
)


PROTOCOL = 23
KEYS = {"schema_version", "protocol_version", "status", "review_status", "financial_gate", "automatic_trading",
        "sensitivity_policy", "upstream_commit", "runtime_kind", "model", "request", "source_bundle", "topology",
        "halted", "analyst_reports", "initial", "revisions", "updated_claims", "research_evaluation",
        "execution_review", "risk_briefs", "independent_assessment", "forward_draft", "forward_calculations",
        "forward_revision", "effective_forward_draft", "effective_forward_calculations", "applied_changes",
        "exchanges", "model_calls", "tool_calls", "recovery", "budget", "rule_rating", "conclusion", "signal"}
NODES = {"Fundamentals Analyst": "基本面分析师", "Market Analyst": "市场分析师", "News Analyst": "新闻分析师",
         "Sentiment Analyst": "舆情分析师", "Bull Researcher": "多方研究员", "Bear Researcher": "空方研究员",
         "Research Manager": "研究经理", "Trader": "交易员", "Aggressive Analyst": "激进风险分析师",
         "Conservative Analyst": "保守风险分析师", "Neutral Analyst": "中性风险分析师", "Portfolio Manager": "组合经理"}
KINDS = {"AnalystReport": "分析报告", "InitialBrief": "论点初稿", "RevisionBrief": "论点修订",
         "ResearchEvaluation": "研究评估", "ExecutionReview": "执行审查", "RiskBrief": "风险检查",
         "IndependentAssessment": "独立初判", "UnderwritingDraft": "前瞻提案", "ForwardRevision": "参数修正",
         "FinalResearchReport": "终稿"}
SOURCES = {"RULE": "规则参考评级（程序按前瞻情景复算）", "RESEARCH_MANAGER": "研究经理评级（中间阶段结论）"}


def stage_label(node, kind):
    return NODES.get(node, node) + "·" + KINDS.get(kind, kind)


def _delivered(record):
    """(delivered answers by stage, degraded rows by stage): the stopped stage's own answer is excluded."""
    halted, exchanges = record["halted"], record["exchanges"]
    stopped = (halted["node"], halted["kind"])
    if exchanges and (exchanges[-1]["node"], exchanges[-1]["kind"]) == stopped:
        exchanges = exchanges[:-1]
    degraded = {(d["node"], d["kind"]): d for d in record["recovery"]["degraded"]}
    return {(e["node"], e["kind"]): e["parsed"] for e in exchanges}, degraded


def validate(record):
    from finauditgate.adapters.thesis_analysts import ANALYSTS, expected_topology, main_stages, validate_report
    from finauditgate.adapters.thesis_content import forward_error
    from finauditgate.adapters.thesis_correction import valid_belief_updates
    from finauditgate.adapters.thesis_degrade import check_degraded, placeholder
    from finauditgate.adapters.thesis_protocol import RESEARCHERS, RISKS, research_request_view, source_view, validate_sources
    from finauditgate.adapters.thesis_recovery import validate_recoveries
    from finauditgate.adapters.thesis_repair import KIND as REPAIR_KIND
    from finauditgate.core.forward_revision import apply_forward_revision
    from finauditgate.core.forward_scenarios import calculate_forward
    from finauditgate.core.rating_rule import rule_rating
    from .thesis_case_v11 import _check_v21_calls, _coverage, bind_exchanges, check_revision, rebuilt_claims

    if (not isinstance(record, dict) or set(record) - {"reused_calls"} != KEYS or record["schema_version"] != SCHEMA
            or type(record["protocol_version"]) is not int or record["protocol_version"] != PROTOCOL
            or record["status"] != "HALTED" or record["review_status"] != "AWAITING_REVIEW"
            or record["financial_gate"] != "NOT_REQUIRED" or record["automatic_trading"] is not False
            or record["sensitivity_policy"] != "DECLARED_SCENARIOS_REPORT_ONLY"
            or record["runtime_kind"] not in ("REAL_MODEL", "OFFLINE_SYNTHETIC")
            or not isinstance(record["model"], str) or not isinstance(record["upstream_commit"], str)):
        raise ValueError("THESIS_HALTED_RECORD_INVALID")
    validate_sources(record["source_bundle"], record["request"])
    sources, request = source_view(record["source_bundle"]), research_request_view(record["request"])
    if request["data_mode"] != "FROZEN_SOURCES" or record["topology"] != expected_topology():
        raise ValueError("THESIS_HALTED_RECORD_INVALID")
    allowed_refs = {r["id"] for r in sources["sources"]}
    calls, recovery = record["model_calls"], record["recovery"]
    excluded, dependencies = validate_recoveries(calls, recovery, complete=False, protocol_version=PROTOCOL)
    halted = recovery["halted"]
    if halted is None or record["halted"] != halted or not deliverable_reason(halted["reason"]):
        raise ValueError("THESIS_HALTED_STAGE_INVALID")
    _check_v21_calls(calls)
    check_degraded(calls, recovery, PROTOCOL, allowed_refs)

    # The exchanges are the protocol's stages in order up to the stop; the stop is the next stage,
    # or the last exchanged one when its answer failed a check after it was parsed.
    exchanges = record["exchanges"]
    parsed, degraded = _delivered(record)
    stages = [stage for stage in main_stages(PROTOCOL) if stage not in degraded]
    reached = [(e["node"], e["kind"]) for e in exchanges]
    stopped = (halted["node"], halted["kind"])
    if reached != stages[:len(reached)] or stopped not in stages[max(len(reached) - 1, 0):len(reached) + 1]:
        raise ValueError("THESIS_PROTOCOL_ORDER_INVALID")
    # Degradations and recoveries happened at the stopped stage or before it; only the stop may be pending.
    position = {stage: i for i, stage in enumerate(main_stages(PROTOCOL))}
    if any(position[stage] >= position[stopped] for stage in degraded):
        raise ValueError("THESIS_HALTED_STAGE_INVALID")
    for row in recovery["attempts"]:
        stage = (row["node"], row["kind"])
        if stage not in position or position[stage] > position[stopped] or (row["retry_run_id"] is None and stage != stopped):
            raise ValueError("THESIS_HALTED_RECOVERY_INVALID")
    binding = record
    if reached and reached[-1] == stopped and stopped[1] == "AnalystReport":
        # A stopped analyst's own answer is withheld, but its exchange still binds to its saved call.
        binding = {**record, "analyst_reports": {**record["analyst_reports"], stopped[0]: exchanges[-1]["parsed"]}}
    order = bind_exchanges(binding, exchanges, protocol=PROTOCOL, selected=True, full_analysts=True, bound=True,
                           sources=sources, request=request, allowed_refs=allowed_refs, dependencies=dependencies)
    if order != sorted(set(order)):
        raise ValueError("THESIS_MODEL_CALL_ORDER_INVALID")
    last = max(order, default=-1)
    held = {i for i, call in enumerate(calls) if call.get("run_id") in excluded}
    tail = [calls[i] for i in range(last + 1, len(calls)) if i not in held]
    kinds = {stopped[1], REPAIR_KIND} if stopped[1] == "FinalResearchReport" else {stopped[1]}
    if (set(order) | held | set(range(last + 1, len(calls))) != set(range(len(calls)))
            or any(c["node"] != stopped[0] or (c.get("thesis_stage") or {}).get("kind") not in kinds for c in tail)):
        raise ValueError("THESIS_UNACCOUNTED_STAGE_CALL")

    # Each delivered field equals the answers that were exchanged, or a degradation placeholder.
    def value(stage):
        if stage in degraded:
            return placeholder(stage[0], stage[1], degraded[stage]["reason"])
        return parsed.get(stage)

    reports = {node: value((node, "AnalystReport")) for node in ANALYSTS if value((node, "AnalystReport")) is not None}
    if record["analyst_reports"] != reports:
        raise ValueError("THESIS_ANALYST_REPORT_CHANGED")
    for report in reports.values():
        if report.get("degraded") is not True:
            validate_report(report, allowed_refs)
    initial = {node: {"claims": [{"id": ("B" if node == RESEARCHERS[0] else "S") + str(i), **claim}
                                  for i, claim in enumerate(parsed[node, "InitialBrief"]["claims"], 1)]}
               for node in RESEARCHERS if (node, "InitialBrief") in parsed}
    revisions = {node: parsed[node, "RevisionBrief"] for node in RESEARCHERS if (node, "RevisionBrief") in parsed}
    risks = {node: parsed[node, "RiskBrief"] for node in RISKS if (node, "RiskBrief") in parsed}
    if (record["initial"] != initial or record["revisions"] != revisions or record["risk_briefs"] != risks
            or record["research_evaluation"] != value(("Research Manager", "ResearchEvaluation"))
            or record["execution_review"] != value(("Trader", "ExecutionReview"))
            or record["independent_assessment"] != value(("Portfolio Manager", "IndependentAssessment"))
            or record["forward_draft"] != value(("Portfolio Manager", "UnderwritingDraft"))
            or record["forward_revision"] != value(("Portfolio Manager", "ForwardRevision"))):
        raise ValueError("THESIS_HALTED_STAGE_CHANGED")
    for node, revision in revisions.items():  # the checks the run applied before keeping each revision
        other = RESEARCHERS[1] if node == RESEARCHERS[0] else RESEARCHERS[0]
        check_revision(revision, [c["id"] for c in initial[node]["claims"]], [c["id"] for c in initial[other]["claims"]])
    if (record["updated_claims"] != rebuilt_claims(record)) if len(revisions) == 2 else record["updated_claims"] is not None:
        raise ValueError("THESIS_UPDATED_CLAIMS_CHANGED")
    independent = record["independent_assessment"]
    if independent is not None:
        ids = [b["belief_id"] for b in independent["beliefs"]]
        if ids != [f"D{i}" for i in range(1, len(ids) + 1)]:
            raise ValueError("THESIS_INDEPENDENT_BELIEF_IDS_INVALID")
    draft = record["forward_draft"]
    if draft is None:
        if record["forward_calculations"] is not None:
            raise ValueError("THESIS_FORWARD_CALCULATION_MISMATCH")
    elif forward_error(draft, request) is not None or calculate_forward(draft) != record["forward_calculations"]:
        raise ValueError("THESIS_FORWARD_CALCULATION_MISMATCH")
    revision = record["forward_revision"]
    effective = ({"effective_forward_draft": None, "effective_forward_calculations": None, "applied_changes": None}
                 if revision is None else apply_forward_revision(draft, revision["changes"]))
    if any(record[key] != effective[key] for key in STAGE_FIELDS["ForwardRevision"][1:]):
        raise ValueError("THESIS_EFFECTIVE_REVISION_MISMATCH")
    if revision is not None:
        _coverage(revision["claim_assessments"], [c["id"] for c in record["updated_claims"]], "claim_id")
        _coverage(revision["belief_updates"], [b["belief_id"] for b in independent["beliefs"]], "belief_id")
        if not valid_belief_updates(revision["belief_updates"], allow_maintain_restatement=True):
            raise ValueError("THESIS_DECISION_UPDATE_INVALID")

    calculations = scenario_calculations(record)
    rule = rule_rating(calculations, request["horizon_months"]) if calculations is not None else None
    if (record["rule_rating"] != rule or record["conclusion"] != conclusion(rule, record["research_evaluation"])
            or record["signal"] != record["conclusion"]["rating"]):
        raise ValueError("THESIS_HALTED_CONCLUSION_CHANGED")
    return record


def _number(value, kind="amount"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "未能计算"
    return {"return": f"{value:+.2%}", "price": f"{value:,.2f}"}.get(kind, f"{value:,.2f}")


def render(record):
    """The halted report, rebuilt byte for byte from the record when the Case is read."""
    from finauditgate.adapters.thesis_analysts import ANALYSTS, main_stages, render_analyst, verbatim
    from finauditgate.adapters.thesis_protocol import RISKS
    from .research_conclusion import RULE_NOTE, rating_text, rule_text
    request, halted = record["request"], record["halted"]
    identity = record["source_bundle"].get("identity") or {}
    name = identity.get("company_short_name")
    title = (f"{name}（{request['symbol']}）" if name else request["symbol"]) + " 研究报告（中止交付）"
    parsed, degraded = _delivered(record)
    done = [s for s in main_stages(PROTOCOL) if s in parsed or s in degraded]
    missing = [s for s in main_stages(PROTOCOL) if s not in parsed and s not in degraded]
    verdict = record["conclusion"]
    parts = ["# " + title, "", f"研究截止日 {request['as_of']} ｜ 研究期限 {request['horizon_months']}个月", "",
             f"> **本次运行在“{stage_label(halted['node'], halted['kind'])}”中止（原因代码：{halted['reason']}），没有可交付的终稿。"
             "以下是已完成部分与程序能给出的结论；阶段原文未经终稿的数字与引用校验，未经人工复核，不构成投资建议。**", "",
             "| 结论 | " + (rating_text(verdict["rating"]) if verdict["rating"] else "未形成") + " |", "| --- | --- |",
             "| 结论来源 | " + (SOURCES[verdict["source"]] if verdict["source"] else "未形成：研究经理完成评估之前运行已中止") + " |",
             "| 规则参考评级 | " + (rule_text(record["rule_rating"]) if record["rule_rating"] else "未进行（前瞻情景未完成）") + " |",
             f"| 中止位置 | {stage_label(halted['node'], halted['kind'])} |",
             f"| 已完成阶段 | {len(done)}/{len(done) + len(missing)}" + (f"（其中{len(degraded)}个非关键角色按降级规则省略）" if degraded else "") + " |",
             "| 人工复核 | 待复核，未签署 |", ""]
    if record["rule_rating"]:
        parts += [RULE_NOTE, ""]
    calculations = record["effective_forward_calculations"] or record["forward_calculations"]
    if calculations is not None:
        source = "修正后有效参数" if record["effective_forward_calculations"] is not None else "前瞻提案原参数"
        parts += ["## 前瞻情景复算（程序计算）", "", f"按{source}复算；情景是研究假设，不是预测或目标价。", "",
                  "| 情景 | 每交易单位盈利 | 退出价格 | 回报（不含股息） | 回报（含股息） |", "| --- | --- | --- | --- | --- |"]
        for row in calculations["scenario_results"]:
            parts.append(f"| {row['scenario_id']} | {_number(row['eps_per_traded_unit'])} | {_number(row['exit_price_per_traded_unit'], 'price')}"
                         f" | {_number(row['return_ex_dividend'], 'return')} | {_number(row['return_with_dividend'], 'return')} |")
        parts.append("")
    evaluation = record["research_evaluation"]
    if evaluation is not None:
        plan = evaluation["plan"]
        parts += ["## 研究经理评估", "", "评级：" + rating_text(plan["recommendation"]), "", "理由：", "", verbatim(plan["rationale"]), "",
                  "估值依据与缺口：", "", verbatim(evaluation["valuation_basis_and_gaps"]), ""]
    if record["risk_briefs"]:
        parts += ["## 风险检查", ""]
        for node in RISKS:
            if node in record["risk_briefs"]:
                parts += ["### " + NODES[node], "", verbatim(record["risk_briefs"][node]["analysis"]), ""]
    independent = record["independent_assessment"]
    if independent is not None:
        initial_rating = independent["decision"]["rating"]
        parts += ["## 独立初判", "", "初判评级：" + (rating_text(initial_rating) if initial_rating != "REVIEW" else "暂不评级（REVIEW）"), "",
                  verbatim(independent["decision"]["executive_summary"]), ""]
        for belief in independent["beliefs"]:
            parts += [belief["belief_id"] + "：", "", verbatim(belief["statement"]), ""]
    if record["updated_claims"] is not None:
        labels = {"maintain": "维持", "revise": "修改", "withdraw": "撤回", "unresolved": "未解决"}
        parts += ["## 多空论点（修订后）", ""]
        for claim in record["updated_claims"]:
            parts += [f"{claim['id']}（{labels.get(claim['status'], claim['status'])}）：", "",
                      verbatim(claim["statement"] if claim["statement"] is not None else "（已撤回）"), ""]
    if record["analyst_reports"]:
        parts += ["## 分析师报告", ""]
        for node in ANALYSTS:
            if node in record["analyst_reports"]:
                parts += [render_analyst(record["analyst_reports"][node], NODES[node]), ""]
    parts += ["## 未完成阶段", ""] + [f"- {stage_label(*stage)}" for stage in missing] + [""]
    parts += ["## 说明", "",
              "- 中止交付由程序在运行中止时生成，没有额外调用模型；全部调用、恢复记录与中止原因保存在case.json，读取时逐项复核。",
              "- 中止阶段在本次执行中不再重试，续跑会停在同一阶段；需要完整报告时应重新运行。", ""]
    return "\n".join(parts).encode()
