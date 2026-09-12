"""Render current research separately from the preserved proposal history.

Only the effective forecast feeds the main tables and bound metric references.
This module does not calculate, edit a proposal, or certify model-written prose.
Released older report renderers are deliberately left unchanged.
"""

import json
import re

from finauditgate.application.forward_report import (
    _ATTRIBUTION_LABELS, _DISPOSITION, _EARNINGS_BASIS, _PARAMETERS, _number,
    _table, _value, render_forward,
)
from finauditgate.application.research_numbers import render_research_block


_STATUS = {"maintain": "维持", "revise": "修改", "withdraw": "撤回", "unresolved": "未解决"}
_PARAMETER_INFO = {key: (label, kind) for key, label, kind in _PARAMETERS}
_PARAMETER_INFO.update(market_price=("起点市场价格", "price"),
                       shares_per_traded_unit=("每交易单位对应普通股数", "conversion"))
_FINANCE_SECTIONS = (
    ("operating_performance", "经营表现与持续性"),
    ("earnings_quality", "盈利质量与归母勾稽"),
    ("cash_and_capital_allocation", "现金创造与资本配置"),
    ("valuation_and_price_requirements", "估值与当前价格要求"),
)
_EFFECTIVE_INPUTS = (
    "net_nonoperating_income", "effective_tax_rate", "noncontrolling_attribution",
    "diluted_ordinary_shares", "fx_reporting_per_price_currency",
    "cash_dividend_per_traded_unit", "non_working_capital_adjustments",
    "operating_asset_liability_cash_effect", "cash_capex", "exit_pe",
)


def _quote(value):
    return "> " + str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\n> ")


def _refs(values):
    return "；".join(values) or "未列直接来源"


def _dispositions(record):
    return {row["scenario_id"]: row for row in record["final_report"]["scenario_assessments"]}


def _state(sid, opinions):
    return _DISPOSITION.get(opinions.get(sid, {}).get("disposition"), "未评估")


def _block(block, record, opinions):
    parts = []
    sids = list(dict.fromkeys(ref["scenario_id"] for ref in block["metrics"]))
    if sids:
        parts += ["本段量化引用的当前状态：" + "；".join(f"{sid} · {_state(sid, opinions)}" for sid in sids)
                  + "。已拒绝路径只用于说明分歧，不代表采用。", ""]
    parts += render_research_block(block, record["effective_forward_draft"], record["effective_forward_calculations"])
    return parts


def _request(record):
    request = record["request"]
    parts = ["## 研究任务与输入", "", "研究问题：", "", _quote(request["question"]), ""]
    for key, label in (("hypotheses", "待检验假设（不是已验证事实）"),
                       ("research_constraints", "研究约束")):
        if request.get(key):
            parts += [label + "：", "", *[_quote(value) for value in request[key]], ""]
    if request.get("user_view") is not None:
        parts += ["用户原始期待（仅记录；未进入主研究请求）：", "", _quote(request["user_view"]), ""]
    return parts


