"""One evidence-based revision, recomputation, and a compact final report."""

from copy import deepcopy
from typing import get_args

from finauditgate.application.research_numbers import METRIC_KEYS, render_research_block
from finauditgate.core.forward_revision import apply_forward_revision


def correction_schemas(base):
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field, StrictFloat

    assumption = base["UnderwritingDraft"].model_fields["market_price"].annotation
    scenario = get_args(base["UnderwritingDraft"].model_fields["scenarios"].annotation)[0]
    attribution = scenario.model_fields["noncontrolling_attribution"].annotation
    old_update = base["FinalAssessment"].model_fields["decision_update"].annotation
    old_belief = get_args(old_update.model_fields["belief_updates"].annotation)[0]
    old_assessment = get_args(base["FinalAssessment"].model_fields["assessments"].annotation)[0]
    rating_type = base["IndependentAssessment"].model_fields["decision"].annotation.model_fields["rating"].annotation

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    class ExpectedAttribution(Strict):
        nature: Literal["profit", "loss", "unknown"]
        amount: StrictFloat | None

    class ForwardChange(Strict):
        scenario_id: Literal["F1", "F2", "F3"] | None = Field(description="Null only for common market_price or shares_per_traded_unit.")
        field: Literal["market_price", "shares_per_traded_unit", "revenue", "operating_margin",
            "net_nonoperating_income", "effective_tax_rate", "noncontrolling_attribution",
            "diluted_ordinary_shares", "non_working_capital_adjustments",
            "operating_asset_liability_cash_effect", "cash_capex", "fx_reporting_per_price_currency",
            "exit_pe", "cash_dividend_per_traded_unit"]
        expected_before: StrictFloat | ExpectedAttribution | None = Field(description="Copy the proposed input VALUE exactly, not a historical source value. For NCI use its proposed nature and amount.value.")
        replacement: assumption | attribution = Field(description="Complete corrected assumption including reason and evidence_refs; NCI is a complete nature+amount object. Unknown uses value:null, never guessed zero.")
        correction_basis: Literal["source_misread", "accounting_correction", "assumption_update"]
        reason: str = Field(min_length=1, max_length=1600)
        evidence_refs: list[str] = Field(min_length=1, max_length=8)

    class ShortClaimAssessment(old_assessment):
        reason: str = Field(min_length=1, max_length=1600)

    class ShortBeliefUpdate(old_belief):
        reason: str = Field(min_length=1, max_length=1600)
        financial_implication: str = Field(min_length=1, max_length=1600)

    class ForwardRevision(Strict):
        changes: list[ForwardChange] = Field(max_length=12, description="Only justified changes. Empty is valid; do not redraw all forecasts or fit inputs to a desired price/rating.")
        claim_assessments: list[ShortClaimAssessment] = Field(max_length=8)
        belief_updates: list[ShortBeliefUpdate] = Field(min_length=2, max_length=4)
        unresolved_issues: list[str] = Field(max_length=6)

    class ResearchMetric(Strict):
        scenario_id: Literal["F1", "F2", "F3"]
        metric: Literal[*METRIC_KEYS]

    class ResearchBlock(Strict):
        text: str = Field(min_length=1, max_length=2400, description="Concise economic explanation. Refer to forward quantities through metrics; do not retype forecast amounts/ratios, create a new target, or repeat the table. Historical claims still need source support.")
        evidence_refs: list[str] = Field(max_length=8)
        metrics: list[ResearchMetric] = Field(max_length=6, description="Program inserts complete metric labels, exact current values, units and period; no value or label is supplied by the model.")

    class ResearchSections(Strict):
        operating_performance: ResearchBlock
        earnings_quality: ResearchBlock
        cash_and_capital_allocation: ResearchBlock
        valuation_and_price_requirements: ResearchBlock

    class ScenarioUse(Strict):
        scenario_id: Literal["F1", "F2", "F3"]
        disposition: Literal["use", "conditional", "reject"]
        reason: str = Field(min_length=1, max_length=1600)
        what_changes_the_view: str = Field(min_length=1, max_length=1200)

    class FinalResearchReport(Strict):
        rating: rating_type
        summary: ResearchBlock
        financial_analysis: ResearchSections
        strongest_counterevidence: ResearchBlock
        scenario_assessments: list[ScenarioUse] = Field(max_length=3)
        limitations: list[str] = Field(max_length=6)

    return {"ForwardRevision": ForwardRevision, "FinalResearchReport": FinalResearchReport}


def render_decision(report, draft, calculations):
    return "\n".join(["**Rating**: " + report["rating"], "",
                      *render_research_block(report["summary"], draft, calculations)])


