"""Fixed retrospective comparisons; selection never reads the check/final years."""

import argparse
from datetime import date
import gzip
import json
import math
from pathlib import Path
import pickle
from types import SimpleNamespace

from quant.analogue_study import StudyInputs, read, write_json
from quant.contracts import Prediction, fingerprint, require, validate_label_groups
from quant.evaluation.prediction import evaluate_predictions, summarize_daily
from quant.evaluation.uncertainty import bound_sensitivity, date_axis_hac, paired_rank_ic_bounds
from quant.labels.ranks import percentiles
from quant.models.analogues import file_digest, fusion_scores

CONFIGS = tuple((alpha, k) for alpha in (0., .25, .5) for k in (32, 64))


def candidate(alpha, k):
    return f"a{alpha:g}-k{k}"


class Evaluator:
    def __init__(self, data):
        self.data = data
        self.root = data.output / "evaluation"
        self.root.mkdir(exist_ok=True)
        v = read(data.inputs / "execution-matrix-v1/views/2024/manifest.json")
        self.development_labels = {item["date"]: item for item in v["days"]}

    def labels(self, text):
        if text < "2024-01-01":
            info = self.development_labels[text]
            path = self.data.inputs / "execution-matrix-v1/views/2024/labels" / (text + ".pickle.gz")
            digest = info["labels_file_sha256"]
        else:
            info = self.data.d_days[text]["final_labels"]
            path = self.data.inputs / "phase-d-inputs-v1/final-labels" / (text + ".pickle.gz")
            digest = info["file_sha256"]
        require(file_digest(path) == digest, "ANALOGUE_EVALUATION_LABEL_CONTENT_MISMATCH")
        # These are existing, privately admitted local LabelRow artifacts, never
        # downloaded/untrusted pickle inputs. New prediction files contain no label.
        labels = pickle.loads(gzip.decompress(path.read_bytes()))
        validate_label_groups(labels)
        require(fingerprint(labels) == info["label_set_id"], "ANALOGUE_EVALUATION_LABEL_ID_MISMATCH")
        baseline = self.data.baseline(text)
        require([row[0] for row in baseline["samples"]] == [label.key.symbol for label in labels]
                and len(labels) == self.data.days[text]["rows"], "ANALOGUE_EVALUATION_POOL_MISMATCH")
        return labels, baseline

    def nn(self, text, config):
        np = self.data.np
        folder = self.data.output / "predictions" / config / text[:4]
        receipt = read(folder / "RECEIPT.json")
        path = folder / (text + ".npz")
        require(file_digest(path) == receipt["date_sha256"][text], "ANALOGUE_PREDICTION_CONTENT_MISMATCH")
        with np.load(path, allow_pickle=False) as z:
            return {name: z[name].copy() for name in z.files}

    def metric(self, scores, labels, text):
        predictions = tuple(Prediction(label.key, float(score)) for label, score in zip(labels, scores))
        value = evaluate_predictions(predictions, SimpleNamespace(rows=labels, labels=labels))["daily"][0]
        value["market_regime"] = self.data.days[text]["market_regime"]
        return value

    def annual(self, year, names, selected=None):
        np = self.data.np
        paths = {name: self.root / f"{year}-{name}.json" for name in names}
        if all(path.exists() for path in paths.values()):
            return {name: read(path) for name, path in paths.items()}
        daily = {name: [] for name in names}
        for ordinal, text in enumerate(self.data.evaluation_dates(year)):
            labels, baseline = self.labels(text)
            xgb = np.asarray([row[2] for row in baseline["samples"]])
            cache = {}
            for name in names:
                if name == "xgb":
                    scores = xgb
                    diagnostics = {}
                else:
                    config = selected if name.startswith("fusion-") else name
                    if config not in cache:
                        cache[config] = self.nn(text, config)
                    nn = cache[config]
                    scores = fusion_scores(xgb, nn["scores"], float(name.removeprefix("fusion-")), nn["fallback"]) if name.startswith("fusion-") else nn["scores"]
                    rank_xgb = np.asarray(percentiles(tuple(float(v) for v in xgb)))
                    rank_nn = np.asarray(percentiles(tuple(float(v) for v in nn["scores"])))
                    score_corr = float(np.corrcoef(xgb, nn["scores"])[0, 1]) if np.std(nn["scores"]) and np.std(xgb) else None
                    rank_corr = float(np.corrcoef(rank_xgb, rank_nn)[0, 1]) if np.std(rank_nn) and np.std(rank_xgb) else None
                    diagnostics = {"fallback_count": int((nn["fallback"] != 0).sum()),
                                   "effective_retrieval_count": int((nn["fallback"] == 0).sum()),
                                   "xgb_nn_score_correlation": score_corr, "xgb_nn_rank_correlation": rank_corr,
                                   "mean_absolute_rank_disagreement": float(np.abs(rank_xgb - rank_nn).mean()),
                                   "mean_neighbor_date_count": float(nn["date_count"][nn["fallback"] == 0].mean())
                                   if (nn["fallback"] == 0).any() else None}
                metric = self.metric(scores, labels, text)
                metric.update(diagnostics)
                daily[name].append(metric)
            if ordinal % 40 == 0:
                print(json.dumps({"stage": "EVALUATION_DATE", "year": year, "date": text, "candidates": names}), flush=True)
        result = {}
        for name in names:
            report = summarize_daily(daily[name])
            report.update(candidate=name, year=year, retrospective=True,
                          evidence_status="PREVIOUSLY_SEEN_HISTORICAL_PERIOD")
            if paths[name].exists():
                require(read(paths[name]) == report, "ANALOGUE_EVALUATION_RECOMPUTATION_CHANGED")
            else:
                write_json(paths[name], report)
            result[name] = report
        return result


