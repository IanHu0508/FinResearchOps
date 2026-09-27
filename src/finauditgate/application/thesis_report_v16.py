"""Complete research delivery with replayable evidence and stage recovery."""

from .research_delivery import report_context
from .research_narrative import change_view
from .thesis_report_v13 import render_with_context
from . import thesis_report_v11 as previous


def render(record):
    context = report_context(record["final_report"], record["effective_forward_draft"],
        record["effective_forward_calculations"], record["source_bundle"], record["request"],
        changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"])
    check = context.evidence_check()
    if record["evidence_check"] != check or record["status"] != check["status"]:
        raise ValueError("THESIS_EVIDENCE_CHECK_CHANGED")
    return render_with_context(record, context, evidence_check=check, mechanical_changes=True)


def render_process(record):
    raw = previous.render_process(record)
    response_id = record["exchanges"][-1]["response_id"]
    outputs = [o for c in record["model_calls"] for o in c.get("output", []) if o.get("id") == response_id]
    from .thesis_report_v13 import _corrections, _belief_updates
    context = report_context(record["final_report"], record["effective_forward_draft"],
        record["effective_forward_calculations"], record["source_bundle"], record["request"],
        changes=change_view(record["applied_changes"]), beliefs=record["forward_revision"]["belief_updates"])
    parts = ["", "## 模型修改理由与信念解释（待核）", "",
        "以下保留模型解释，可能与真实前后参数矛盾；不构成已验证的纠错或金融事实。"
        "客观变化以主报告的程序前后对账为准。", "",
        *_corrections(record, context, selected=True), *_belief_updates(record, context),
        "## 终稿原始输出与证据检查", "",
        "原始模型响应完整保留。正文的来源/指标清单由程序从选择器派生；显式附加指标也保留展示。"
        "未核来源不会生成虚假原文或位置，绑定成功也不代表语义支持。", "",
        *previous._json(outputs), *previous._json(record["evidence_check"]),
        "## 节点恢复记录", "",
        "恢复只处理明确的技术失败或缺失理由；补全不得改动已有判断和数值。"
        "原始失败响应及费用仍保留，推理强度变化不表示质量等价。", "",
        *previous._json(record["recovery"])]
    return raw + "\n".join(parts).encode("utf-8")
