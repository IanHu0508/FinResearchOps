"""Generate the complete Chinese research report from saved comparisons only."""

import argparse
import json
import statistics
from pathlib import Path

from quant.report_page import page as report_page

from quant.artifacts.store import _private_root
from quant.contracts import require
from quant.models.analogues import file_digest
from quant.state_evaluation import FUSIONS, RANK_ORDER, RISK_NAMES
from quant.evaluation.prediction import summarize_daily
from quant.state_study import read, write


def number(value, digits=6):
    return "无定义" if value is None else format(value, f".{digits}f")


def table(headers, rows):
    def cell(value):
        return str(value).replace("|", "&#124;").replace("\n", " ")
    return ["| " + " | ".join(map(cell, headers)) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(map(cell, row)) + " |" for row in rows], ""]


def render(result, protocol, schedule):
    require(result.get("schema_version") == "quant.state-comparison/v1"
            and result["full_original_scoring_pool"] is True and result["new_blind_test"] is False,
            "STATE_REPORT_COMPARISON_REQUIRED")
    selection = result["selection"]
    names = (*RANK_ORDER, *FUSIONS)
    require(set(result["final_2024_2025"]) == set(names)
            and set(result["risk_by_year"]) == {str(y) for y in range(2018, 2026)}
            and set(result["risk_final"]) == {*RISK_NAMES, "selected-risk"}, "STATE_REPORT_COMPLETE_RESULTS_REQUIRED")
    default = selection["default_rank"]
    chosen = result["final_2024_2025"][default]
    support = chosen["paired_to_full_xgb"]
    supported = support is not None and support["positive_increment_supported"]
    lines = ["# 可学习近邻、下行风险与情景组合：完整固定历史研究", "",
        f"开发期固定的默认排名路径为 **{default}**，所选近邻为 **{selection['selected_nn']}**。",
        "最终历史比较" + ("获得了相对原完整 XGB 的正增量差异支持。" if supported else
        "未获得相对原完整 XGB 的明确正增量支持。") + "默认选择与差异证据分别判定。", "",
        "研究覆盖原 2018—2025 年评分日期和成员。2018—2022 年用于选择，2023 年固定检查，"
        "2024—2025 年完整历史比较。所有时期已见过；时间验证不恢复新盲测身份。", "",
        "## 研究问题与数据边界", "",
        "排名任务预测下一交易日开盘至第 20 个后续交易日收盘的参考持有收益在完整股票池中的相对位置。"
        "未知收益保留在评分和评价股票池中，已知成员的目标以排名区间表示。未知结果不填零、区间不取中点。", "",
        "风险任务单独预测 max(-R20,0) 的条件均值。单位是参考收益率的损失分量，"
        "不是亏损概率、最大回撤、年度回报或账户损益。历史证券资格及供应商 vintage 限制沿用原冻结数据。", "",
        "## 学习与时间控制", "",
        "普通 NN 使用标准化数值欧氏距离及日期平衡等权。RS 对排名区间损失学习特征权重，"
        "RD 对未来损失分量学习特征权重。两者分别比较纯数值和数值/余弦混合结构，学习核带宽。"
        "尺度转换只使用当次训练历史；零或缺失趋势向量使用中性形态距离，双方缺失仍计惩罚。", "",
        f"训练查询上限 {protocol['query_limit']}，每查询候选上限 {protocol['candidate_limit']}；"
        "训练是有记录的计算近似，完整评分池推理使用精确全库检索。"
        f"每次取 {protocol['k']} 个邻居，每历史日期最多 {protocol['date_cap']} 个。"
        "日期权重乘学习距离核；重复证券日期合并权重，所有亏损邻居保留。", "",
        "每个历史半年度块在块首冻结基础拟合、参数选择、风险校准及风险来源。"
        "后续校准和组合训练只连接截止前成熟标签。校准前后均保留，普通 NN、RD 与风险 XGB享有相同校准机会。"
        "G0 使用当时市场状态和趋势，G1 再加入条件风险；条件风险不可用时 G1 使用 G0 的分数和邻居权重。", "",
        f"调度包含 {len(schedule['all_base_cutoffs'])} 个基础模型截止点；共享度量候选尝试上限 "
        f"{schedule['metric_candidate_attempt_upper_bound']}，最终几何重拟合上限 {schedule['final_geometry_refit_upper_bound']}。"
        "相同边界复用产物；有限迭代标记和失败记录保留，这些上限不证明全局最优。", "",
        "## 开发期选择", ""]
    lines += table(("候选", "2018—2022 日均 Rank IC 下界", "选择资格"), [
        (name, number(report["mean_rank_ic_lower_bound"]), report["selection_eligible"])
        for name, report in {**result["development"], **result["development_fusion"]}.items()])
    lines += ["新候选只有共同开发日期的保守下界严格高于原完整 XGB 才能替换默认路径。"
              "相等或更低保留原模型；2023 年及最终历史比较不触发重选。", "", "## 2023 年固定检查", ""]
    lines += table(("候选", "日期数", "IC 下界", "IC 上界", "日期等权区间 MSE"), [
        (name, report["days"], number(report["mean_rank_ic_lower_bound"]), number(report["mean_rank_ic_upper_bound"]),
         number(report["date_equal_interval_mse"])) for name, report in result["check_2023"].items()])
    lines += ["## 2024—2025 年完整排名比较", ""]
    lines += table(("候选", "日期数", "IC 下界", "IC 上界", "区间 MSE", "配对 HAC20 外区间", "配对 HAC60 外区间"), [
        (name, item["summary"]["days"], number(item["summary"]["mean_rank_ic_lower_bound"]),
         number(item["summary"]["mean_rank_ic_upper_bound"]), number(item["summary"]["date_equal_interval_mse"]),
         json.dumps(item["paired_to_full_xgb"]["hac"]["20"]["outer_approximate_95_interval"]) if item["paired_to_full_xgb"] else "无定义",
         json.dumps(item["paired_to_full_xgb"]["hac"]["60"]["outer_approximate_95_interval"]) if item["paired_to_full_xgb"] else "无定义")
        for name, item in result["final_2024_2025"].items()])
    lines += ["排名识别区间来自缺失结果的不确定性；HAC 区间是日期依赖下的抽样误差近似。"
              "它们不是同一种区间，不能把正均值或一次模型选择等同于已证明的预测增量。", "",
              "## 风险目标、校准与覆盖", ""]
    lines += table(("候选", "MSE", "MAE", "偏差", "校准斜率", "已知结果成员日", "未知结果成员日", "条件预测成员日", "无条件回退成员日"), [
        (name, number(report["mse"]), number(report["mae"]), number(report["bias"]), number(report["calibration_slope"]),
         report["observed_count"], report["unknown_count"], report["conditional_prediction_count"], report["unconditional_fallback_count"])
        for name, report in result["risk_final"].items()])
    lines += ["MSE、MAE 与偏差按日期等权、在结果已知的成员上计算，未知结果没有被当作零损失。"
        "条件预测覆盖与无条件回退分列。校准斜率是事后诊断，不能反向进入历史预测。", "",
        "## G1 相对 G0 的风险输入增量", "", json.dumps(result["gate_risk_increment"]["hac"], ensure_ascii=False)
        if result["gate_risk_increment"] else "排名差异在部分日期无定义，不能作明确增量判断。", "",
        "## 年度、月度与固定市场状态", ""]
    for name in names:
        item = result["final_2024_2025"][name]
        development = result["development_fusion"][name] if name in FUSIONS else result["development"][name]
        all_reports = (development, result["check_2023"][name], item["summary"])
        days = [day for report in all_reports for day in report["daily"]]
        if name == "G1":
            require(bool(days) and all("risk_fallback_to_G0_count" in day for day in days),
                    "STATE_REPORT_G1_DIAGNOSTIC_REQUIRED")
        lines += ["### " + name, ""]
        retrieved = [day for day in days if "effective_retrieval_count" in day]
        date_counts = [day["mean_neighbor_date_count"] for day in retrieved if day["mean_neighbor_date_count"] is not None]
        effective_dates = [day["mean_effective_dates"] for day in retrieved if day["mean_effective_dates"] is not None]
        lines += table(("评分成员日", "已知结果", "未知结果", "检索有效", "检索回退", "G1风险回退至G0", "平均邻居日期数", "平均有效日期数"), [(
            sum(day["count"] for day in days), sum(day["observed_count"] for day in days),
            sum(day["unknown_count"] for day in days), sum(day["effective_retrieval_count"] for day in retrieved) if retrieved else "不适用",
            sum(day["fallback_count"] for day in retrieved) if retrieved else "不适用",
            sum(day["risk_fallback_to_G0_count"] for day in days) if name == "G1" else "不适用",
            number(statistics.fmean(date_counts)) if date_counts else "不适用",
            number(statistics.fmean(effective_dates)) if effective_dates else "不适用")])
        lines += table(("年度", "日均 IC 下界", "日均 IC 上界", "区间 MSE"), [
            (row["year"], number(row["mean_rank_ic_lower_bound"]), number(row["mean_rank_ic_upper_bound"]),
             number(row["date_equal_interval_mse"])) for report in all_reports for row in report["by_year"]])
        lines += table(("月份", "日均 IC 下界", "日均 IC 上界", "区间 MSE"), [
            (row["month"], number(row["mean_rank_ic_lower_bound"]), number(row["mean_rank_ic_upper_bound"]),
             number(row["date_equal_interval_mse"])) for report in all_reports for row in report["by_month"]])
        regimes = {regime: summarize_daily([day for day in days if day["market_regime"] == regime])
                   for regime in sorted({day["market_regime"] for day in days if day["market_regime"] is not None})}
        lines += table(("原固定状态", "日期数", "IC 下界", "IC 上界"), [
            (regime, report["days"], number(report["mean_rank_ic_lower_bound"]), number(report["mean_rank_ic_upper_bound"]))
            for regime, report in regimes.items()])
    lines += ["## 风险年度与月度结果", ""]
    for year, reports in result["risk_by_year"].items():
        lines += ["### " + year, ""] + table(("候选", "MSE", "MAE", "偏差", "校准斜率"), [
            (name, number(item["mse"]), number(item["mae"]), number(item["bias"]), number(item["calibration_slope"]))
            for name, item in reports.items()])
        for name, report in reports.items():
            lines += ["#### " + name, ""] + table(("月份", "MSE", "MAE", "偏差", "校准斜率"), [
                (month, number(item["mse"]), number(item["mae"]), number(item["bias"]), number(item["calibration_slope"]))
                for month, item in report["by_month"].items()])
            lines += table(("原固定状态", "MSE", "MAE", "偏差", "校准斜率"), [
                (regime, number(item["mse"]), number(item["mae"]), number(item["bias"]), number(item["calibration_slope"]))
                for regime, item in report["by_fixed_regime"].items()])
    lines += ["## 风险相对校准训练常数的配对 MSE 差异", ""]
    lines += table(("候选", "HAC20 差异均值", "HAC20 近似区间", "HAC60 近似区间"), [
        (name, number(value["20"]["mean"]) if value else "无定义",
         json.dumps(value["20"]["normal_95_interval"]) if value else "无定义",
         json.dumps(value["60"]["normal_95_interval"]) if value else "无定义")
        for name, value in result["risk_paired_mse_to_constant"].items()])
    lines += ["MSE 差异为候选减训练常数，负数表示较小误差；抽样近似与风险校准结果分别解释。", ""]
    lines += ["## 解释与项目使用", "",
        "研究用于检验局部相似状态、风险目标与情景组合是否补充全局树模型。"
        "结果不支持增量时保留对应负结果和既定基线，不删除未胜出的结构，也不将工程测试当作金融效果。"
        "公司主链只接收截止日前可知的输入、成熟邻居和开发期选择；完整最终历史评价另作附件。", "",
        "真实交易成本、可执行组合、当前投资有效性与生产部署均不由本次参考收益研究证明。", "",
        "## 可复现证据", "", "本报告由 COMPARISON.json、PROTOCOL.json 和 SCHEDULE.json 直接生成。"
        "年度成员预测、模型、监督视图、OOF、原失败、输入摘要和明细结果保留在私有研究目录，"
        "旧 A—D 和 V2 产物保持原样。"]
    return "\n".join(lines) + "\n"


