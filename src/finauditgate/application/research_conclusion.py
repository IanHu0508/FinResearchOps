"""Protocol 23: how a Case presents its conclusion and the rule's reference rating."""

RATINGS = {"Buy": "买入", "Overweight": "增持", "Hold": "中性", "Underweight": "减持", "Sell": "卖出"}
CONFIDENCE = {"high": "高", "medium": "中", "low": "低"}
RULE_NOTE = ("规则参考评级由程序按固定规则计算：取各情景到估值日的回报（情景给出股息时含股息），按研究期限换算为年化，"
             "等权平均并取整到0.01%后分档：不低于20%为买入，5%至20%为增持，−5%至5%为中性，−20%至−5%为减持，"
             "不高于−20%为卖出。它可由已保存的复算结果重算，不是公允价值、情景概率或交易指令；模型评级可以偏离它。")
_NOT_COMPUTABLE = {"NO_SCENARIOS": "无法计算（没有前瞻情景）",
                   "SCENARIO_RETURN_MISSING": "无法计算（有情景没有可复算的回报，例如未给退出市盈率）"}


def rule_text(rule):
    """One line for the rule's reference rating; the numbers come from the saved calculations."""
    if rule["status"] != "COMPUTED":
        return _NOT_COMPUTABLE[rule["reason"]]
    period = "" if rule["horizon_months"] == 12 else f"（{rule['horizon_months']}个月期限年化）"
    return (f"{RATINGS[rule['rating']]}（{rule['rating']}）：{len(rule['scenario_returns'])}个情景回报"
            f"等权平均{rule['expected_return']:+.2%}{period}")


def rating_text(rating, confidence=None):
    label = f"{RATINGS.get(rating, rating)}（{rating}）"
    return label if confidence is None else f"{label}，置信度{CONFIDENCE[confidence]}"
