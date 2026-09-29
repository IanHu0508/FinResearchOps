"""One review and, when needed, one correction after the main Case is saved."""

from copy import deepcopy
import re
from typing import get_args

from finauditgate.application import thesis_quality as quality
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
from .thesis_responses import response_candidate


def schemas(base, *, concluded=False):
    """Aftercare stage types; from protocol 23 (concluded) the revision keeps one of the five ratings."""
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field
    change_type = get_args(base["ForwardRevision"].model_fields["changes"].annotation)[0]

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    class Finding(Strict):
        id: str = Field(pattern=r"^R[1-9][0-9]?$")
        path: str
        original_text: str = Field(min_length=1)
        issue: str = Field(min_length=1)
        impact: Literal["wording", "financial", "conclusion", "optional"]
        evidence_refs: list[str] = Field(max_length=48)
        next_check: str = Field(min_length=1)

    class FinancialChange(Strict):
        finding_ids: list[str] = Field(min_length=1, max_length=12)
        change: change_type

    class QualityReview(Strict):
        findings: list[Finding] = Field(max_length=12)
        financial_changes: list[FinancialChange] = Field(max_length=12)
        coverage_and_limits: str = Field(min_length=1)

    class Edit(Strict):
        finding_ids: list[str] = Field(min_length=1, max_length=12)
        path: str
        expected_text: str = Field(min_length=1)
        replacement_text: str = Field(min_length=1)

    class Resolution(Strict):
        finding_id: str
        outcome: Literal["corrected", "not_supported", "unresolved"]
        reason: str = Field(min_length=1)
        evidence_refs: list[str] = Field(max_length=48)

    class Rating(Strict):
        value: (Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"] if concluded
                else Literal["Buy", "Hold", "Sell", "REVIEW"])
        reason: str = Field(min_length=1)

    class ScenarioDecision(Strict):
        scenario_id: Literal["F1", "F2", "F3"]
        disposition: Literal["use", "conditional", "reject"]

    class QualityRevision(Strict):
        edits: list[Edit] = Field(max_length=32)
        resolutions: list[Resolution] = Field(max_length=12)
        rating: Rating
        scenario_decisions: list[ScenarioDecision] = Field(max_length=3)

    return {"QualityReview": QualityReview, "QualityRevision": QualityRevision}