def _current_tables(record, opinions):
    draft, calculations = record["effective_forward_draft"], record["effective_forward_calculations"]
    results = {row["scenario_id"]: row for row in calculations["scenario_results"]}
    reporting, price = draft["reporting_currency"], draft["price_currency"]
    parts = ["## 当前有效情景与计算", "",
             "有效表示本次明确修改已应用并复算，不表示假设已被验证；采纳状态由终判单独说明。", "",
             f"预测期间：{draft['forecast_start']} 至 {draft['forecast_end']}；"
             f"条件估值日：{draft['valuation_date']}；起点行情日：{draft['market_price_date'] or '未提供'}。", "",
             "盈利口径：" + _EARNINGS_BASIS.get(draft["earnings_basis"], draft["earnings_basis"]) + "。", "",
             f"金额为百万元 {reporting}；EPS、价格、股息为每交易单位（股票／ADS）的 {price}；"
             "回报为持有期间累计值，非年化。", ""]
    operating, pricing = [], []
    for scenario in draft["scenarios"]:
        sid = scenario["scenario_id"]
        row = results[sid]
        name = f"{sid} · {scenario['name']}（{_state(sid, opinions)}）"
        operating.append((name, _number(scenario["revenue"]["value"]),
                          _number(scenario["operating_margin"]["value"], percent=True),
                          _number(row["consolidated_net_income"]), _number(row["parent_net_income"]),
                          _number(row["eps_per_traded_unit"]), _number(row["operating_cash_flow"]),
                          _number(row["cash_after_capex_proxy"])))
        pricing.append((name, _number(scenario["exit_pe"]["value"]), _number(row["exit_price_per_traded_unit"]),
                        _number(row["return_ex_dividend"], percent=True, signed=True),
                        _number(row["return_with_dividend"], percent=True, signed=True),
                        _number(row["price_only_break_even_pe"]), _number(row["dividend_adjusted_break_even_pe"])))
    if operating:
        parts += _table(("情景与采纳", "营业收入", "经营利润率", "合并净利润", "归母净利润",
                         f"EPS（{price}／交易单位）", "经营现金流", "扣资本购买现金代理"), operating)
        parts += _table(("情景与采纳", "条件PE（倍）", f"条件价格（{price}／交易单位）",
                         "不含股息累计回报", "含股息累计回报", "维持起点价格所需PE（倍）",
                         "含股息打平所需PE（倍）"), pricing)
    else:
        parts += ["未形成适用的量化情景；保留经营研究和具体信息缺口。", ""]
    parts += _effective_inputs(record, opinions)
    parts += ["已拒绝情景仍显示以供核对，其数字不是获采纳预测。现金代理已扣资本购买，不等于股权自由现金流或可全部分配现金。"
              "条件价格不是公允价值认证；打平PE是给定盈利和股息的条件反推，不是合理倍数或市场共识。", ""]
    for scenario in draft["scenarios"]:
        sid = scenario["scenario_id"]
        opinion = opinions.get(sid, {})
        parts += [f"### {sid} · {_state(sid, opinions)}", "", "终判理由：" + opinion.get("reason", "未提供"), "",
                  "什么会改变判断：" + opinion.get("what_changes_the_view", "未提供"), ""]
        row = results[sid]
        if row.get("missing_inputs"):
            labels = {key: info[0] for key, info in _PARAMETER_INFO.items()}
            labels.update({"market_price_date": "起点行情日期",
                           "noncontrolling_attribution.nature": "少数股东盈利或亏损性质",
                           "noncontrolling_attribution.amount": "少数股东归属金额"})
            parts += ["未满足的计算输入：" + "；".join(labels.get(key, key) for key in row["missing_inputs"]) + "。", ""]
        if row.get("limitations"):
            parts += [*['- ' + item for item in row["limitations"]], ""]
    return parts


def _input_value(field, value, draft):
    label, kind = _PARAMETER_INFO[field]
    if field == "noncontrolling_attribution":
        label = _ATTRIBUTION_LABELS[value["nature"]]
        value = value["amount"]
    return label, _value(value, kind, draft["reporting_currency"], draft["price_currency"])


def _input_reason(field, value):
    assumption = value["amount"] if field == "noncontrolling_attribution" else value
    return assumption["reason"], _refs(assumption["evidence_refs"])


