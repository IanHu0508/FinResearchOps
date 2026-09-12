"""Render forward assumptions, arithmetic and adoption as a report section."""

import math


_BASIS = {
    "reported": "模型标注：披露来源",
    "company_guidance": "模型标注：公司指引",
    "reference_comparison": "模型标注：参考比较",
    "analyst_assumption": "模型标注：研究假设",
}
_EARNINGS_BASIS = {
    "trailing_at_valuation": "估值日滚动年度盈利",
    "forward_from_valuation": "估值日之后年度盈利",
    "fiscal_year": "指定财年盈利",
}
_DISPOSITION = {"use": "采用", "conditional": "有条件采用", "reject": "已拒绝"}
_ATTRIBUTION_LABELS = {
    "profit": "少数股东盈利归属额（从合并净利扣除）",
    "loss": "少数股东亏损归属额（加回）",
    "unknown": "少数股东归属额（盈利或亏损性质未知）",
}
_PARAMETERS = (
    ("revenue", "收入", "amount"),
    ("operating_margin", "经营利润率", "percent"),
    ("net_nonoperating_income", "非经营净收益（损失为负）", "amount"),
    ("effective_tax_rate", "有效税率", "percent"),
    ("noncontrolling_attribution", "少数股东归属", "amount"),
    ("diluted_ordinary_shares", "摊薄普通股数", "shares"),
    ("non_working_capital_adjustments", "非营运资金调整（含非现金等）", "amount"),
    ("operating_asset_liability_cash_effect", "经营性资产负债现金影响（非严格NWC或余额变化）", "amount"),
    ("cash_capex", "现金资本购买", "amount"),
    ("fx_reporting_per_price_currency", "汇率", "fx"),
    ("exit_pe", "期末条件市盈率", "multiple"),
    ("cash_dividend_per_traded_unit", "期间现金股息／交易单位", "price"),
)


