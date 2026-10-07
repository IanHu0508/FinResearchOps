"""Dated company evidence from complete state-study forecasts and neighbours."""

import argparse
from datetime import datetime
import hashlib
import math

from quant.artifacts.store import _private_root
from quant.contracts import canonical, require
from quant.labels.ranks import percentiles
from quant.models.analogues import file_digest, fusion_scores
from quant.models.state_metric import normalize, pair_distances
from quant.state_data import FEATURES, GROUPS, timestamp_ns
from quant.state_evaluation import StateEvaluator
from quant.state_meta import saved_pool
from quant.state_study import StateStudy, cutoff, read, write


def packet(study, text, symbol):
    import numpy as np
    year = int(text[:4])
    require(text in study.dates(year), "STATE_PACKET_SCORING_DATE_REQUIRED")
    asof = text + "T21:30:00+08:00"
    boundary = cutoff(f"{year}-01-01")
    symbols = study.data.symbols(text)
    require(symbol in symbols, "STATE_PACKET_SYMBOL_NOT_IN_ORIGINAL_POOL")
    position = symbols.index(symbol)
    values = study.data.rows(text)
    evaluator = StateEvaluator(study)
    original = evaluator.original.data.baseline(text)
    full = np.array([x[2] for x in original["samples"]])
    require([x[0] for x in original["samples"]] == symbols, "STATE_PACKET_BASELINE_IDENTITY")
    selection = read(study.root / "SELECTION.json")
    require(selection["development_years"] == list(range(2018, 2023))
            and selection["check_year_accessed"] is False and selection["final_years_accessed"] is False
            and timestamp_ns(datetime.fromisoformat(asof)) >= timestamp_ns(datetime.fromisoformat("2024-01-01T00:00:00+08:00")),
            "STATE_PACKET_SELECTION_KNOWLEDGE_BOUNDARY")
    memory, view = study.view(boundary)
    risk = evaluator.arrays("risk", text)
    risk_receipt = read(study.root / "predictions/risk" / str(year) / "RECEIPT.json")
    setup = read(study.root / "meta" / f"{year}-01-01" / "risk.json")
    require(setup["context"]["fit_cutoff"] == boundary and risk_receipt["context"]["fit_cutoff"] == boundary
            and all(item is None or item["latest_label_available_ns"] < timestamp_ns(datetime.fromisoformat(boundary))
                    for item in setup["calibrators"].values()), "STATE_PACKET_FUTURE_CALIBRATION")
    names = ("ordinary", "rank-numeric", "rank-mixed", "risk-numeric", "risk-mixed", "G0", "G1", "xgb-rank")
    forecasts = {name: evaluator.arrays(name, text) for name in names}
    baseline_folder = study.inputs / "phase-d-v1" / f"{year}-xgboost_stock_context"
    baseline_receipt = read(baseline_folder / "PREDICTION_RECEIPT.json")
    baseline_model = read(baseline_folder / "model.json")
    require(file_digest(baseline_folder / "model.json") == baseline_receipt["model_sha256"]["model.json"],
            "STATE_PACKET_FULL_XGB_MODEL_CHANGED")
    estimates = {"xgb-full": {"target_score": float(full[position]),
                             "cross_sectional_percentile": percentiles(tuple(float(v) for v in full))[position],
                             "model_version": baseline_model["model_version"]}}
    for name, arrays in forecasts.items():
        receipt = read(study.root / "predictions" / name / str(year) / "RECEIPT.json")
        estimates[name] = {"target_score": float(arrays["scores"][position]),
                          "cross_sectional_percentile": percentiles(tuple(float(v) for v in arrays["scores"]))[position],
                          "model_version": receipt.get("model_version", "state-meta-" + file_digest(
                              study.root / "meta" / f"{year}-01-01" / "gates.json")[:24]),
                          "fallback": int(arrays["fallback"][position]) if "fallback" in arrays else None}
    for name in ("fusion-0", "fusion-0.25", "fusion-0.5", "fusion-1"):
        base = forecasts[selection["selected_nn"]]
        scores = fusion_scores(full, base["scores"], float(name.removeprefix("fusion-")), base["fallback"])
        estimates[name] = {"target_score": float(scores[position]),
                          "cross_sectional_percentile": percentiles(tuple(float(v) for v in scores))[position],
                          "nn_component": selection["selected_nn"]}
    configurations = {}
    for name in names[:5]:
        artifact = read(study.root / "models" / f"{year}-01-01" / (name + ".json"))
        arrays = forecasts[name]
        one = {key: value[position:position + 1] for key, value in arrays.items()}
        targets, weights, identifiers = saved_pool(study.data, memory, artifact, values[position:position + 1], one)
        ids = identifiers[0]
        rows = []
        if (ids >= 0).all():
            q, qm = normalize(values[position:position + 1], artifact["transform"])
            c, cm = normalize(study.data.values[ids], artifact["transform"])
            distances = pair_distances(q, c[None], qm, cm[None], np.array(artifact["weights"]), artifact["eta"])[0]
            for absolute, target, weight, distance in zip(ids, targets[0], weights[0], distances):
                day = study.data.manifest["days"][int(study.data.day_codes[absolute])]
                historical = study.data.symbols(day["date"])[int(absolute) - day["start"]]
                require(int(study.data.available[absolute]) < timestamp_ns(datetime.fromisoformat(boundary)),
                        "STATE_PACKET_FUTURE_NEIGHBOUR")
                rows.append({"row_id": int(absolute), "date": day["date"], "symbol": historical,
                             "weight": float(weight), "squared_distance": float(distance),
                             "rank_interval": [float(x) for x in target],
                             "reference_return": float(study.data.returns[absolute]),
                             "outcome_available_ns": int(study.data.available[absolute])})
        group_weights = [sum(artifact["weights"][i] for i in group) for group in GROUPS]
        configurations[name] = {"neighbors": rows, "eta": artifact["eta"], "tau": artifact["tau"],
            "feature_weights": artifact["weights"], "group_weights": group_weights,
            "within_group_weights": [[artifact["weights"][i] / group_weights[g] for i in group] for g, group in enumerate(GROUPS)],
            "fallback": int(arrays["fallback"][position]), "fit_cutoff": artifact["fit_cutoff"],
            "date_count": int(arrays["date_count"][position]), "effective_dates": float(arrays["effective_dates"][position])}
    gate_receipt = read(study.root / "predictions/G0" / str(year) / "RECEIPT.json")
    mixtures = {}
    for name in ("G0", "G1"):
        pi = forecasts[name]["pi"][position]
        merged = {}
        for coefficient, configuration in zip(pi, gate_receipt["context"]["views"]):
            for neighbor in configurations[configuration]["neighbors"]:
                key = neighbor["row_id"]
                if key not in merged:
                    merged[key] = {**neighbor, "weight": 0.}
                merged[key]["weight"] += float(coefficient) * neighbor["weight"]
        mixtures[name] = {"configuration_weights": {key: float(weight) for key, weight in zip(
            gate_receipt["context"]["views"], pi)}, "merged_neighbors": sorted(merged.values(), key=lambda r: (-r["weight"], r["row_id"])),
            "risk_fallback_to_G0": bool(forecasts[name].get("risk_fallback_to_G0", np.zeros(len(values)))[position]),
            "weighted_history": {key: float(forecasts[name][key][position]) if math.isfinite(float(forecasts[name][key][position])) else None
                for key in ("history_mean_return", "history_loss_frequency", "history_lower_decile", "history_loss_component",
                            "history_return_std", "effective_dates")}}
    valid = bool(risk["selected_risk_valid"][position])
    return {"schema_version": "quant.state-company-packet/v1", "symbol": symbol, "as_of": asof,
        "fit_cutoff": boundary, "selection_knowledge_cutoff": "2024-01-01T00:00:00+08:00",
        "frozen_dataset_id": study.data.manifest["frozen_dataset_id"], "cache_sha256": study.cache_hash,
        "original_pool_size": len(symbols), "scoring_position": position,
        "inputs": {key: float(v) if math.isfinite(float(v)) else None for key, v in zip(FEATURES, values[position])},
        "rank_estimates": estimates, "default_rank": selection["default_rank"],
        "risk": {"target": "max(-R20,0)", "source": setup["selected"], "conditional_available": valid,
                 "score": float(risk["risk_raw"][position]) if valid else None,
                 "unconditional_fallback": float(risk["risk_raw"][position]) if not valid else None,
                 "raw_and_calibrated_candidates": {key: float(value[position]) for key, value in risk.items()
                                                    if key.startswith(("raw-", "cal-"))}},
        "configurations": configurations, "mixtures": mixtures, "full_period_results_in_primary_input": False,
        "limitations": ["历史时点模拟，供应商历史vintage和证券资格覆盖限制沿用原冻结数据。",
            "排名、未来损失估计与加权历史类比结果分列；都不是账户收益或交易指令。",
            "当前封包不包含截止日之后的完整研究评价；这些结果在回顾性附件中交付。"]}


