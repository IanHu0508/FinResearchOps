"""Conditional annual earnings/cash bridges, with no rating or fair-value claim.

Inputs are model-proposed assumptions. This module checks their shape and does
arithmetic; it does not certify their financial meaning or source support.
Amounts and ordinary shares are in millions. EPS, dividends and prices refer to
one traded unit in the price currency. Returns are cumulative, not annualized.
"""

import calendar
import math
import re
from datetime import date
from decimal import Context, Decimal, localcontext


_BASIS_TYPES = {
    "reported", "company_guidance", "reference_comparison", "analyst_assumption"
}
_SCENARIO_INPUTS = (
    "revenue", "operating_margin", "net_nonoperating_income",
    "effective_tax_rate", "diluted_ordinary_shares",
    "non_working_capital_adjustments", "operating_asset_liability_cash_effect", "cash_capex",
    "fx_reporting_per_price_currency", "exit_pe", "cash_dividend_per_traded_unit",
)
_LIMITATIONS = (
    "盈利与市盈率情景属于条件演算，不能直接视为已经确立的公允价值或投资评级。",
    "扣除资本支出后的金额是现金流代理指标，不等于股权自由现金流（FCFE）或可分配给股东的现金；"
    "市盈率推算价格未额外加上净现金。",
    "标注假设类型和证据引用，不代表已经验证其经济合理性、来源支持或预测准确性。",
    "情景回报以报价币种计算，为累计回报而非年化回报；未计入投资者税费、交易费用或股息再投资。",
    "打平所需市盈率仅是按情景盈利维持起点价格，或计入假设股息后收回起点成本的条件等式；"
    "不代表公允倍数、市场共识或建议采用的期末市盈率。",
    "少数股东损益的盈利或亏损方向及绝对金额由模型提出；程序按所给方向扣除盈利或加回亏损，"
    "不认证该经济归属判断。",
    "经营现金流由合并净利润加上非营运资金调整及经营性资产负债调整推算；"
    "前者包含非现金等调整，后者采用现金流表口径，不等同于严格净营运资金或资产负债表余额变化。",
)


def _text(value, path):
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected a string")
    return value


def _strings(value, path):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{path}: expected a list of strings")
    return list(value)


def _required(mapping, key, path):
    if key not in mapping:
        raise ValueError(f"{path}.{key}: required field missing")
    return mapping[key]


def _assumption(value, path, *, positive=False, nonnegative=False):
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an assumption object")
    basis = _required(value, "basis_type", path)
    if not isinstance(basis, str) or basis not in _BASIS_TYPES:
        raise ValueError(f"{path}.basis_type: unsupported assumption basis")
    _text(_required(value, "reason", path), f"{path}.reason")
    _strings(_required(value, "evidence_refs", path), f"{path}.evidence_refs")
    number = _required(value, "value", path)
    if number is None:
        return None
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise ValueError(f"{path}.value: expected a finite JSON number or null")
    result = Decimal(str(number))
    if not result.is_finite():
        raise ValueError(f"{path}.value: expected a finite JSON number or null")
    if positive and result <= 0:
        raise ValueError(f"{path}.value: must be greater than zero")
    if nonnegative and result < 0:
        raise ValueError(f"{path}.value: must be nonnegative")
    return result


def _date(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{path}: expected an ISO calendar date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path}: invalid calendar date") from exc


def valuation_date_for(as_of: str, horizon_months: int) -> str:
    """Add positive calendar months, clamping the day to the target month end."""
    start = _date(as_of, "as_of")
    if isinstance(horizon_months, bool) or not isinstance(horizon_months, int) or horizon_months <= 0:
        raise ValueError("horizon_months: expected a positive integer")
    year, month_zero = divmod(start.year * 12 + start.month - 1 + horizon_months, 12)
    if not 1 <= year <= 9999:
        raise ValueError("horizon_months: resulting valuation date is outside the supported calendar")
    month = month_zero + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def _calculate(operation, *values):
    return None if any(x is None for x in values) else operation(*values)


def _number(value, path):
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result) or (value != 0 and result == 0):
        raise ValueError(f"{path}: result cannot be represented as a finite JSON number")
    return result