def generate(root, destination):
    root, destination = _private_root(root), _private_root(destination)
    result, protocol, schedule = (read(root / name) for name in ("COMPARISON.json", "PROTOCOL.json", "SCHEDULE.json"))
    text = render(result, protocol, schedule)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "QUANT_RESEARCH_REPORT.md"
    if path.exists():
        require(path.read_text() == text, "STATE_REPORT_APPEND_ONLY_CONFLICT")
    else:
        path.write_text(text)
    page = report_page(text, "量化研究完整报告").decode()
    html_path = destination / "QUANT_RESEARCH_REPORT.html"
    if html_path.exists():
        require(html_path.read_text() == page, "STATE_REPORT_APPEND_ONLY_CONFLICT")
    else:
        html_path.write_text(page)
    write(destination / "QUANT_REPORT_RECEIPT.json", {"schema_version": "quant.state-report-receipt/v1",
        "inputs": {name: file_digest(root / name) for name in ("COMPARISON.json", "PROTOCOL.json", "SCHEDULE.json")},
        "outputs": {path.name: file_digest(path), html_path.name: file_digest(html_path)},
        "report_is_current_complete_comparison": True, "financial_or_production_validation": False})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--study", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    generate(Path(args.study), Path(args.output))


if __name__ == "__main__":
    main()
