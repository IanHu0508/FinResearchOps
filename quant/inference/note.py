"""A deterministic, Agent-facing research note projected from one v4 signal.

The note shows only the cross-sectional model rank with its direction, input
observations explicitly without attribution, time-gated validation evidence and
limitations. Usage rules live in the note text itself; the Agent prompt is not
changed. The result is one finresearchops.thesis-sources/v2 source row.
"""

from datetime import datetime
import hashlib

from quant.contracts import CHINA, MARKET_NAMES, RELATIVE_NAMES, SCALAR_NAMES, require
from quant.evaluation.evidence import ValidationEvidence
from .signals import validate_signal

NOTE_VERSION = "quant-research-note/v1"
MODES = {"HISTORICAL_SIMULATION": "历史模拟（只列评分时点已能计算的历史结果）",
         "RETROSPECTIVE": "事后回顾（可含评分时点之后才揭晓的历史结果，并逐项标注）"}
INPUTS = {"stock-only": "个股日频价量特征", "stock+context": "个股日频价量特征加市场状态"}
MODEL_FAMILIES = {"xgboost-": "XGBoost 树模型"}
MISSING = "不可得（该项输入缺失）"


def _time(value):
    return value.astimezone(CHINA).strftime("%Y-%m-%d %H:%M")


def _signed(value):
    return MISSING if value is None else f"{value * 100:+.2f}%"


def _plain(value):
    return MISSING if value is None else f"{value * 100:.2f}%"


def _evidence_line(item, as_of):
    late = "（回顾性：以下结果在评分时点之后才揭晓）" if item.knowledge_cutoff > as_of else ""
    if not item.evaluable_days:
        return (f"{late}截至{_time(item.knowledge_cutoff)}尚无已揭晓结果的样本外评价"
                f"（已有{item.forecast_days}个评分日的预测，其20个交易日结果尚未揭晓）。")
    bounds = ("无定义（部分评分日排序相关系数无定义）" if item.rank_ic_bounds is None
              else f"[{item.rank_ic_bounds[0]:.3f}, {item.rank_ic_bounds[1]:.3f}]")
    months = "、".join(item.negative_ic_months) or "无"
    return (f"{late}在{item.period[0]}至{item.period[1]}的{item.evaluable_days}个已揭晓结果的评分日"
            f"（按{_time(item.knowledge_cutoff)}已知结果计算），日均全池 Rank IC 识别区间为{bounds}；"
            f"日均 Rank IC 上界低于0的月份：{months}。")


