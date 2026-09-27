"""Fixed four-analyst contract; runtime schemas are loaded only on demand."""

import re


ANALYSTS = {
    "Fundamentals Analyst": "fundamentals_report",
    "Market Analyst": "market_report",
    "News Analyst": "news_report",
    "Sentiment Analyst": "sentiment_report",
}
KEYS = ("fundamentals", "market", "news", "social")
REPORT_FIELDS = (*ANALYSTS.values(), "investment_plan", "trader_investment_plan", "final_trade_decision")
_RESEARCHERS = ("Bull Researcher", "Bear Researcher")
_RISKS = ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst")


def main_stages(version):
    if type(version) is not int or version not in (10, 11, 13, 16, 17):
        raise ValueError("THESIS_PROTOCOL_VERSION_INVALID")
    stages = [(node, "InitialBrief") for node in _RESEARCHERS]
    stages += [(node, "RevisionBrief") for node in _RESEARCHERS]
    stages += [("Research Manager", "ResearchEvaluation"), ("Trader", "ExecutionReview")]
    stages += [(node, "RiskBrief") for node in _RISKS]
    stages += [("Portfolio Manager", "IndependentAssessment"), ("Portfolio Manager", "UnderwritingDraft")]
    stages += ([("Portfolio Manager", "FinalAssessment")] if version == 10 else
               [("Portfolio Manager", "ForwardRevision"), ("Portfolio Manager", "FinalResearchReport")])
    return [(node, "AnalystReport") for node in ANALYSTS] + stages if version == 17 else stages


def expected_topology():
    """The pinned graph with fundamentals, market, news, social in that order."""
    nodes = [*ANALYSTS, *_RESEARCHERS, "Research Manager", "Trader", *_RISKS, "Portfolio Manager"]
    edges = []

    def add(source, target, conditional=False):
        edges.append({"source": source, "target": target, "conditional": conditional})

    labels = list(ANALYSTS)
    add("__start__", labels[0])
    for i, (node, key) in enumerate(zip(labels, KEYS)):
        tool = "tools_" + key
        clear = "Msg Clear " + node.removesuffix(" Analyst")
        nodes.extend((tool, clear))
        add(node, tool, True)
        add(node, clear, True)
        add(tool, node)
        add(clear, labels[i + 1] if i + 1 < len(labels) else "Bull Researcher")
    for node in _RESEARCHERS:
        for target in (*_RESEARCHERS, "Research Manager"):
            add(node, target, True)
    add("Research Manager", "Trader")
    add("Trader", "Aggressive Analyst")
    for node in _RISKS:
        for target in (*_RISKS, "Portfolio Manager"):
            add(node, target, True)
    add("Portfolio Manager", "__end__")
    return {"nodes": sorted([*nodes, "__start__", "__end__"]),
            "edges": sorted(edges, key=lambda e: (e["source"], e["target"], e["conditional"]))}


def schemas():
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field

    class Observation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        statement: str = Field(min_length=1)
        evidence_refs: list[str] = Field(min_length=1, max_length=48)

    class AnalystReport(BaseModel):
        model_config = ConfigDict(extra="forbid")
        analysis: str = Field(min_length=1)
        observations: list[Observation] = Field(max_length=12)
        coverage: Literal["available", "partial", "unavailable"]
        limits: list[str] = Field(max_length=12)
        evidence_refs: list[str] = Field(max_length=48)

    return {"AnalystReport": AnalystReport}


def normalize_limits(value):
    """Reflow excess prose-only limits without dropping or editing any text."""
    rows = value.get("limits") if isinstance(value, dict) else None
    if (isinstance(rows, list) and 12 < len(rows) <= 48
            and all(isinstance(row, str) and row.strip() for row in rows)):
        return {**value, "limits": [*rows[:11], "\n".join(rows[11:])]}
    return value