def complete_corrected_report(session, node, config):
    from finauditgate.adapters.thesis_protocol import belief_view, claim_view, risk_view

    payload = session.corpus({})
    payload.update(independent_beliefs=belief_view(session.independent),
                   updated_claims=claim_view(session.updated_claims()), risk_briefs=risk_view(session.risks),
                   forward_draft=session.forward_draft, forward_calculations=session.forward_calculations)
    claim_ids = [c["id"] for c in payload["updated_claims"]]
    belief_ids = [b["belief_id"] for b in payload["independent_beliefs"]]
    resolution = session.ask(node, "ForwardRevision",
        "现在只完成一次有证据的参数修正及简短信念更新，不写评级或整份研报。核对原提案的历史依据、会计口径和内部矛盾。"
        "未来假设可以不同于历史；不能仅因尚未实现就判错。但不能把误读历史数据当作未来假设的依据。"
        "如发现重要错误，请实际给changes使程序修正并复算，不要只写拒绝或提醒。无合理新值时置value:null并解释；不强求每个情景都修改。"
        "每项expected_before必须等于当前提案字段的值；NCI为{nature,amount数值}，replacement为完整性质+假设对象。"
        "正的少数股东盈利从合并利润扣减，亏损才加回；应结合合并与归母勾稽判断，不能只凭报表括号。现金从合并净利起算，两组调节不重复。"
        "修改理由写具体来源或推理改变，不能为了让股价便宜或维持原看法而反解增长率、利润率、税率或倍数。不得改来源、情景ID、研究期限。"
        "claim_assessments只且完整覆盖（含撤回项）" + ",".join(claim_ids) + "；belief_updates只且完整覆盖"
        + ",".join(belief_ids) + "。用户假设在理由中回应，不另造H编号。每项只写差异和实质理由，避免复制整套数字。",
        payload, config)
    session.coverage(resolution["claim_assessments"], claim_ids)
    updates = resolution["belief_updates"]
    if (len(updates) != len(belief_ids) or {u["belief_id"] for u in updates} != set(belief_ids)
            or any((u["status"] == "revise") != bool(u["new_statement"]) for u in updates)):
        raise ValueError("THESIS_DECISION_UPDATE_INVALID")
    result = apply_forward_revision(session.forward_draft, resolution["changes"])
    session.forward_revision = deepcopy(resolution)
    session.effective_forward_draft = result["effective_forward_draft"]
    session.effective_forward_calculations = result["effective_forward_calculations"]
    session.applied_changes = result["applied_changes"]
    final_payload = session.corpus({})
    final_payload.update(effective_forward_draft=session.effective_forward_draft,
                         effective_forward_calculations=session.effective_forward_calculations,
                         research_resolution={k: deepcopy(resolution[k]) for k in
                             ("claim_assessments", "belief_updates", "unresolved_issues")})
    final = session.ask(node, "FinalResearchReport",
        "依据原始资料、修正后的有效参数和程序复算，形成一份精简但完整的中文终判。没有初判评级、用户期待或原错误预测表。"
        "分别回答经营驱动、盈利质量、现金与资本配置、价格要求，再给最强反证与情景采纳。每段聚焦经济解释，不重复投资总论、估值长文和全部检查清单。"
        "关键前瞻数量必须通过metrics选择scenario_id和metric，由程序插入标签、数值、单位、期间；不要再在text里复制预测数值或发明未计算目标价。"
        "历史数字仍要核对来源中的期间和集团/分部、合并/归母口径。不要把公司经营利润说成未披露的分部经营利润。"
        "有效版本只表示已应用修正并复算，不等于假设获证实。若仍有重要问题，可拒绝该情景并解释缺项；不再启动新一轮修改，也不因程序成功就采信。"
        "使用有理由的预测假设，不要求未来已验证；公司质量与当前价格吸引力分开。无可辩护定价依据时可REVIEW，不能用Hold填空，也不能把条件打平PE当公允倍数。"
        "不重复生成B/S逐项评估或D信念更新，程序会呈现已保存的修正与观点变化。scenario_assessments仅且完整覆盖"
        + ",".join(s["scenario_id"] for s in session.effective_forward_draft["scenarios"]) + "。",
        final_payload, config)
    ids = [s["scenario_id"] for s in session.effective_forward_draft["scenarios"]]
    if len(final["scenario_assessments"]) != len(ids) or {s["scenario_id"] for s in final["scenario_assessments"]} != set(ids):
        raise ValueError("THESIS_FORWARD_ASSESSMENT_COVERAGE_INVALID")
    for block in (final["summary"], *final["financial_analysis"].values(), final["strongest_counterevidence"]):
        render_research_block(block, session.effective_forward_draft, session.effective_forward_calculations)
    session.final = final
    return render_decision(final, session.effective_forward_draft, session.effective_forward_calculations)