def research_note(signal, row, evidence, *, mode, source_id="QUANT", extra_limitations=()):
    """Render one security's note; `row` is that security's FeatureRow from the scored panel.

    Exactly one EXACT_MODEL record is required, even when nothing has matured, so
    the note always states the current model's own evidence explicitly. A
    historical simulation refuses any record known only after the scoring time.
    """
    validate_signal(signal)
    require(mode in MODES, "NOTE_MODE_INVALID")
    require(isinstance(source_id, str) and bool(source_id) and source_id.strip() == source_id, "NOTE_SOURCE_ID_INVALID")
    require(type(extra_limitations) is tuple and all(isinstance(x, str) and x.strip() for x in extra_limitations),
            "NOTE_LIMITATIONS_INVALID")
    as_of = datetime.fromisoformat(signal["as_of"])
    require(row.key.symbol == signal["symbol"] and row.key.as_of == as_of
            and row.data_cutoff.isoformat() == signal["data_cutoff"], "NOTE_ROW_SIGNAL_MISMATCH")
    require(type(evidence) is tuple and all(isinstance(item, ValidationEvidence) for item in evidence),
            "NOTE_EVIDENCE_INVALID")
    for item in evidence:
        require((item.scope == "EXACT_MODEL") == (item.model_version == signal["model_version"]),
                "NOTE_EVIDENCE_SCOPE_MISMATCH")
        require(mode == "RETROSPECTIVE" or item.knowledge_cutoff <= as_of, "NOTE_EVIDENCE_AFTER_SCORING_TIME")
    exact = [item for item in evidence if item.scope == "EXACT_MODEL"]
    require(len(exact) == 1, "NOTE_REQUIRES_ONE_EXACT_MODEL_RECORD")
    family = [item for item in evidence if item.scope == "METHOD_FAMILY"]
    scalar = dict(zip(SCALAR_NAMES, row.scalars))
    relative = dict(zip(RELATIVE_NAMES, row.relative_features))
    market = dict(zip(MARKET_NAMES, row.market_context))
    model = next((name for prefix, name in MODEL_FAMILIES.items() if signal["model_version"].startswith(prefix)),
                 "已冻结的排序模型")
    rank = signal["cross_sectional_model_rank"]
    ratio = MISSING if scalar["amount_ratio_20"] is None else f"{scalar['amount_ratio_20']:.2f}倍"
    lines = [
        f"QUANT_RESEARCH_NOTE {NOTE_VERSION}",
        f"证券{signal['symbol']}；评分时点{_time(as_of)}（上海时间）；证据模式：{MODES[mode]}。",
        f"信号格式{signal['schema_version']}；推理输入指纹{signal['inference_input_id']}；模型版本{signal['model_version']}。",
        "",
        "一、模型评分",
        f"模型：{model}，输入为{INPUTS[signal['feature_ablation']]}；训练所用结果信息截止"
        f"{_time(datetime.fromisoformat(signal['training_cutoff']))}。",
        "预测对象：下一交易日开盘至第20个交易日收盘的参考持有收益，在当日完整合格股票池中的相对排名。",
        f"横截面排名分位：{rank:.3f}（当日合格股票池{signal['universe_size']}只；数值越高，模型给出的相对排序越靠前；"
        f"约{rank * 100:.1f}%的其他股票模型分数更低）。",
        "使用规则：该排名只表示相对排序，不是上涨概率、预期收益率、目标价或买卖建议。",
        "",
        "二、评分日可得的市场观察（未做归因）",
        "以下是评分日已可得的输入状态；没有做归因分析，不能用来解释模型为何给出上述排名。",
        f"个股近5个交易日参考收益：{_signed(scalar['return_5'])}",
        f"个股近20个交易日参考收益：{_signed(scalar['return_20'])}",
        f"个股近20个交易日相对合格股票池中位收益（复合）：{_signed(relative['relative_return_20'])}",
        f"个股近20个交易日日收益标准差：{_plain(scalar['volatility_20'])}",
        f"个股当日成交额相对此前20个交易日均值：{ratio}",
        f"合格股票池当日中位收益：{_signed(market['median_return'])}；当日上涨股票占比：{_plain(market['breadth'])}；"
        f"近20个交易日池中位收益的日波动：{_plain(market['market_volatility_20'])}",
        "",
        "三、历史验证证据",
        f"本模型（EXACT_MODEL，{exact[0].label}）：{_evidence_line(exact[0], as_of)}",
        *(f"同方法的其他模型（METHOD_FAMILY，{item.label}，模型版本{item.model_version}）：{_evidence_line(item, as_of)}"
          for item in family),
        "Rank IC 识别区间是少量结果未知时的保守上下界，不是置信区间；它是排序相关系数，不是收益率或概率。",
        "方法层面证据说明同一建模方法在其他时期的表现，不是本模型的检验结果，也不是该证券的胜率。",
        "",
        "四、限制",
        *(f"{item.label}证据期内结果完整的{item.complete_days}个评分日：模型最高20%组的20日平均参考收益相对全池平均"
          f"{item.top_group_minus_pool * 100:+.2f}个百分点，最低20%组相对全池平均{item.bottom_group_minus_pool * 100:+.2f}个百分点。"
          for item in (*exact, *family) if item.complete_days),
        *(() if any(item.complete_days for item in evidence) else ("尚无可用于分组比较的结果完整评分日。",)),
        "分组收益为理论参考收益，未扣交易成本，不是账户收益；结果完整的评分日不是随机样本。",
        "A股个股做空渠道有限，最高组与最低组的收益差不可直接执行；排序能力不代表买入最高分股票能获得超额收益。",
        "Rank IC 衡量全池整体排序相关性，不代表任何单只股票的结果。",
        "输入只有日频价量与市场状态，不含财务、估值、行业、公告或新闻信息。",
        "预测期限为20个交易日，与基本面研究的年度或多年期限不同；两者结论不一致时应并列说明各自期限与依据，不应以一方改写另一方。",
        ("行情为最新供应商版本的历史重建，未认证当时的供应商版本；行情可得与评分时间为研究约定，非当年实测。"
         if signal["data_kind"] == "REAL_DATA" else "本说明基于合成数据，仅用于检查。"),
        *extra_limitations,
    ]
    content = "\n".join(lines) + "\n"
    latest = max(item.knowledge_cutoff for item in evidence)
    return {"id": source_id, "origin": f"FinResearchOps {NOTE_VERSION}（确定性生成，来源信号{signal['schema_version']}）",
            "availability_note": f"评分时点{_time(as_of)}上海时间；行情截止{_time(datetime.fromisoformat(signal['data_cutoff']))}；"
                                 f"{MODES[mode]}；证据知识截止最晚{_time(latest)}。",
            "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest(), "use": "research"}