def source_note(value):
    risk = value["risk"]
    lines = ["量化研究证据（历史时点模拟）", "证券：" + value["symbol"] + "；评分时点：" + value["as_of"],
        "模型训练截止：" + value["fit_cutoff"] + "；原评分池成员数：" + str(value["original_pool_size"]),
        "目标：下一交易日开盘到第20个后续交易日收盘的参考持有收益排名；不是上涨概率、年度收益或内在价值。",
        "默认排名模型：" + value["default_rank"] + "；选择只使用已成熟的开发期结果。"]
    lines.append("当前14项原始状态输入（null表示缺失，并非零）：" + canonical(value["inputs"]))
    lines.append("检索使用训练历史标准化输入；普通NN为数值欧氏距离，学习配置混合趋势加权余弦与数值距离。"
                 "D²=aT[eta*2(1-cos_v)+(1-eta)*dT²]+其余组数值距离+0.25*任一方缺失比例。"
                 "零或缺失趋势采用中性形态距离。"
                 "每配置取64条精确邻居、每历史日期最多8条；学习配置以日期平衡权重乘exp(-D²/tau²)，普通NN仅日期平衡。")
    for name, configuration in sorted(value["configurations"].items()):
        details = {key: configuration[key] for key in
            ("eta", "tau", "date_count", "effective_dates", "fallback")}
        details["feature_weights"] = dict(zip(FEATURES, configuration["feature_weights"]))
        details["group_weights"] = dict(zip(("T", "R", "L", "M"), configuration["group_weights"]))
        lines.append(name + "距离参数及覆盖：" + canonical(details))
        examples = [{key: row[key] for key in ("date", "symbol", "weight", "squared_distance",
                    "rank_interval", "reference_return")} for row in configuration["neighbors"][:2]]
        lines.append(name + "按距离顺序的前2条历史定位示例（非按收益筛选；完整邻居保留，结果在训练截止前已成熟）："
                     + canonical(examples))
    for name, estimate in sorted(value["rank_estimates"].items()):
        lines.append(f"{name}：目标排名估计 {estimate['target_score']:.6f}，当前完整池排序分位 {estimate['cross_sectional_percentile']:.6f}。")
    lines.append("risk score目标为未来20个交易日 max(-参考持有收益,0) 的条件均值；不是亏损概率或最大回撤。")
    lines.append(f"风险来源：{risk['source']}；" + (f"条件预测 {risk['score']:.6f}（收益率单位）。" if risk["conditional_available"] else
        f"条件风险不可用，无条件回退值 {risk['unconditional_fallback']:.6f}；G1采用G0。"))
    for name, mixture in sorted(value["mixtures"].items()):
        lines.append(name + "配置权重：" + canonical(mixture["configuration_weights"]))
        lines.append(name + "加权历史结果（不冒充校准的未来分布）：" + canonical(mixture["weighted_history"]))
    lines += value["limitations"]
    content = "\n".join(lines)
    return {"id": "QUANT", "use": "research", "analyst_roles": ["Market Analyst"],
            "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "origin": "FinResearchOps quant.state-company-packet/v1 确定性摘要；完整邻居与合并权重保留在量化证据文件",
            "availability_note": "仅使用评分日前输入、训练截止前成熟邻居和开发期选择；不含评分日后的全期间比较。"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    destination = _private_root(args.output)
    study = StateStudy(args.inputs, args.cache)
    result = packet(study, args.date, args.symbol)
    write(destination / "QUANT_EVIDENCE.json", result)
    write(destination / "QUANT_SOURCE.json", source_note(result))


if __name__ == "__main__":
    main()
