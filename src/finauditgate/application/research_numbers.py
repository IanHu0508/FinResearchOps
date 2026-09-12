"""Bind report metric references to effective forward inputs and calculations.

The caller validates the source references and the draft/calculation pair. This
renderer neither recalculates forecasts nor validates historical claims in model
prose. It deliberately has no model-controlled numeric value or display label.
"""

from types import MappingProxyType

from .forward_report import _number, _table


_METRICS = {
    "revenue": ("营业收入", "amount"),
    "operating_margin": ("经营利润率", "percent"),
    "effective_tax_rate": ("有效所得税率", "percent"),
    "operating_profit": ("经营利润", "amount"),
    "pretax_income": ("税前利润", "amount"),
    "consolidated_net_income": ("合并净利润", "amount"),
    "noncontrolling_attribution_effect": ("少数股东损益归属调节（负数扣减、正数加回）", "signed_amount"),
    "parent_net_income": ("归属于母公司股东的净利润", "amount"),
    "eps_per_traded_unit": ("每交易单位摊薄盈利（EPS）", "price"),
    "operating_cash_flow": ("经营活动现金流", "amount"),
    "cash_after_capex_proxy": ("扣除现金资本购买后的现金流代理", "amount"),
    "exit_price_per_traded_unit": ("条件估值价格", "price"),
    "price_only_break_even_pe": ("维持起点价格所需市盈率", "multiple"),
    "dividend_adjusted_break_even_pe": ("计入股息后打平所需市盈率", "multiple"),
    "return_ex_dividend": ("不含股息累计回报率（非年化）", "return"),
    "return_with_dividend": ("含股息累计回报率（非年化）", "return"),
}
METRIC_KEYS = tuple(_METRICS)
METRIC_NAMES = MappingProxyType({key: name for key, (name, _) in _METRICS.items()})
_INPUT_KEYS = frozenset(("revenue", "operating_margin", "effective_tax_rate"))


def _index(rows, path):
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: expected scenario objects")
        sid = row.get("scenario_id")
        if not isinstance(sid, str) or not sid or sid in indexed:
            raise ValueError(f"{path}: expected unique scenario identifiers")
        indexed[sid] = row
    return indexed


def _validate_block(block):
    if not isinstance(block, dict) or set(block) != {"text", "evidence_refs", "metrics"}:
        raise ValueError("research block: expected exactly text, evidence_refs and metrics")
    if not isinstance(block["text"], str):
        raise ValueError("research block.text: expected a string")
    refs = block["evidence_refs"]
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("research block.evidence_refs: expected nonempty source identifiers")
    if not isinstance(block["metrics"], list):
        raise ValueError("research block.metrics: expected a list")
    for ref in block["metrics"]:
        if not isinstance(ref, dict) or set(ref) != {"scenario_id", "metric"}:
            raise ValueError("research metric: expected exactly scenario_id and metric")
        if not isinstance(ref["scenario_id"], str) or not ref["scenario_id"]:
            raise ValueError("research metric.scenario_id: expected a scenario identifier")
        if not isinstance(ref["metric"], str) or ref["metric"] not in _METRICS:
            raise ValueError("research metric.metric: unsupported metric")


def render_research_block(block, draft, calculations) -> list[str]:
    """Render exact model prose and separately bound, named financial metrics.

Source identifier existence is checked by the caller against the source bundle;
the identifiers occurring in forecast assumptions are not the full source set.
Only this block's metric references are accepted, never supplied values/labels.
The effective draft and calculations must already be validated by the caller.
"""
    _validate_block(block)
    scenarios = _index(draft.get("scenarios", []), "draft.scenarios")
    results = _index(calculations.get("scenario_results", []), "calculations.scenario_results")
    if not results.keys() <= scenarios.keys():
        raise ValueError("calculations.scenario_results: unknown scenario")
    reporting = draft.get("reporting_currency") or "未说明报表币种"
    price = draft.get("price_currency") or "未说明报价币种"
    units = {"amount": f"百万元 {reporting}", "signed_amount": f"百万元 {reporting}",
             "price": f"{price}／交易单位（股票／ADS）", "multiple": "倍",
             "percent": "%", "return": "%（累计，非年化）"}
    rows = []
    selected = set()
    for ref in block["metrics"]:
        sid, key = ref["scenario_id"], ref["metric"]
        if sid not in scenarios:
            raise ValueError("research metric.scenario_id: unknown scenario")
        scenario = scenarios[sid]
        if key in _INPUT_KEYS:
            assumption = scenario.get(key)
            value = assumption.get("value") if isinstance(assumption, dict) else None
        else:
            value = results.get(sid, {}).get(key)
        name, kind = _METRICS[key]
        number = _number(value, percent=kind in {"percent", "return"},
                         signed=kind in {"signed_amount", "return"}, precise=True)
        if kind == "signed_amount" and number != "未能计算" and not number.startswith("-"):
            number = "+" + number
        rows.append((f"{sid} · {scenario.get('name') or '未命名情景'}", name, number, units[kind]))
        selected.add(key)
    parts = [block["text"], ""]
    # Use the same escaped table helper as the metric rows, without changing it.
    if block["evidence_refs"]:
        parts += _table(("模型解释所列来源引用",), [("；".join(block["evidence_refs"]),)])
    if rows:
        parts += [f"预测期间：{draft.get('forecast_start') or '未说明'} 至 "
                  f"{draft.get('forecast_end') or '未说明'}；条件估值日："
                  f"{draft.get('valuation_date') or '未说明'}；起点行情日："
                  f"{draft.get('market_price_date') or '未提供'}。", ""]
        parts += _table(("情景", "指标", "数值", "单位"), rows)
    if "cash_after_capex_proxy" in selected:
        parts += ["现金流代理已扣现金资本购买，不等于股权自由现金流（FCFE）或可分配给股东的现金。", ""]
    if selected & {"price_only_break_even_pe", "dividend_adjusted_break_even_pe"}:
        parts += ["打平所需市盈率仅为给定盈利、价格及股息假设下的条件反推，不是公允倍数或市场共识。", ""]
    if "exit_price_per_traded_unit" in selected:
        parts += ["条件估值价格取决于情景假设，不自动构成公允价值或已认可的目标价。", ""]
    if selected & {"return_ex_dividend", "return_with_dividend"}:
        parts += ["回报为起点行情日至条件估值日的累计回报，非年化；未计投资者税费、交易费用或股息再投资。", ""]
    parts += ["指标数值仅绑定本次有效参数与计算结果；模型解释原样保留，"
              "此绑定不验证自由文字中的历史事实、经济依据或预测准确性。", ""]
    return parts