REVIEW_INSTRUCTION = (
    "你对已保存研报做一次有界实质复核，不重做投资决策，不因评级是REVIEW就忽略正文错误。"
    "先按顺序检查summary、financial_analysis的四部分、strongest_counterevidence，再检查scenario_assessments和limitations。"
    "change_explanations与belief_explanations是原过程附录；不要把对历史附录的检查代替当前主稿检查。"
    "只报告有依据的具体问题，不以检查项数量证明质量；但不能只挑最明显的一两项而跳过其他正文。"
    "原始资料是证据；分析假设、模型reason和数学复算都不是披露认证。editable_text提供可精确定位的正文。"
    "对每项给R编号、path、逐字original_text、问题、影响、来源ID和核查方向；original_text必须在该字段唯一存在，选择需要修订的最小完整句段。"
    "同一问题若在摘要及正文重复，分别列出对应位置，不漏掉受影响的主结论；不要把多处位置塞入一个path。"
    "核对：假设是否被写成事实；主体/期间/指标是否混用；引文是否真的支持该句；因果或总体概括是否过强；"
    "样本未载明是否被说成公司未披露；条件打平PE是否被说成金额或公允价值；短期排序是否被当作概率/年度收益。"
    "逐项区分：情景少数股东归属额不能证明实际占比；非经常性损益中少数股东影响不是总少数股东净利；"
    "投资现金流不是现金资本购买；融资余额增减不是完整融资置换桥；部分在建不等于所有项目均未投产。"
    "已给出的盈利情景不是不存在前瞻盈利；合理估值需要当前可得的校准依据，不要求提前取得未来估值日实际报价。"
    "PDF换行、缺非关键页码不是事实错误；合理机制解释与明确预测假设应保留，不因未来未发生而否定。"
    "wording用于正文事实、范围、因果、引用或定义修正；financial用于明确错误的参数；conclusion用于影响评级依据；optional用于非关键提示。"
    "financial_changes只针对financial疑点且已有依据的必要改动，从当前effective参数复制expected_before，保留完整reason/basis_type/来源；"
    "无可用新值可明确null，禁止猜零。只需改文字时不得重抽参数或改评级。"
    "避免罗列一切可能缺数据。没有问题时findings和financial_changes为空。"
)
REVISION_INSTRUCTION = (
    "根据复核疑点完成唯一一次定向校订。复核意见也可能错，回到原始资料判断，不自动采纳。"
    "每项resolutions给corrected/not_supported/unresolved、实质理由及来源；反驳疑点须有来源。"
    "若疑点定位effective_forward_draft中的已有假设reason，可以只修理由：程序保留value、nature、basis_type并调用原复算，"
    "不能把其他输入字段当文字修改。下标[1]与.1均可。这些新理由同样使用财务数字引用，不重抄数字。"
    "related_text列出该理由对应的同一情景采用理由（共同参数则对应估值正文）。修正输入理由时必须同步重审这个相关正文，"
    "有问题则改写；若相关正文已经正确，提交完整expected_text与相同replacement_text确认保留，并在resolution.reason说明理由。"
    "不能只在附录或limitations免责。related_text编辑须复制该字段完整原文为expected_text；其他无关字段不允许改。"
    "若同一related_text字段还被其他R项指出问题，将它们合并为一次该字段完整修改并关联全部R编号，不提交重叠编辑。"
    "只用edits修改已定位问题，path和expected_text必须分别完全等于所关联finding的path和original_text，"
    "replacement_text完整保留应有的研究含量和引文。每个edit关联finding_ids；不得删整节、删反证、用空泛无法判断替代分析。"
    "程序拒绝单字段去除引用标记后的正文缩短超过一半，短字段还需保留基本说明；这只是防止丢内容，不是质量评分。"
    "原句与字段属于校订前版本，多个edit不重叠；同一疑点在摘要/正文重复出现时一并修。不得只把错误搬到附录。"
    "corrected必须实际修改所指原句或金融参数；unresolved须在所指正文撤回未经证实的断言并说明影响，不能只加全局免责声明。"
    "financial_changes已由程序对effective输入应用并复算。只能引用这里的新metrics，不能自己再改数字；"
    "如已提参数修正本身不成立，明确unresolved，不能批准该修正。参数/关键依据变化时必须重审评级、情景采纳和受影响正文，"
    "否则评级与情景采纳保持原样。仅wording/optional问题不能趁机改评级。scenario_decisions只列须改变的情景。"
    "报告允许有价值、有理由的假设，不要求未来已验证；依照assumption_context明确其性质。"
    "不要把review.issue/next_check/reason中的金额、百分比或日期简称直接复制到replacement_text。"
    "例如来源载明某期比率下降，可以写‘该期比率下降{{source:E0001}}’，不要再抄写原文百分比；完整原数会由程序显示。"
    "所有财务数量仍用{{metric:F1:eps_per_traded_unit}}等原协议；历史数字用{{source:E0001}}引用，"
    "保留正确的经营、盈利、现金、估值和反证，不重写无关内容。不输出新完整研报，只给定向改动与处理理由。"
)


