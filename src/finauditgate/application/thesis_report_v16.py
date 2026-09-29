"""Complete research delivery with replayable evidence and stage recovery."""

from finauditgate.adapters.thesis_degrade import case_status
from .research_delivery import contract_for, report_context
from .research_narrative import change_view
from .thesis_report_v13 import render_with_context
from . import thesis_report_v11 as previous


RECOVERY_NOTES = {
    "thesis-stage-recovery/v2": "恢复只处理明确的技术失败或缺失理由；补全不得改动已有判断和数值。"
        "原始失败响应及费用仍保留，推理强度变化不表示质量等价。",
    "thesis-stage-recovery/v3": "恢复只处理明确的技术失败、无法解析的回答、缺失理由或越界的依据类型标签；"
        "无法解析时用原提示重问一次，补全与标签修正不得改动其他判断和数值。原始失败响应及费用仍保留，"
        "推理强度变化不表示质量等价，重问所得回答也不因此更可信。",
    "thesis-stage-recovery/v4": "恢复只处理明确的技术失败、无法解析或不符合结构约定的回答、缺失理由或越界的依据类型标签；"
        "无法解析或不符合结构约定时用原提示重问一次，补全与标签修正不得改动其他判断和数值。原始失败响应及费用仍保留，"
        "推理强度变化不表示质量等价，重问所得回答也不因此更可信。",
    "thesis-stage-recovery/v5": "恢复只处理明确的技术失败、无法解析或不符合结构约定的回答、缺失理由或越界的依据类型标签；"
        "无法解析或不符合结构约定时用原提示重问一次，补全与标签修正不得改动其他判断和数值。"
        "终稿若只因少数句子含未绑定数字被拒，只请模型改写这些句子，其余文字、评级和情景采纳不变，改写后整份终稿按同一数字规则重新检查。"
        "四类分析与交易员属于非关键角色：其全部回答经程序证明不可用时，以明示的降级占位继续，Case状态记为PARTIAL。"
        "原始失败响应及费用仍保留，推理强度变化不表示质量等价，重问或改写所得文字也不因此更可信。",
    "thesis-stage-recovery/v6": "在v5规则之上，程序能从已保存回答及其提示证明的两类内容问题也在同一步骤再请求一次："
        "前瞻提案未通过程序在复算前执行的检查（情景编号、行情日、估值日及复算器自身的期间、口径与数值范围规则），"
        "或终稿以前的关键研究步骤引用了资料中不存在的来源编号；四类分析与交易员仍按降级规则处理。"
        "附加说明写在原提示之后并写明被证明的问题；该步骤已用过其他恢复、额外调用已满或仍不通过时停止。"
        "原始失败响应及费用保留，重问所得文字不因此更可信。",
}


def render(record):
    context = report_context(record["final_report"], record["effective_forward_draft"],
        record["effective_forward_calculations"], record["source_bundle"], record["request"],
        changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"],
        contract=contract_for(record))
    check = context.evidence_check()
    if record["evidence_check"] != check or record["status"] != case_status(degraded(record), check["status"]):
        raise ValueError("THESIS_EVIDENCE_CHECK_CHANGED")
    return render_with_context(record, context, evidence_check=check, mechanical_changes=True)


def degraded(record):
    return record.get("recovery", {}).get("degraded", [])


def _v20_records(record):
    """Degraded stages and the final-report sentence repair, each shown with its saved evidence."""
    parts = []
    if degraded(record):
        parts += ["## 降级省略的非关键角色", "",
                  "以下角色的全部回答经程序证明不可用，本次以明示占位继续；这不代表资料中没有相关信息或观点中性。"
                  "原始回答和失败原因保留在上方模型调用记录中。", ""]
        for row in degraded(record):
            parts += [f"- {row['node']}（{row['kind']}）：原因代码 {row['reason']}，涉及调用 {len(row['run_ids'])} 次"]
        parts.append("")
    if record.get("number_repair"):
        parts += ["## 终稿句子级数字修复", "",
                  "终稿的下列句子被数字规则拒收，仅这些句子由模型改写；其余文字、评级和情景采纳未变，"
                  "改写后整份终稿已按同一规则重新检查。改写不代表所述内容获证实。", ""]
        for row in record["number_repair"]["replacements"]:
            parts += ["字段：" + row["field"], "", "原句：", "", *previous._json(row["original"]),
                      "改写：", "", *previous._json(row["replacement"])]
    return parts


def render_process(record):
    raw = previous.render_process(record)
    response_id = record["exchanges"][-1]["response_id"]
    outputs = [o for c in record["model_calls"] for o in c.get("output", []) if o.get("id") == response_id]
    from .thesis_report_v13 import _corrections, _belief_updates
    context = report_context(record["final_report"], record["effective_forward_draft"],
        record["effective_forward_calculations"], record["source_bundle"], record["request"],
        changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"],
        contract=contract_for(record))
    parts = ["", "## 模型修改理由与信念解释（待核）", "",
        "以下保留模型解释，可能与真实前后参数矛盾；不构成已验证的纠错或金融事实。"
        "客观变化以主报告的程序前后对账为准。", "",
        *_corrections(record, context, selected=True), *_belief_updates(record, context),
        "## 终稿原始输出与证据检查", "",
        "原始模型响应完整保留。正文的来源/指标清单由程序从选择器派生；显式附加指标也保留展示。"
        "未核来源不会生成虚假原文或位置，绑定成功也不代表语义支持。", "",
        *previous._json(outputs), *previous._json(record["evidence_check"]),
        "## 节点恢复记录", "", RECOVERY_NOTES[record["recovery"]["policy"]], "",
        *previous._json(record["recovery"]), *_v20_records(record)]
    return raw + "\n".join(parts).encode("utf-8")