def calculate_forward(draft) -> dict:
    """Calculate each proposed scenario without mutating it or filling gaps.

    Nulls propagate only to dependent calculations. Invalid input shapes,
    non-finite numbers, nonpositive prices/share counts/FX and invalid dates
    raise ValueError with the field path. A nonpositive EPS/PE or a forecast
    period outside 350--380 inclusive days leaves terminal price/returns null.
    Calculation outputs are finite JSON floats or null.
    """
    if not isinstance(draft, dict):
        raise ValueError("draft: expected an object")
    metadata = {}
    for key in ("reporting_currency", "price_currency"):
        value = _text(_required(draft, key, "draft"), key)
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(f"{key}: expected a three-letter uppercase currency code")
        metadata[key] = value
    if _required(draft, "amount_unit", "draft") != "million":
        raise ValueError("amount_unit: only million is supported")
    dates = {}
    for key in ("forecast_start", "forecast_end", "valuation_date"):
        dates[key] = _date(_required(draft, key, "draft"), key)
        metadata[key] = dates[key].isoformat()
    price_date = _required(draft, "market_price_date", "draft")
    if price_date is not None:
        price_date = _date(price_date, "market_price_date")
        if price_date > dates["valuation_date"]:
            raise ValueError("market_price_date: must not follow valuation_date")
    metadata["market_price_date"] = price_date.isoformat() if price_date else None
    forecast_days = (dates["forecast_end"] - dates["forecast_start"]).days + 1
    if forecast_days <= 0:
        raise ValueError("forecast_end: must not precede forecast_start")
    earnings_basis = _required(draft, "earnings_basis", "draft")
    if not isinstance(earnings_basis, str) or earnings_basis not in {
        "trailing_at_valuation", "forward_from_valuation", "fiscal_year"
    }:
        raise ValueError("earnings_basis: expected trailing_at_valuation, forward_from_valuation or fiscal_year")
    if earnings_basis == "trailing_at_valuation" and dates["forecast_end"] != dates["valuation_date"]:
        raise ValueError("forecast_end: trailing_at_valuation requires forecast_end equal to valuation_date")
    if earnings_basis == "forward_from_valuation" and (dates["forecast_start"] - dates["valuation_date"]).days not in (0, 1):
        raise ValueError("forecast_start: forward_from_valuation requires valuation_date or the following day")
    metadata["earnings_basis"] = earnings_basis
    annual = 350 <= forecast_days <= 380
    limitations = _strings(_required(draft, "limitations", "draft"), "limitations")
    if earnings_basis == "fiscal_year":
        limitations.append("本情景使用指定财年盈利，财年起止日未必与估值日对应的过去或未来十二个月重合；比较市盈率或价格时需核对盈利期间口径。")
    scenarios = _required(draft, "scenarios", "draft")
    if not isinstance(scenarios, list):
        raise ValueError("scenarios: expected a list")
    if not scenarios:
        limitations.append("未提供适用的一般企业盈利情景，因此不生成数值预测、价格或回报。")
    price = _assumption(_required(draft, "market_price", "draft"), "market_price", positive=True)
    traded_shares = _assumption(
        _required(draft, "shares_per_traded_unit", "draft"),
        "shares_per_traded_unit", positive=True,
    )
    results = []
    seen = set()
    # A new Context prevents the caller's precision, rounding or Inexact traps
    # from changing these results. Financial conclusions are outside this math.
    with localcontext(Context(prec=34)):
        for index, scenario in enumerate(scenarios):
            path = f"scenarios[{index}]"
            if not isinstance(scenario, dict):
                raise ValueError(f"{path}: expected an object")
            scenario_id = _text(_required(scenario, "scenario_id", path), f"{path}.scenario_id")
            if not re.fullmatch(r"F[1-9]\d*", scenario_id) or scenario_id in seen:
                raise ValueError(f"{path}.scenario_id: expected a unique F1, F2, ... identifier")
            seen.add(scenario_id)
            name = _text(_required(scenario, "name", path), f"{path}.name")
            for key in ("drivers", "valuation_reasoning", "evidence_that_changes_case"):
                _text(_required(scenario, key, path), f"{path}.{key}")
            values = {
                key: _assumption(
                    _required(scenario, key, path), f"{path}.{key}",
                    positive=key in ("diluted_ordinary_shares", "fx_reporting_per_price_currency"),
                    nonnegative=key in ("cash_capex", "cash_dividend_per_traded_unit"),
                )
                for key in _SCENARIO_INPUTS
            }
            attribution_path = f"{path}.noncontrolling_attribution"
            attribution = _required(scenario, "noncontrolling_attribution", path)
            if not isinstance(attribution, dict):
                raise ValueError(f"{attribution_path}: expected an object")
            nature = _required(attribution, "nature", attribution_path)
            if not isinstance(nature, str) or nature not in {"profit", "loss", "unknown"}:
                raise ValueError(f"{attribution_path}.nature: expected profit, loss or unknown")
            attribution_amount = _assumption(
                _required(attribution, "amount", attribution_path),
                f"{attribution_path}.amount", nonnegative=True,
            )
            attribution_effect = None
            if attribution_amount is not None and nature != "unknown":
                attribution_effect = -attribution_amount if nature == "profit" else attribution_amount
            missing = [key for key, value in values.items() if value is None]
            if nature == "unknown":
                missing.append("noncontrolling_attribution.nature")
            if attribution_amount is None:
                missing.append("noncontrolling_attribution.amount")
            missing += [key for key, value in (
                ("market_price", price), ("shares_per_traded_unit", traded_shares)
            ) if value is None]
            if price_date is None:
                missing.append("market_price_date")
            notes = []
            if nature == "unknown":
                notes.append("少数股东损益的盈利或亏损归属方向未知，暂不计算归母盈利及依赖该盈利的每股收益、价格和回报；合并口径现金流仍可计算。")
            if price_date is None and price is not None:
                notes.append("市场价格缺少报价日期；保留条件盈利与价格计算，但不计算相对该报价的回报。")
            if price_date == dates["valuation_date"]:
                notes.append("报价日与估值日相同，持有期为零；保留条件价格，但不将非正持有期的价格差计为累计投资回报。")
            comparison_price = price if price_date is not None and price_date < dates["valuation_date"] else None
            op = _calculate(lambda a, b: a * b, values["revenue"], values["operating_margin"])
            pretax = _calculate(lambda a, b: a + b, op, values["net_nonoperating_income"])
            consolidated = _calculate(lambda a, b: a * (1 - b), pretax, values["effective_tax_rate"])
            parent = _calculate(lambda a, b: a + b, consolidated, attribution_effect)
            eps = _calculate(
                lambda earnings, shares, units, fx: earnings / shares * units / fx,
                parent, values["diluted_ordinary_shares"], traded_shares,
                values["fx_reporting_per_price_currency"],
            )
            cfo = _calculate(
                lambda income, adjustments, operating_assets_liabilities: income + adjustments + operating_assets_liabilities,
                consolidated, values["non_working_capital_adjustments"], values["operating_asset_liability_cash_effect"],
            )
            cash_proxy = _calculate(lambda a, b: a - b, cfo, values["cash_capex"])
            exit_price = None
            if not annual:
                notes.append("预测期包含首尾日后的长度不在350至380天范围内，不使用年度市盈率推算价格或回报。")
            if eps is not None and eps <= 0:
                notes.append("每交易单位盈利不大于零，不使用市盈率推算价格。")
            if values["exit_pe"] is not None and values["exit_pe"] <= 0:
                notes.append("期末市盈率假设不大于零，无法据此计算条件价格。")
            if annual and eps is not None and eps > 0 and values["exit_pe"] is not None and values["exit_pe"] > 0:
                exit_price = eps * values["exit_pe"]
            price_only_break_even_pe = None
            dividend_adjusted_break_even_pe = None
            if annual and eps is not None and eps > 0 and comparison_price is not None:
                price_only_break_even_pe = comparison_price / eps
                dividend_adjusted_break_even_pe = _calculate(
                    lambda initial_price, dividend, earnings: (initial_price - dividend) / earnings,
                    comparison_price, values["cash_dividend_per_traded_unit"], eps,
                )
                if dividend_adjusted_break_even_pe is not None and dividend_adjusted_break_even_pe < 0:
                    notes.append("股息假设已超过起点价格并覆盖成本；含股息打平倍数保留其负数代数结果，不能理解为负市盈率估值。")
            if pretax is not None and pretax < 0:
                notes.append("税前亏损的税项仍按所给有效税率机械计算；由此产生的税收利益能否实现尚未验证。")
            if values["effective_tax_rate"] is not None and not 0 <= values["effective_tax_rate"] <= 1:
                notes.append("有效税率不在0至100%范围内，需要解释该税率假设的经济依据。")
            computed = {
                "operating_profit": op,
                "pretax_income": pretax,
                "consolidated_net_income": consolidated,
                "noncontrolling_attribution_effect": attribution_effect,
                "parent_net_income": parent,
                "eps_per_traded_unit": eps,
                "operating_cash_flow": cfo,
                "cash_after_capex_proxy": cash_proxy,
                "exit_price_per_traded_unit": exit_price,
                "price_only_break_even_pe": price_only_break_even_pe,
                "dividend_adjusted_break_even_pe": dividend_adjusted_break_even_pe,
                "return_ex_dividend": _calculate(lambda a, b: a / b - 1, exit_price, comparison_price),
                "return_with_dividend": _calculate(
                    lambda a, dividend, b: (a + dividend) / b - 1,
                    exit_price, values["cash_dividend_per_traded_unit"], comparison_price,
                ),
            }
            results.append({
                "scenario_id": scenario_id, "name": name,
                **{key: _number(value, f"{path}.{key}") for key, value in computed.items()},
                "missing_inputs": missing, "limitations": notes,
            })
    return {
        "method": "ordinary_company_annual_earnings_bridge",
        **metadata, "amount_unit": "million", "forecast_days": forecast_days,
        "annual_earnings_period": annual,
        "scenario_results": results,
        "limitations": limitations + list(_LIMITATIONS),
    }