def _invoke(session, types, kind, instruction, payload, config, exchanges):
    from .thesis_protocol import messages
    if kind == "QualityRevision":
        from finauditgate.application.research_delivery import final_instruction
        rules = final_instruction(session.protocol_version)
        instruction = rules + "\n以上财务引用规则适用于replacement_text；本次只返回下面QualityRevision结构，不生成FinalResearchReport。\n" + instruction
    prompt = messages("Data Review Agent", instruction, payload)
    prompt[0]["content"] += "\n只返回一个完整JSON对象；不使用Markdown或工具调用。所有required字段必须存在：\n" + canonical_json_bytes(types[kind].model_json_schema()).decode()
    from .thesis_recovery import extra_call_limit, retained_length_outputs, successful_call
    previous = [] if not session.completed else [c for c in session.completed._prior_calls
        if c.get("thesis_stage", {}).get("kind") == kind and c.get("node") == "Data Review Agent"]
    quality._require(len(previous) <= 2, "PRIOR_CALL_LIMIT")
    outputs, response_id = None, None
    for attempt in (1, 2):
        effort = "max" if attempt == 1 else "high"
        call = None
        if len(previous) >= attempt:
            call = previous[attempt - 1]
            actual = [{"role": "user" if m["type"] == "human" else m["type"], "content": m["content"]} for m in call["messages"][0]]
            quality._require(actual == prompt and call.get("thesis_stage") == {
                "kind": kind, "attempt": attempt, "reasoning_effort": effort}, "RESUME_INPUT_CHANGED")
            session.completed._carry(call, session.capture)
        else:
            model = session.model
            if hasattr(model, "reasoning_effort"):
                model = model.model_copy(update={"reasoning_effort": effort})
            before = len(session.capture.model_calls)
            try:
                response = model.with_structured_output(types[kind], method="json_mode", include_raw=True).invoke(prompt,
                    config={**config, "metadata": {**config.get("metadata", {}), "thesis_stage": {
                        "kind": kind, "attempt": attempt, "reasoning_effort": effort}}})
                quality._require(isinstance(response, dict) and response.get("raw") is not None, "RAW_RESPONSE_REQUIRED")
            except Exception:
                if len(session.capture.model_calls) == before:
                    raise
            call = session.capture.model_calls[-1]
        retained = retained_length_outputs(call)
        if retained is not None:
            # A complete answer is parsed and retained, never redrawn.
            types[kind].model_validate(response_candidate(retained, kind, protocol_version=16))
            session.capture.retain_complete_length(call["run_id"], retained)
            call = next(c for c in session.capture.model_calls if c["run_id"] == call["run_id"])
            if session.budget is not None:
                session.budget.release_confirmed_truncation(confirmed_length=True)
        if successful_call(call):
            outputs = call.get("output", [])
            response_id = outputs[0]["id"] if outputs else None
            break
        if (attempt != 1 or not quality.no_answer_length(call)
                or session.quality_retries + len(session.recovery.value["attempts"])
                    >= extra_call_limit(session.recovery.value["policy"])):
            raise ValueError("THESIS_QUALITY_PRIOR_ATTEMPT_FAILED")
        session.quality_retries += 1
        if session.budget is not None:
            session.budget.release_confirmed_truncation(confirmed_length=True)
    quality._require(outputs is not None, "RAW_RESPONSE_REQUIRED")
    value = types[kind].model_validate(response_candidate(outputs, kind, protocol_version=16)).model_dump(mode="json")
    exchanges.append({"node": "Data Review Agent", "kind": kind, "messages": prompt,
                      "response_id": response_id, "parsed": deepcopy(value)})
    return value


def run_quality(session, record, config):
    start = len(session.capture.model_calls)
    exchanges = []
    result = {"schema_version": quality.SCHEMA, "main_sha256": sha256_hex(canonical_json_bytes(record)),
        "status": "PARTIAL", "reason": "QUALITY_NOT_FINISHED", "findings": [],
        "coverage_and_limits": "模型复核不构成金融认证。", "assessment": None, "revision": None,
        "effective": None, "exchanges": exchanges, "model_calls": []}
    # Aftercare is outside the completed thirteen-stage recovery chain.
    session.last_stage = None
    session.quality_retries = 0
    try:
        types = schemas(session.types, concluded=session.protocol_version >= 23)
        assessment = _invoke(session, types, "QualityReview", REVIEW_INSTRUCTION, quality.review_payload(record), config, exchanges)
        result.update(assessment=assessment, findings=assessment["findings"], coverage_and_limits=assessment["coverage_and_limits"])
        payload = quality.revision_payload(record, assessment)
        revision = None
        if any(f["impact"] != "optional" for f in assessment["findings"]):
            revision = _invoke(session, types, "QualityRevision", REVISION_INSTRUCTION, payload, config, exchanges)
            result["revision"] = revision
        effective = quality.apply_revision(record, assessment, revision)
        result.update(effective=effective, status=effective["status"], reason="MODEL_REVIEW_NOT_CERTIFICATION")
    except Exception as exc:
        result.update(status="PARTIAL", reason="QUALITY_REVIEW_OR_REVISION_FAILED", error_type=type(exc).__name__)
        code = str(exc)
        if re.fullmatch(r"[A-Z0-9_]{1,120}", code):
            result["error_code"] = code
    finally:
        result["model_calls"] = deepcopy(session.capture.model_calls[start:])
    return result