def validate_report(value, allowed_refs):
    """Check structure and source identities, not the truth of model analysis."""
    def require(ok, code):
        if not ok:
            raise ValueError("THESIS_ANALYST_" + code)

    def text(item):
        require(isinstance(item, str) and bool(item.strip()), "TEXT_REQUIRED")

    def refs(items):
        require(isinstance(items, list) and len(items) <= 48
                and all(isinstance(item, str) and item in allowed_refs for item in items), "SOURCE_INVALID")

    require(isinstance(value, dict) and set(value) == {
        "analysis", "observations", "coverage", "limits", "evidence_refs"}, "SHAPE_INVALID")
    text(value["analysis"])
    require(value["coverage"] in ("available", "partial", "unavailable"), "COVERAGE_INVALID")
    require(isinstance(value["limits"], list) and len(value["limits"]) <= 12, "LIMITS_INVALID")
    for limit in value["limits"]:
        text(limit)
    refs(value["evidence_refs"])
    require(isinstance(value["observations"], list) and len(value["observations"]) <= 12, "OBSERVATIONS_INVALID")
    for row in value["observations"]:
        require(isinstance(row, dict) and set(row) == {"statement", "evidence_refs"}, "OBSERVATION_INVALID")
        text(row["statement"])
        refs(row["evidence_refs"])
        require(bool(row["evidence_refs"]), "OBSERVATION_SOURCE_REQUIRED")
    if value["coverage"] == "unavailable":
        require(not value["observations"], "UNAVAILABLE_HAS_OBSERVATIONS")
    else:
        require(bool(value["evidence_refs"]), "SOURCE_REQUIRED")
    return value


def instruction(node):
    roles = {
        "Fundamentals Analyst": "检查经营、盈利、现金、资本配置和资料口径；区分合并与归母、税前与税后、扣非与经营利润，以及披露事实和预测假设。",
        "Market Analyst": "检查截止日前价格、成交、波动和已提供的Quant材料；量价共变不证明消息归因。Quant是其声明期限内的横截面排序，不是上涨概率、年度收益或公司内在价值；不得更换日期或把零分位说成零概率。",
        "News Analyst": "分析所给新闻与公告的事件、时点、潜在经营传导及反证；区分原始公告、媒体报道、评论与传闻。同源转载不是多个独立证据，发布日期不等于事件发生日。",
        "Sentiment Analyst": "分析所给社媒原帖或评论样本的观点、分歧和取样限制。帖子、情绪和传闻不是公司事实；少量样本不能代表市场共识或总体情绪。新闻叙述不能冒充社媒原帖，没有真实社媒材料就明确unavailable，不编造帖子、热度或多空比例。",
    }
    if node not in roles:
        raise ValueError("THESIS_ANALYST_NODE_INVALID")
    return ("你是本次研究的" + node + "。" + roles[node]
        + "仅使用给定原始资料，按研究截止日区分已知事实、解释和未知；不调用外部工具补充当前信息。"
          "给出有内容的analysis、带原来源ID的observations和具体limits；不能只回复已载入资料。"
          "不得输出投资评级、目标价或仓位指令。模型分析不是新来源，引用必须指向给定research来源。"
          "coverage表示资料覆盖而非正确性认证；available或partial必须列来源，unavailable时observations为空并说明缺口。"
          "保留分歧和合理假设，不把样本未含某项信息说成公司未披露，也不以无消息证明没有风险。")


def verbatim(value):
    """Fence arbitrary prose without modifying its characters or Markdown."""
    longest = max((len(m[0]) for m in re.finditer(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "text\n" + value + "\n" + fence


def render_analyst(value, title):
    title = re.sub(r"[\r\n]+", " ", title)
    coverage = {"available": "已有资料", "partial": "资料不完整", "unavailable": "没有可用的该类资料"}[value["coverage"]]
    parts = ["### " + title, "", "资料覆盖：" + coverage + "；不是事实认证。", "",
             verbatim(value["analysis"]), "", "#### 观察与来源", ""]
    if not value["observations"]:
        parts += ["本角色未提出可由所给资料支持的观察。", ""]
    for i, row in enumerate(value["observations"], 1):
        parts += [f"观察 {i}：", "", verbatim(row["statement"]), "",
                  "来源：" + "、".join(row["evidence_refs"]), ""]
    parts += ["#### 资料与推断限制", ""]
    parts += [verbatim(limit) for limit in value["limits"]] or ["模型未另列限制；不表示资料充分或结论已核。"]
    parts += ["", "整体来源：" + ("、".join(value["evidence_refs"]) or "未提供"), ""]
    return "\n".join(parts)
