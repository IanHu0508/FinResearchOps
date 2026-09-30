"""Current report: bound numerical prose and separately attributed input edits."""

from copy import deepcopy
import json

from . import thesis_report_v11 as previous
from .research_narrative import report_context, change_view, _escape
from .research_numbers import render_research_block
from .forward_report import _number, _table
from finauditgate.core.revision_effects import revision_effects


def _corrections(record, context, *, selected=False, mechanical=False):
    parts = ["## 参数变化：事实纠错与假设更新", "",
             "原因分类由模型申报，不是程序对来源支持的认证。数值影响按修改顺序复算；"
             "同一少数股东项目先变性质、再变金额，分解依赖该顺序，不代表统计因果归因。", ""]
    draft = record["effective_forward_draft"]
    explanations = {(r["scenario_id"], r["field"]): r["explanation"] for r in record["final_report"]["change_explanations"]}
    effects = revision_effects(record["forward_draft"], record["forward_revision"]["changes"])
    labels = {"source_misread": "来源误读", "accounting_correction": "会计处理修正", "assumption_update": "研究假设更新"}
    components = {"direction_change": "盈利／亏损方向变化", "amount_change": "金额变化",
                  "input_change": "参数变化", "rationale_only": "仅改依据或标签，数值不变"}
    for edit in record["applied_changes"]:
        sid, field = edit["scenario_id"], edit["field"]
        parts += [f"### {sid or '共同参数'} · {previous._PARAMETER_INFO[field][0]}", ""]
        rows = []
        for version, value in (("原输入（已替换）", edit["before"]), ("当前输入", edit["after"])):
            label, display = previous._input_value(field, value, draft)
            rows.append((version, label, display))
        parts += _table(("版本", "口径", "取值与单位"), rows)
        parts += ["模型申报原因：" + labels.get(edit["correction_basis"], "未分类") + "。", ""]
        if mechanical:
            parts += ["程序仅确认上表的输入变化及下表的算术影响。模型修改理由与最终解释完整保留于"
                      "[过程记录附录](process-record.md)，未经语义认可，不作为本节变化事实。", ""]
        else:
            parts += ["修改依据：" + context.text(explanations[(sid, field)]["text"]), ""]
        parts += ["来源引用：" + _escape(previous._refs(edit["evidence_refs"])), ""]
        if selected:
            explanation = context.block(explanations[(sid, field)])
            display_draft = deepcopy(draft)
            for scenario in display_draft["scenarios"]:
                scenario["name"] = "条件情景"
            parts += render_research_block({"text": "", "metrics": explanation["metrics"],
                "evidence_refs": explanation["evidence_refs"]}, display_draft, record["effective_forward_calculations"])
        assumption = edit["after"]["amount"] if field == "noncontrolling_attribution" else edit["after"]
        if assumption["basis_type"] == "analyst_assumption":
            parts += ["当前金额／参数仍标为研究假设；来源或会计纠错标签不使该未来数值成为已证实事实。", ""]
        for effect in effects:
            if (effect["scenario_id"], effect["field"]) != (sid, field):
                continue
            parts += [components[effect["component"]] + "：", ""]
            values = []
            for result in effect["results"]:
                for key, label, unit in (
                    ("parent_net_income", "归母净利润", f"百万元 {draft['reporting_currency']}"),
                    ("eps_per_traded_unit", "EPS", f"{draft['price_currency']}／交易单位"),
                    ("operating_cash_flow", "经营现金流", f"百万元 {draft['reporting_currency']}"),
                    ("return_with_dividend", "含息累计回报", "%；变化为百分点")):
                    nums = result["metrics"][key]
                    fmt = lambda v: _number(v, percent=key == "return_with_dividend", precise=True, signed=True)
                    values.append((result["scenario_id"], label, fmt(nums["before"]), fmt(nums["after"]), fmt(nums["delta"]), unit))
            parts += _table(("情景", "指标", "本步之前", "本步之后", "本步变化", "单位"), values)
    if not record["applied_changes"]:
        parts += ["未应用参数修改；原假设保持，不表示原假设已获验证。", ""]
    return parts


def _belief_updates(record, context, *, mechanical=False):
    parts = ["## 旧信念为何维持或改变", "",
             "比较同一研究时点内的独立初判与更新，不是历史评级。原信念及原始数字完整保存在"
             "[过程记录附录](process-record.md)；以下解释使用本次有效数值。", ""]
    explanations = {r["belief_id"]: r["explanation"] for r in record["final_report"]["belief_explanations"]}
    display_draft = deepcopy(record["effective_forward_draft"])
    for row in display_draft["scenarios"]:
        row["name"] = "条件情景"
    for update in record["forward_revision"]["belief_updates"]:
        bid = update["belief_id"]
        parts += [f"### {bid} · {previous._STATUS[update['status']]}", ""]
        if mechanical:
            parts += ["以上仅记录模型申报的信念状态，不代表判断已经改善。原信念、新陈述、修改理由和终稿解释均在"
                      "[过程记录附录](process-record.md)完整保留，需与程序前后对账分别复核。", ""]
        else:
            parts += render_research_block(context.block(explanations[bid]), display_draft, record["effective_forward_calculations"])
    return parts