def _effective_inputs(record, opinions):
    draft = record["effective_forward_draft"]

    def input_row(field, value):
        label, display = _input_value(field, value, draft)
        assumption = value["amount"] if field == "noncontrolling_attribution" else value
        return label, display, _refs(assumption["evidence_refs"])

    parts = ["### 当前计算实际采用的输入", "",
             "下表直接列示有效输入，来源编号是模型所列依据，不证明预测成立。股数和汇率是对应未来情景的假设；"
             "历史期间股数或便利汇率只适用于相应历史参考计算。现金股息是持有期累计每交易单位金额。"
             "完整输入理由见[过程记录附录](process-record.md)。", ""]
    parts += _table(("共同参数", "取值与单位", "来源引用"),
                    [input_row(field, draft[field]) for field in ("market_price", "shares_per_traded_unit")])
    for scenario in draft["scenarios"]:
        sid = scenario["scenario_id"]
        parts += [f"#### {sid} · {_state(sid, opinions)}：有效参数", ""]
        parts += _table(("参数", "取值与单位", "来源引用"),
                        [input_row(field, scenario[field]) for field in _EFFECTIVE_INPUTS])
    parts += ["模型范围：本年度盈利桥只建模所列税费和少数股东损益归属；其他归属调整或摊薄项目未单列时须另行核对。"
              "稀释普通股数、股票／ADS比例和汇率按表中假设计算，不构成法定摊薄EPS认证。", ""]
    return parts


def _corrections(record):
    draft = record["effective_forward_draft"]
    parts = ["## 参数修正与尚未解决的问题", "",
             "以下差异记录原输入与本次有效输入；原输入仅供追溯，不能当作当前采纳。修正理由仍是模型提案，不构成来源真实性认证。", ""]
    for edit in record["applied_changes"]:
        sid, field = edit["scenario_id"], edit["field"]
        scope = sid or "共同参数"
        before_label, before_value = _input_value(field, edit["before"], draft)
        after_label, after_value = _input_value(field, edit["after"], draft)
        old_reason, old_refs = _input_reason(field, edit["before"])
        new_reason, new_refs = _input_reason(field, edit["after"])
        parts += [f"### {scope} · {_PARAMETER_INFO[field][0]}", ""]
        parts += _table(("版本", "口径", "取值与单位", "原样依据说明", "来源引用"),
                        [("原输入（已替换）", before_label, before_value, old_reason, old_refs),
                         ("当前有效输入", after_label, after_value, new_reason, new_refs)])
        parts += ["修正类型：" + edit["correction_basis"], "", "修正理由：" + edit["reason"], "",
                  "修正引用：" + _refs(edit["evidence_refs"]), ""]
    if not record["applied_changes"]:
        parts += ["本轮没有应用参数修改；有效输入沿用原提案，不代表原提案已获全面验证。", ""]
    issues = record["forward_revision"].get("unresolved_issues", [])
    if issues:
        parts += ["尚未解决：", "", *["- " + str(issue) for issue in issues], ""]
    return parts


def _belief_updates(record):
    parts = ["## 旧信念为何维持或改变", "",
             "比较的是同一研究时点内的独立初判与最终判断，不是历史评级；终判不读取初判评级或摘要。", ""]
    comparison = record["rating_comparison"]
    parts += [f"独立初判：{comparison['before']}；当前终判：{comparison['after']}。评级未变不代表信念未更新。", ""]
    originals = {row["belief_id"]: row for row in record["independent_assessment"]["beliefs"]}
    for update in record["forward_revision"]["belief_updates"]:
        bid = update["belief_id"]
        original = originals[bid]
        parts += [f"### {bid} · {_STATUS[update['status']]}", "",
                  "原独立信念（过程记录）：" + original["statement"], "",
                  "更新依据：" + update["reason"], "",
                  "对金融判断的影响：" + update["financial_implication"], "",
                  "来源引用：" + _refs(update["evidence_refs"]), ""]
        if update.get("new_statement"):
            parts += ["本次修正表述：" + update["new_statement"], ""]
        if original.get("would_change_mind"):
            parts += ["初判记录的后续观察条件（不是终判硬门槛）：" + original["would_change_mind"], ""]
    return parts


def _sources(record):
    parts = ["## 来源目录与过程记录", "",
             "来源编号用于定位资料，不证明所引论断正确。原提案、全部原参数及计算、经理/交易员过程文本与初始论点见"
             "[过程记录附录](process-record.md)；完整结构化记录见[Case](case.json)。", ""]
    parts += _table(("编号", "来源", "时间可得性与限制", "用途"),
                    [(source["id"], source["origin"], source["availability_note"],
                      "报告附录情景，未进入主判断" if source.get("use") == "sensitivity" else "研究资料")
                     for source in record["source_bundle"]["sources"]])
    return parts