def combine(reports):
    return summarize_daily([day for report in reports for day in report["daily"]])


def select_fixed(reports, order):
    require(all(reports[name]["selection_eligible"] for name in order), "ANALOGUE_SELECTION_UNDEFINED_METRICS")
    maximum = max(reports[name]["mean_rank_ic_lower_bound"] for name in order)
    return next(name for name in order if maximum - reports[name]["mean_rank_ic_lower_bound"] <= 1e-12)


def final_comparison(data):
    evaluator = Evaluator(data)
    names = ["xgb", *[candidate(alpha, k) for alpha, k in CONFIGS]]
    # This block completes and persists selection before any 2023--2025 metric
    # is evaluated by this study. Forecasts for those years are already fixed.
    develop = {year: evaluator.annual(year, names) for year in range(2018, 2023)}
    merged = {name: combine([develop[year][name] for year in develop]) for name in names}
    nn_order = names[1:]
    selected = select_fixed(merged, nn_order)
    weights = ["fusion-0", "fusion-0.25", "fusion-0.5", "fusion-1"]
    fusion_develop = {year: evaluator.annual(year, weights, selected) for year in range(2018, 2023)}
    fusion_merged = {name: combine([fusion_develop[year][name] for year in fusion_develop]) for name in weights}
    chosen_fusion = select_fixed(fusion_merged, weights)
    selection = {"schema_version": "quant.analogue-selection/v1", "development_years": list(range(2018, 2023)),
                 "selected_nn": selected, "selected_fusion": chosen_fusion,
                 "selected_weight": float(chosen_fusion.removeprefix("fusion-")),
                 "criterion": "date-equal mean Rank IC lower bound", "tolerance": 1e-12,
                 "nn_order": nn_order, "fusion_order": weights, "check_year_accessed": False,
                 "final_years_accessed": False, "all_periods_previously_seen": True,
                 "development_scores": {name: report["mean_rank_ic_lower_bound"] for name, report in {**merged, **fusion_merged}.items()}}
    selection_path = data.output / "SELECTION.json"
    if selection_path.exists():
        require(read(selection_path) == selection, "ANALOGUE_FROZEN_SELECTION_CHANGED")
    else:
        write_json(selection_path, selection)
    checked = {year: evaluator.annual(year, names + weights, selected) for year in (2023, 2024, 2025)}
    final = {name: combine([checked[year][name] for year in (2024, 2025)]) for name in names + weights}
    calendar = [date.fromisoformat(text) for text in data.days if "2024-01-01" <= text <= "2025-12-31"]
    details = {}
    for name, report in final.items():
        paired = paired_rank_ic_bounds(report["daily"], final["xgb"]["daily"])
        hac = {}
        for lag in (20, 60):
            lower = {date.fromisoformat(item["as_of"][:10]): item["lower"] for item in paired}
            upper = {date.fromisoformat(item["as_of"][:10]): item["upper"] for item in paired}
            l, u = date_axis_hac(lower, calendar, lag=lag), date_axis_hac(upper, calendar, lag=lag)
            hac[str(lag)] = {"lower_endpoint": l, "upper_endpoint": u,
                             "outer_approximate_95_interval": [l["normal_95_interval"][0], u["normal_95_interval"][1]]}
        regimes = {}
        for regime in sorted({day["market_regime"] for day in report["daily"] if day["market_regime"] is not None}):
            regimes[regime] = summarize_daily([day for day in report["daily"] if day["market_regime"] == regime])
        details[name] = {"summary": report, "hac": bound_sensitivity(report["daily"], calendar),
                         "paired_to_xgb": {"daily": paired, "hac": hac}, "by_fixed_regime": regimes}
    chosen = final[chosen_fusion]
    pair = details[chosen_fusion]["paired_to_xgb"]
    annual_nonnegative = all(combine([checked[year][chosen_fusion]])["mean_rank_ic_lower_bound"]
                             >= checked[year]["xgb"]["mean_rank_ic_upper_bound"] for year in (2024, 2025))
    retrieved = sum(day["effective_retrieval_count"] for day in chosen["daily"])
    total = sum(day["count"] for day in chosen["daily"])
    supported = (chosen["mean_rank_ic_lower_bound"] > final["xgb"]["mean_rank_ic_upper_bound"]
                 and all(pair["hac"][str(lag)]["outer_approximate_95_interval"][0] > 0 for lag in (20, 60))
                 and annual_nonnegative and retrieved / total >= .95)
    diagnostics = {}
    for name in nn_order:
        days = final[name]["daily"]
        count = math.ceil(len(days) * .1)
        np = data.np
        losses = np.asarray([day["interval_mse"] for day in days])
        base_losses = np.asarray([day["interval_mse"] for day in final["xgb"]["daily"]])
        diagnostics[name] = {"diagnostic_only_not_selection": True,
                             "daily_interval_loss_correlation_to_xgb": float(np.corrcoef(losses, base_losses)[0, 1])
                             if np.std(losses) and np.std(base_losses) else None,
                             "largest_disagreement_dates": sorted(days, key=lambda d: (-d["mean_absolute_rank_disagreement"], d["as_of"]))[:count]}
    comparison = {"schema_version": "quant.analogue-comparison/v1", "selection": selection,
                  "development": merged, "development_fusion": fusion_merged,
                  "check_2023": checked[2023], "final_2024_2025": details, "diagnostics": diagnostics,
                  "retrospective_increment_supported": supported, "default_candidate": chosen_fusion if supported else "xgb",
                  "effective_retrieval_fraction": retrieved / total, "new_blind_test": False,
                  "financial_or_production_validation": False}
    path = data.output / "COMPARISON.json"
    if path.exists():
        require(read(path) == comparison, "ANALOGUE_COMPARISON_CHANGED")
    else:
        write_json(path, comparison)
    print(json.dumps({"stage": "COMPARISON_COMPLETED", "selected_nn": selected,
                      "selected_fusion": chosen_fusion, "default_candidate": comparison["default_candidate"]}), flush=True)
    return comparison


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    final_comparison(StudyInputs(args.inputs, args.output))


if __name__ == "__main__":
    main()