def _number(value, *, percent=False, signed=False, precise=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "未能计算"
    if percent:
        return format(value, "+.2%" if signed else ".2%")
    if precise:
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return format(value, "+,.2f" if signed else ",.2f")


def _cell(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "&#124;").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"))


def _table(headers, rows):
    return ["| " + " | ".join(_cell(v) for v in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows], ""]


def _value(assumption, kind, reporting, price):
    value = assumption.get("value")
    formatted = _number(value, percent=kind == "percent", precise=kind != "percent")
    if formatted == "未能计算" or kind == "percent":
        return formatted
    unit = {"amount": f"百万元 {reporting}", "shares": "百万普通股",
            "fx": f"{reporting}／{price}", "multiple": "倍",
            "price": f"{price}／交易单位", "conversion": "普通股／交易单位"}[kind]
    return formatted + " " + unit


def _assumption_row(label, assumption, kind, reporting, price):
    return (label, _value(assumption, kind, reporting, price),
            _BASIS.get(assumption.get("basis_type"), "未说明"),
            assumption.get("reason", "") or "未说明",
            ", ".join(assumption.get("evidence_refs", [])) or "未列引用")


def render_forward(draft, calculations, assessment) -> list[str]:
    """Render stored values only; do not infer a rating or mutate any input."""
    reporting = draft.get("reporting_currency") or "未说明报表币种"
    price = draft.get("price_currency") or "未说明报价币种"
    scenarios = draft.get("scenarios", [])
    results = {r["scenario_id"]: r for r in calculations.get("scenario_results", [])}
    opinions = {a["scenario_id"]: a for a in assessment.get("scenario_assessments", [])}
    earnings_basis = _EARNINGS_BASIS.get(draft.get("earnings_basis"), "未说明盈利期间口径")
    parts = ["## 前瞻推演：经营、现金与条件回报", "",
             f"预测期间：{draft.get('forecast_start') or '未说明'} 至 {draft.get('forecast_end') or '未说明'}；"
             f"条件估值日：{draft.get('valuation_date') or '未说明'}；"
             f"起点行情日：{draft.get('market_price_date') or '未提供'}。", "",
             f"估值采用的盈利口径：{earnings_basis}。预测期间是收入、利润和现金流对应的区间；"
             "条件估值日是把该盈利口径用于价格推演的时点，两者不等同。", "",
             f"金额单位：百万元 {reporting}；股数单位：百万普通股。EPS、股息和价格均为每交易单位（股票／ADS）的 {price}；"
             "回报为起点价格至条件估值日的累计回报，非年化。", ""]
    if scenarios:
        operating_rows, pricing_rows = [], []
        for scenario in scenarios:
            sid = scenario["scenario_id"]
            result = results.get(sid, {})
            disposition = _DISPOSITION.get(opinions.get(sid, {}).get("disposition"), "未评估")
            name = f"{sid} · {scenario['name']}（{disposition}）"
            operating_rows.append((name, _number(scenario.get("revenue", {}).get("value")),
                _number(scenario.get("operating_margin", {}).get("value"), percent=True),
                _number(result.get("operating_profit")), _number(result.get("parent_net_income")),
                _number(result.get("eps_per_traded_unit")), _number(result.get("operating_cash_flow")),
                _number(result.get("cash_after_capex_proxy"))))
            pricing_rows.append((name, _number(scenario.get("exit_pe", {}).get("value")),
                _number(result.get("exit_price_per_traded_unit")),
                _number(result.get("return_ex_dividend"), percent=True, signed=True),
                _number(result.get("return_with_dividend"), percent=True, signed=True),
                _number(result.get("price_only_break_even_pe")),
                _number(result.get("dividend_adjusted_break_even_pe"))))
        parts += _table(("情景与采纳", "收入", "经营利润率", "经营利润", "归母净利",
                         f"EPS（{price}／交易单位）", "经营现金流", "扣资本购买现金代理"), operating_rows)
        parts += _table(("情景与采纳", "条件 PE（倍）", f"条件价格（{price}／交易单位）",
                         "不含股息累计回报", "含股息累计回报", "维持起点股价所需PE（倍）",
                         "含股息打平所需PE（倍）"), pricing_rows)
        parts += ["两列所需 PE 是给定盈利与股息假设下的条件反推，不是推荐或公允倍数，不能证明市场共识或股价便宜。", ""]
        parts += ["扣资本购买现金代理已扣现金资本购买，不等同于 FCFE 或可全部分配给股东的现金。"
                  "条件价格由对应情景推算，不自动构成公允价值、目标价或评级；已拒绝情景的数值仅保留作过程依据。", ""]
    else:
        parts += ["当前没有可列示的量化情景；以下保留经济分析及具体缺口。", ""]
    parts += ["### 对投资判断的经济含义", "", assessment.get("economic_conclusion", "") or "未提供经济结论。", "",
              assessment.get("confidence_and_limits", "") or "未说明结论的适用范围。", "",
              "### 共同口径与输入依据", "", draft.get("business_model", "") or "未说明经营模型。", "",
              "以下参数表的依据类型由模型声明；引用存在不证明预测或假设成立。", ""]
    parts += _table(("参数", "取值与单位", "模型标注的依据类型", "采用理由", "引用"), [
        _assumption_row("起点市场价格", draft.get("market_price", {}), "price", reporting, price),
        _assumption_row("每交易单位对应普通股数", draft.get("shares_per_traded_unit", {}), "conversion", reporting, price),
    ])
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        result, opinion = results.get(sid, {}), opinions.get(sid, {})
        disposition = _DISPOSITION.get(opinion.get("disposition"), "未评估")
        parts += [f"### {sid} · {scenario['name']}：{disposition}", "",
                  "经营驱动：" + scenario.get("drivers", ""), "",
                  "估值推理：" + scenario.get("valuation_reasoning", ""), "",
                  "终判采纳理由：" + (opinion.get("reason", "") or "未提供"), "",
                  "终判何时改变：" + (opinion.get("what_changes_the_view", "") or "未提供"), "",
                  "情景需要更新的证据：" + scenario.get("evidence_that_changes_case", ""), ""]
        parameter_rows = []
        for key, label, kind in _PARAMETERS:
            assumption = scenario.get(key, {})
            if key == "noncontrolling_attribution":
                label = _ATTRIBUTION_LABELS.get(assumption.get("nature"), _ATTRIBUTION_LABELS["unknown"])
                assumption = assumption.get("amount", {})
            parameter_rows.append(_assumption_row(label, assumption, kind, reporting, price))
        parts += _table(("参数", "取值与单位", "模型标注的依据类型", "采用理由", "引用"), parameter_rows)
        parts += [f"利润桥（百万元 {reporting}）：经营利润 {_number(result.get('operating_profit'))} → "
                  f"税前利润 {_number(result.get('pretax_income'))} → 合并净利润 {_number(result.get('consolidated_net_income'))} → "
                  f"少数股东归属调节 {_number(result.get('noncontrolling_attribution_effect'), signed=True)} → "
                  f"归母净利润 {_number(result.get('parent_net_income'))}。", ""]
        missing = result.get("missing_inputs", [])
        if missing:
            labels = {key: label for key, label, _kind in _PARAMETERS}
            labels.update(market_price="起点市场价格", shares_per_traded_unit="每交易单位对应普通股数")
            parts += ["尚缺计算输入：" + "；".join(labels.get(key, key) for key in missing) + "。", ""]
        if not result:
            parts += ["该情景未提供计算结果。", ""]
        if result.get("limitations"):
            parts += ["本情景的计算限制：", "", *["- " + item for item in result["limitations"]], ""]
    limitations = list(dict.fromkeys([*draft.get("limitations", []), *calculations.get("limitations", [])]))
    if limitations:
        parts += ["### 共同限制与待补资料", "", *["- " + item for item in limitations], ""]
    return parts
