"""A deterministic, Agent-facing research note projected from one v4 signal.

The note shows only the cross-sectional model rank with its direction, input
observations explicitly without attribution, time-gated validation evidence and
limitations. Usage rules live in the note text itself; the Agent prompt is not
changed. The result is one finresearchops.thesis-sources/v2 source row.
"""

from datetime import datetime
from collections import Counter
import hashlib
import math

from quant.contracts import CHINA, MARKET_NAMES, RELATIVE_NAMES, SCALAR_NAMES, finite, require
from quant.evaluation.evidence import ValidationEvidence
from .signals import validate_signal

NOTE_VERSION = "quant-research-note/v1"
MODES = {"HISTORICAL_SIMULATION": "历史模拟（只列评分时点已能计算的历史结果）",
         "RETROSPECTIVE": "事后回顾（可含评分时点之后才揭晓的历史结果，并逐项标注）"}
INPUTS = {"stock-only": "个股日频价量特征", "stock+context": "个股日频价量特征加市场状态"}
MODEL_FAMILIES = {"xgboost-": "XGBoost 树模型", "analogue-": "历史相似状态近邻模型", "fusion-": "树模型与近邻模型融合"}
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


def analogue_research_note(signals, row, evidence, details, *, mode, source_id="QUANT"):
    """Bind global/local/fused estimates and inspectable historical samples.

    Evidence is time-gated by the existing note contract. The three estimates
    share inputs and outcomes; this is one quantitative evidence source.
    Neighbour intervals are presented intact, with no midpoint distribution.
    """
    require(set(signals) == set(evidence) == {"xgb", "nn", "fusion"}, "NOTE_V2_MODELS_REQUIRED")
    require(set(details) == {"alpha", "k", "fusion_weight", "fallback", "date_count", "mean_distance", "neighbors"},
            "NOTE_V2_DETAIL_SHAPE")
    require(details["alpha"] in (0., .25, .5) and details["k"] in (32, 64)
            and details["fusion_weight"] in (0., .25, .5, 1.), "NOTE_V2_CONFIG_INVALID")
    require(type(details["fallback"]) is int and details["fallback"] in (0, 1, 2, 3)
            and type(details["date_count"]) is int and details["date_count"] >= 0
            and finite(details["mean_distance"]) and details["mean_distance"] >= 0,
            "NOTE_V2_RETRIEVAL_SUMMARY_INVALID")
    for signal in signals.values():
        validate_signal(signal)
    base = signals["xgb"]
    for signal in signals.values():
        require(all(signal[key] == base[key] for key in ("symbol", "as_of", "data_cutoff", "universe_size",
                                                        "inference_input_id", "target_id")), "NOTE_V2_MODEL_POOL_MISMATCH")
    notes = {name: research_note(signal, row, evidence[name], mode=mode)
             for name, signal in signals.items()}
    neighbors = details["neighbors"]
    require(isinstance(neighbors, list) and (not neighbors if details["fallback"] else len(neighbors) == details["k"]),
            "NOTE_V2_NEIGHBOR_COVERAGE")
    dates, identities = [], set()
    for neighbor in neighbors:
        require(set(neighbor) == {"symbol", "as_of", "target_interval", "weight"}, "NOTE_V2_NEIGHBOR_SHAPE")
        lower, upper = neighbor["target_interval"]
        require(finite(lower) and finite(upper) and 0 <= lower <= upper <= 1
                and finite(neighbor["weight"]) and neighbor["weight"] > 0
                and datetime.fromisoformat(neighbor["as_of"]) < datetime.fromisoformat(signals["nn"]["training_cutoff"]),
                "NOTE_V2_NEIGHBOR_TIME_OR_TARGET")
        historical_date = datetime.fromisoformat(neighbor["as_of"]).astimezone(CHINA).date()
        identity = (historical_date, neighbor["symbol"])
        require(identity not in identities, "NOTE_V2_NEIGHBOR_UNIQUENESS")
        identities.add(identity)
        dates.append(historical_date)
    counts = Counter(dates)
    require(all(count <= details["k"] // 8 for count in counts.values()), "NOTE_V2_NEIGHBOR_DATE_CAP")
    require(details["date_count"] == len(counts), "NOTE_V2_NEIGHBOR_DATE_COUNT")
    require(all(math.isclose(neighbor["weight"], 1 / (len(counts) * counts[day]), rel_tol=0, abs_tol=1e-12)
                for neighbor, day in zip(neighbors, dates)), "NOTE_V2_NEIGHBOR_WEIGHTS")
    expected_fusion = base["predicted_target_percentile"] if details["fallback"] else (
        (1 - details["fusion_weight"]) * base["predicted_target_percentile"]
        + details["fusion_weight"] * signals["nn"]["predicted_target_percentile"])
    require(math.isclose(signals["fusion"]["predicted_target_percentile"], expected_fusion,
                         rel_tol=0, abs_tol=1e-12), "NOTE_V2_FUSION_SCORE_MISMATCH")
    lines = ["QUANT_RESEARCH_NOTE quant-research-note/v2", "",
             "本说明包含三个分别计算的估计器。它们共享股票池、价量信息和历史标签，不能计作三份独立市场证据。",
             "全局树模型学习总体条件关系；局部近邻检索历史样本；融合检验两种估计是否互补。", "",
             f"NN固定配置：k={details['k']}，市场状态距离权重alpha={details['alpha']}；融合权重w={details['fusion_weight']}。",
             "普通NN也含相对收益；alpha检验的是额外四项市场状态距离。",
             "融合对预测标量加权后重新全池排名，不直接平均两个rank，不产生一份新的原始市场来源。",
             "| 估计器 | 目标排名预测标量（非上涨概率） | 当日全池模型rank |",
             "| --- | --- | --- |",
             *[f"| {name} | {signals[name]['predicted_target_percentile']:.6f} | {signals[name]['cross_sectional_model_rank']:.6f} |"
               for name in ("xgb", "nn", "fusion")], "",
             "检索结果内日期等权；单日期至多k/8个样本；点预测最小化区间损失，多解向训练中心投影。",
             "不同历史日期仍可能相关；日期数、距离和样本数不是胜率或校准可信度。", "",
             f"本次有效邻居日期数：{details['date_count']}；平均完整平方距离：{details['mean_distance']:.6f}；回退标记：{details['fallback']}。",
             "回退时NN输出训练期常数、融合输出XGB，不把回退常数解释为新的局部预测依据。", "",
             "历史邻居（完整保留目标排名区间，不用中点替代标签）：",
             "| 证券 | 历史评分时点 | 已成熟target区间 | 日期等权样本权重 |",
             "| --- | --- | --- | --- |",
             *[f"| {n['symbol']} | {n['as_of']} | [{n['target_interval'][0]:.6f}, {n['target_interval'][1]:.6f}] | {n['weight']:.6f} |" for n in neighbors], ""]
    for name, title in (("xgb", "全局模型"), ("nn", "历史局部模型"), ("fusion", "融合估计")):
        lines += ["## " + title, "", notes[name]["content"], ""]
    lines += ["本轮模型选择与比较使用已见历史，是冻结新规则后的回顾性研究；没有新的blind test或生产验证。",
              "Quant的二十个交易日目标与公司年度经营/财务假设不同；不能直接据此设定收入、利润、税率或公允PE。"]
    content = "\n".join(lines) + "\n"
    return {"id": source_id, "origin": "FinResearchOps quant-research-note/v2（确定性生成，三个估计器共享市场来源）",
            "availability_note": notes["xgb"]["availability_note"], "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(), "use": "research"}