def render(record) -> bytes:
    """Render the compact current report using only effective metric bindings."""
    request, final = record["request"], record["final_report"]
    opinions = _dispositions(record)
    parts = ["# 投研报告：经营判断、参数修正与反证更新", "",
             f"{request['symbol']} · 研究截止日 {request['as_of']} · {request['horizon_months']}个月", "",
             "研究草稿，待人工复核。程序绑定预测数字不等于验证历史事实、预测或投资判断；未连接交易执行。", "",
             f"资料模式：{request['data_mode']}；冻结资料不等于本日新抓取行情。", "",
             f"**当前评级：{final['rating']}**", "", "## 结论", ""]
    parts += _block(final["summary"], record, opinions)
    parts += _current_tables(record, opinions)
    for key, label in _FINANCE_SECTIONS:
        parts += ["## " + label, "", *_block(final["financial_analysis"][key], record, opinions)]
    parts += ["## 最强反证及其影响", "", *_block(final["strongest_counterevidence"], record, opinions)]
    parts += _corrections(record)
    parts += _belief_updates(record)
    parts += ["## 结论限制", ""]
    # The calculator also carries the draft's original limitation prose. Keep
    # that full record in the appendix rather than silently readopting old text.
    limits = list(dict.fromkeys(final["limitations"]))
    parts += ["- " + item for item in limits] + [""]
    parts += _request(record)
    parts += _sources(record)
    return "\n".join(parts).encode("utf-8")


def _json(value):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    longest = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "json", text, fence, ""]


def render_process(record) -> bytes:
    """Preserve the full original proposal separately from current conclusions."""
    parts = ["# 投研过程记录附录", "",
             "本附录保存原提案与过程输出，不代表当前采纳。部分旧数字或推断已被修正或拒绝；"
             "当前有效参数、计算和终判请阅读[主报告](report.md)。", ""]
    parts += _request(record)
    parts += ["## 原始前瞻提案（不是当前有效稿）", ""]
    parts += render_forward(record["forward_draft"], record["forward_calculations"], {
        "economic_conclusion": "这里只展示修正前提案的计算，不赋予其修正后的采纳状态。",
        "confidence_and_limits": "请以主报告的有效计算和终判为准，勿把原表作为当前投资结论。",
        "scenario_assessments": [],
    })
    for label, key in (("原始前瞻输入完整记录", "forward_draft"),
                       ("原始程序计算完整记录", "forward_calculations"),
                       ("修正响应完整记录", "forward_revision"),
                       ("实际应用的差异", "applied_changes"),
                       ("当前有效输入完整记录", "effective_forward_draft"),
                       ("当前有效计算完整记录", "effective_forward_calculations"),
                       ("独立初判原记录", "independent_assessment"),
                       ("多空初稿", "initial"), ("多空反证更新", "revisions"),
                       ("研究经理原提案", "research_evaluation"),
                       ("交易员执行条件原提案", "execution_review"), ("风险研究原记录", "risk_briefs")):
        if key in record:
            parts += ["## " + label, "", *_json(record[key])]
    for key, label in (("fundamentals_report", "基本面过程输出"), ("market_report", "市场过程输出"),
                       ("investment_plan", "研究经理过程文本"), ("trader_investment_plan", "交易员过程文本")):
        if key in record.get("reports", {}):
            parts += ["## " + label, "", record["reports"][key], ""]
    for source in record["source_bundle"]["sources"]:
        if source.get("use") == "sensitivity":
            parts += ["## 报告附录情景：" + source["id"], "",
                      "编排者的条件情景仅作附录展示，未进入主判断，不是获采纳目标价或估值。", "", source["content"], ""]
    parts += _sources(record)
    return "\n".join(parts).encode("utf-8")