def render(record):
    draft, calc, sources, request = (record[k] for k in ("effective_forward_draft", "effective_forward_calculations", "source_bundle", "request"))
    final = record["final_report"]
    context = report_context(final, draft, calc, sources, request,
                             changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"])
    return render_with_context(record, context)


def render_with_context(record, context, *, evidence_check=None, mechanical_changes=False, masking=None):
    """Shared report body; a selected-evidence report supplies its own context.

    masking (protocol 24) is the Case's number_masking record, shown in the opening notice."""
    draft, calc, sources, request = (record[k] for k in ("effective_forward_draft", "effective_forward_calculations", "source_bundle", "request"))
    final = record["final_report"]
    # Upstream scenario names are unconstrained old prose. Stable scenario IDs
    # identify current tables; complete original names remain in the appendix.
    display = deepcopy(record)
    for row in display["effective_forward_draft"]["scenarios"]:
        row["name"] = "条件情景"
    for row in display["final_report"]["scenario_assessments"]:
        row["reason"] = context.text(row["reason"])
        row["what_changes_the_view"] = context.text(row["what_changes_the_view"])
    opinions = previous._dispositions(display)
    parts = ["# 投研报告：经营判断、参数变化与反证更新", "",
             f"{_escape(request['symbol'])} · 研究截止日 {_escape(request['as_of'])} · {request['horizon_months']}个月", "",
             "研究草稿，待人工复核。正文数值通过有效计算或标明的来源摘录提供；"
             "引用吻合不证明历史事实、经济解释或预测正确。原始问题和过程文字见附录。", "",
             f"**当前评级：{final['rating']}**", "", "## 结论", ""]
    if evidence_check is not None:
        partial = evidence_check["status"] == "PARTIAL"
        notice = ("**证据待核：完整研究已保存，尚不构成证据绑定完整的交付。模型评级保留供复核，不能视为获准结论。**"
                  if partial else "**来源绑定检查完成；经济含义与关键事实仍待人工复核，不是审计通过或投资判断认证。**")
        if masking:
            whole = sum(r["mode"] == "sentence" for r in masking["sentences"])
            notice += (f"\n\n**终稿有{len(masking['sentences'])}个句子含未能由程序核对的数字，这些数字已替换为“〔数值待核〕”"
                       + (f"（其中{whole}句有文字无法逐个隐去数字，已整体隐去）" if whole else "")
                       + "，Case状态为部分完成；原句保留在过程记录中，待人工核对。**")
        parts[4] = notice
        parts[6] = f"**模型提出的评级：{final['rating']}**"

    def block(value):
        return render_research_block(context.block(value), display["effective_forward_draft"], calc)

    parts += block(final["summary"])
    parts += previous._current_tables(display, opinions)
    for key, label in previous._FINANCE_SECTIONS:
        parts += ["## " + label, "", *block(final["financial_analysis"][key])]
    parts += ["## 最强反证及其影响", "", *block(final["strongest_counterevidence"])]
    if evidence_check is not None:
        for row in final["scenario_assessments"]:
            if row["metrics"]:
                parts += [f"## {row['scenario_id']} · 情景指标", "",
                    *render_research_block({"text": "", "evidence_refs": [], "metrics": row["metrics"]},
                                           display["effective_forward_draft"], calc)]
    if mechanical_changes:
        from .research_changes import render_parameter_facts
        parts += render_parameter_facts(record)
    parts += _corrections(record, context, selected=evidence_check is not None, mechanical=mechanical_changes)
    parts += _belief_updates(record, context, mechanical=mechanical_changes)
    parts += ["## 结论限制", "", *["- " + context.text(t) for t in final["limitations"]], ""]
    parts += previous._sources(record)
    if evidence_check is not None and evidence_check["findings"]:
        reasons = {"THESIS_UNKNOWN_EVIDENCE_BLOCK": "所选证据块不存在，需重新选择原文依据",
                   "THESIS_UNKNOWN_SOURCE_REFERENCE": "来源不存在或不在本次研究资料范围",
                   "RESEARCH_SOURCE_QUOTE_NOT_UNIQUE": "原文未找到唯一匹配，出处仍需核对",
                   "UNBOUND_RESEARCH_NUMBER_PENDING": "未绑定数字：未经程序计算或来源绑定，需核对是否为金融数值"}
        numbers = any(r["reason"] == "UNBOUND_RESEARCH_NUMBER_PENDING" for r in evidence_check["findings"])
        parts += ["## 待核证据项", "",
                  "下列项目未获得可靠的来源定位。相关论述仍在正文完整保留；未核引文不得当作已证实依据。"
                  + ("其中未绑定数字按原文保留，未被识别为时间或登记标签，也未经程序计算，不作为已核数值。" if numbers else ""), "",
                  *_table(("位置", "引用", "问题"), [(r["field"], r["reference"], reasons.get(r["reason"], "证据关联需要复核"))
                      for r in evidence_check["findings"]])]
    return "\n".join(parts).encode("utf-8")


def render_process(record):
    from finauditgate.adapters.thesis_responses import scenario_metric_echoes
    raw = previous.render_process(record)
    response_id = record["exchanges"][-1]["response_id"]
    for call in record["model_calls"]:
        for output in call.get("output", []):
            if output.get("id") != response_id or not isinstance(output.get("content"), str):
                continue
            try:
                echoes = scenario_metric_echoes(json.loads(output["content"]))
            except (ValueError, TypeError):
                continue
            if echoes:
                parts = ["", "## 机械格式归一记录", "",
                         "模型在情景对象中额外重复列出以下指标选择。每一项均已以相同情景和指标在该情景正文中引用，"
                         "因此只合并重复选择元数据；没有删除新指标、数值、理由或评级。原始模型返回仍保存在Case的model_calls中。", "",
                         *previous._json(echoes)]
                return raw + "\n".join(parts).encode("utf-8")
    return raw
