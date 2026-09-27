"""Four real analyst outputs alongside the unchanged financial report body."""

import json
import re

from finauditgate.adapters.thesis_analysts import ANALYSTS, render_analyst, verbatim
from . import thesis_report_v16
from .research_delivery import evidence_catalog
from .forward_report import _table


_TITLES = {"Fundamentals Analyst": "基本面分析", "Market Analyst": "市场与量价分析",
           "News Analyst": "新闻与事件分析", "Sentiment Analyst": "社媒与情绪分析"}


def _analysts(record):
    parts = ["", "## 四类分析原始输出", "",
             "以下为四个分析角色的完整模型输出，保留原文、引用和缺口。来源存在不等于论述获证实；"
             "新闻和社媒观点不自动成为公司事实。缺该类样本时必须明确，不伪造覆盖。", ""]
    for node in ANALYSTS:
        parts += [render_analyst(record["analyst_reports"][node], _TITLES[node])]
    return "\n".join(parts)


def _quant_citations(value, quant_blocks):
    if isinstance(value, dict):
        if "QUANT" in value.get("evidence_refs", []):
            return True
        return any(_quant_citations(child, quant_blocks) for child in value.values())
    if isinstance(value, list):
        return any(_quant_citations(child, quant_blocks) for child in value)
    if isinstance(value, str):
        return any(qid in quant_blocks for qid in re.findall(r"\{\{source:([^:{}]+)\}\}", value))
    return False


def _quant(record):
    sources = [s for s in record["source_bundle"]["sources"]
               if s["id"] == "QUANT" and s.get("use", "research") == "research"]
    parts = ["", "## Quant输入与各阶段引用记录", "",
             "以下保留输入中的原始Quant说明。排序期限、知识截止和模型限制以该说明为准；"
             "排序不是上涨概率或年度回报，引用记录不能证明Quant改善了决策。", ""]
    for source in sources:
        parts += [verbatim(source["content"]), ""]
    if not sources:
        parts += ["本任务未提供research用途的QUANT来源，不补造信号。", ""]
    quant_blocks = {row["id"] for row in evidence_catalog(record["source_bundle"]) if row["source_id"] == "QUANT"}
    rows = []
    for i, exchange in enumerate(record["exchanges"], 1):
        payload = json.loads(exchange["messages"][1]["content"])
        supplied = any(s.get("id") == "QUANT" for s in payload.get("source_bundle", {}).get("sources", []))
        cited = _quant_citations(exchange["parsed"], quant_blocks)
        rows.append((str(i), exchange["node"], exchange["kind"], "已提供" if supplied else "未提供",
                     "有显式引用" if cited else "未见显式引用"))
    parts += _table(("阶段", "角色", "输出类型", "原Quant输入", "返回中的引用"), rows)
    parts += ["", "程序只记录请求中的来源和返回中的引用；未见显式引用不等于模型未受影响，"
              "有引用不等于采纳、因果贡献或预测有效。", ""]
    return "\n".join(parts)


def _user_context(record):
    request = record["request"]
    parts = ["", "## 用户输入与判断分流", "", "原研究问题：", "", verbatim(request["question"]), ""]
    for key, label in (("hypotheses", "待检验假设"), ("research_constraints", "研究约束")):
        parts += ["### " + label, ""]
        items = request.get(key, [])
        parts += [verbatim(item) for item in items] or ["未单独提供。"]
        parts.append("")
    parts += ["明确分流的user_view字段作为用户期望单独保存，不进入模型研究请求。"
              "原问题、待检验假设及研究约束保留原文；这不是对所有自然语言方向性暗示的自动清洗。", ""]
    if request.get("user_view"):
        parts += ["单独保存的用户期望（不作为证据或评级指令）：", "", verbatim(request["user_view"]), ""]
    parts += ["初判、反证、参数变化及观点更新的原始记录见[过程记录附录](process-record.md)。"
              "观点变化只说明模型前后输出变化，不证明研究质量提高。", ""]
    return "\n".join(parts)


def supplement(record):
    return (_analysts(record) + _quant(record) + _user_context(record) + _belief_records(record)
            + _manager_binding_notes(record)).encode("utf-8")


def _manager_binding_notes(record):
    expected = [row["id"] for row in record["updated_claims"]]
    assessments = record["research_evaluation"]["assessments"]
    actual = [row["claim_id"] for row in assessments]
    missing = [value for value in expected if value not in actual]
    unmatched = list(dict.fromkeys(value for value in actual if value not in expected))
    duplicate = list(dict.fromkeys(value for value in actual if actual.count(value) > 1))
    if not (missing or unmatched or duplicate):
        return ""
    parts = ["", "## 研究经理中间稿的编号提示", "",
        "中间经理稿未实现与全部原论点逐项一一绑定。以下保留全部原评估，不猜测编号映射；"
        "这不表示缺项已评价，也不把中间经理提案视为最终结论。最终经理仍单独回应全部原论点与独立观点。", "",
        "未逐项绑定的原论点：" + ("、".join(missing) or "无"), "",
        "不能绑定到原论点的经理编号：" + ("、".join(unmatched) or "无"), "",
        "重复出现的经理编号：" + ("、".join(duplicate) or "无"), ""]
    for row in assessments:
        parts += ["原编号：" + verbatim(row["claim_id"]), "",
                  "原处置：" + row["disposition"], "", verbatim(row["reason"]), ""]
    return "\n".join(parts)


def _belief_records(record):
    beliefs = {b["belief_id"]: b for b in record["independent_assessment"]["beliefs"]}
    labels = {"maintain": "维持", "revise": "修改", "withdraw": "撤回", "unresolved": "未解决"}
    parts = ["", "## 独立观点与反证回应", "",
             "以下状态是模型自报。维持标记附带重述时保留两种文字，不认定它们语义完全相同；"
             "有更新记录也不证明模型固有偏好已消除。", ""]
    for item in record["forward_revision"]["belief_updates"]:
        parts += ["### " + item["belief_id"] + " · 模型标记：" + labels[item["status"]], "",
                  "原独立观点：", "", verbatim(beliefs[item["belief_id"]]["statement"]), ""]
        if item["new_statement"] is not None:
            title = "模型附带重述（未认定与原句同义）：" if item["status"] == "maintain" else "模型给出的新表述："
            parts += [title, "", verbatim(item["new_statement"]), ""]
        parts += ["回应理由：", "", verbatim(item["reason"]), "", "来源：" + "、".join(item["evidence_refs"]), ""]
    return "\n".join(parts)


def render(record):
    return thesis_report_v16.render(record) + supplement(record)


def render_process(record):
    return thesis_report_v16.render_process(record) + supplement(record)
