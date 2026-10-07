"""Full original-pool rank and downside comparisons for the frozen study."""

from datetime import date
from types import SimpleNamespace
import math

from quant.analogue_evaluation import Evaluator, combine
from quant.analogue_study import StudyInputs
from quant.contracts import require
from quant.contracts import fingerprint
from quant.evaluation.prediction import summarize_daily
from quant.evaluation.uncertainty import bound_sensitivity, date_axis_hac, paired_rank_ic_bounds
from quant.models.analogues import file_digest, fusion_scores
from quant.state_meta import RISK_ORDER
from quant.state_study import read, write

RANK_ORDER = ("xgb-full", "xgb-rank", "ordinary", "rank-numeric", "rank-mixed",
              "risk-numeric", "risk-mixed", "G0", "G1")
FUSIONS = ("fusion-0", "fusion-0.25", "fusion-0.5", "fusion-1")
RISK_NAMES = tuple(prefix + name for prefix in ("raw-", "cal-") for name in RISK_ORDER)
RISK_EDGES = (0., .005, .01, .02, .04, .08, .16, float("inf"))


def select_rank(reports, order):
    eligible = [name for name in order if reports[name]["selection_eligible"]]
    require(bool(eligible), "STATE_NO_IDENTIFIED_SELECTION")
    maximum = max(reports[name]["mean_rank_ic_lower_bound"] for name in eligible)
    return next(name for name in eligible if maximum - reports[name]["mean_rank_ic_lower_bound"] <= 1e-12)


def risk_day(prediction, returns, *, text, regime, conditional_valid=None):
    import numpy as np
    prediction, returns = np.asarray(prediction), np.asarray(returns)
    require(prediction.shape == returns.shape and np.isfinite(prediction).all()
            and (prediction >= 0).all(), "STATE_RISK_EVALUATION_SHAPE")
    known = np.isfinite(returns)
    valid = np.ones(len(returns), bool) if conditional_valid is None else np.asarray(conditional_valid)
    require(valid.dtype == bool and valid.shape == returns.shape, "STATE_RISK_VALIDITY_SHAPE")
    base = {"date": text, "market_regime": regime, "count": len(returns),
            "observed_count": int(known.sum()), "unknown_count": int((~known).sum()),
            "conditional_prediction_count": int(valid.sum()), "unconditional_fallback_count": int((~valid).sum())}
    if not known.any():
        return {**base, "mse": None, "mae": None, "bias": None, "moments": None, "bins": []}
    x, y = prediction[known], np.maximum(-returns[known], 0)
    error = x - y
    bins = []
    for lower, upper in zip(RISK_EDGES, RISK_EDGES[1:]):
        rows = (x >= lower) & (x < upper)
        bins.append({"lower": lower, "upper": upper if math.isfinite(upper) else None,
                     "count": int(rows.sum()), "mean_prediction": float(x[rows].mean()) if rows.any() else None,
                     "mean_loss": float(y[rows].mean()) if rows.any() else None})
    return {**base, "mse": float(np.mean(error ** 2)), "mae": float(np.mean(np.abs(error))),
            "bias": float(error.mean()), "bins": bins,
            "moments": [float(v) for v in (x.mean(), y.mean(), (x ** 2).mean(), (y ** 2).mean(), (x * y).mean())]}


def summarize_risk(daily):
    import numpy as np
    known = [x for x in daily if x["mse"] is not None]
    require(bool(daily), "STATE_RISK_DAILY_EMPTY")
    moments = np.mean([x["moments"] for x in known], axis=0) if known else None
    slope, intercept = None, None
    if moments is not None:
        ex, ey, ex2, _, exy = moments
        if ex2 - ex ** 2 > 1e-15:
            slope = float((exy - ex * ey) / (ex2 - ex ** 2))
            intercept = float(ey - slope * ex)
    return {"days": len(daily), "observed_loss_days": len(known),
            "count": sum(x["count"] for x in daily),
            "observed_count": sum(x["observed_count"] for x in daily),
            "unknown_count": sum(x["unknown_count"] for x in daily),
            "conditional_prediction_count": sum(x["conditional_prediction_count"] for x in daily),
            "unconditional_fallback_count": sum(x["unconditional_fallback_count"] for x in daily),
            **{key: float(np.mean([x[key] for x in known])) if known else None for key in ("mse", "mae", "bias")},
            "calibration_slope": slope, "calibration_intercept": intercept,
            "target": "max(-20_session_reference_return,0)", "date_equal": True,
            "scope": "known outcomes only; unknown outcomes never filled", "daily": daily}


def paired_rank(first, second, calendar):
    daily = paired_rank_ic_bounds(first["daily"], second["daily"])
    hac = {}
    for lag in (20, 60):
        lower = {date.fromisoformat(x["as_of"][:10]): x["lower"] for x in daily}
        upper = {date.fromisoformat(x["as_of"][:10]): x["upper"] for x in daily}
        lo, hi = date_axis_hac(lower, calendar, lag=lag), date_axis_hac(upper, calendar, lag=lag)
        hac[str(lag)] = {"lower_endpoint": lo, "upper_endpoint": hi,
                         "outer_approximate_95_interval": [lo["normal_95_interval"][0], hi["normal_95_interval"][1]]}
    return {"daily": daily, "hac": hac,
            "positive_increment_supported": all(v["outer_approximate_95_interval"][0] > 0 for v in hac.values())}


class StateEvaluator:
    def __init__(self, study):
        import numpy as np
        self.study, self.data, self.root = study, study.data, study.root / "evaluation"
        self.root.mkdir(exist_ok=True)
        # Reuse the existing source-label/baseline validators. No old NN cache
        # or score is used by this facade.
        facade = SimpleNamespace(np=np, inputs=study.inputs, output=study.root,
            cache=study.data.manifest, days=study.data.days,
            d_days={x["date"]: x for x in read(study.inputs / "phase-d-inputs-v1/manifest.json")["days"]})
        facade.baseline = lambda text: StudyInputs.baseline(facade, text)
        manifest = read(study.inputs / "execution-matrix-v1/manifest.json")
        require(file_digest(study.inputs / "execution-matrix-v1/views/2024/manifest.json") ==
                manifest["files"]["views/2024/manifest.json"], "STATE_EVALUATION_LABEL_MANIFEST_CHANGED")
        self.original = Evaluator(facade)

    def evaluation_context(self, year, names, selected=None):
        baseline = self.study.inputs / ("phase-c-v2" if year < 2024 else "phase-d-v1") / \
                   f"{year}-xgboost_stock_context"
        receipts = {}
        for name in names:
            if name == "xgb-full":
                path = baseline / ("RECEIPT.json" if year < 2024 else "PREDICTION_RECEIPT.json")
            else:
                key = selected if name.startswith("fusion-") else name
                path = self.study.root / "predictions" / key / str(year) / "RECEIPT.json"
            receipts[name] = file_digest(path)
        return {"cache_sha256": self.study.cache_hash, "protocol_sha256": fingerprint(self.study.protocol),
                "predictions": receipts, "selected_nn": selected if any(x.startswith("fusion-") for x in names) else None}

    def arrays(self, name, text):
        import numpy as np
        folder = self.study.root / "predictions" / name / text[:4]
        receipt = read(folder / "RECEIPT.json")
        path = folder / (text + ".npz")
        require(file_digest(path) == receipt["files"][text], "STATE_EVALUATION_PREDICTION_CHANGED")
        with np.load(path, allow_pickle=False) as saved:
            result = {key: saved[key].copy() for key in saved.files}
        require(all(len(x) == self.data.days[text]["rows"] for x in result.values()),
                "STATE_EVALUATION_FULL_POOL_SHAPE")
        return result

    def annual_rank(self, year, names=RANK_ORDER, *, selected=None):
        import numpy as np
        paths = {name: self.root / f"{year}-{name}.json" for name in names}
        context = self.evaluation_context(year, names, selected)
        if all(path.exists() for path in paths.values()):
            reports = {name: read(path) for name, path in paths.items()}
            require(all(value["input_binding"] == {**context, "predictions": {name: context["predictions"][name]},
                "selected_nn": selected if name.startswith("fusion-") else None}
                        for name, value in reports.items()), "STATE_EVALUATION_CONTEXT_CHANGED")
            require(all(value["daily"] for name, value in reports.items() if name == "G1")
                    and all("risk_fallback_to_G0_count" in day for name, value in reports.items()
                        if name == "G1" for day in value["daily"]),
                    "STATE_EVALUATION_G1_DIAGNOSTIC_REVISION_REQUIRED")
            return reports
        daily = {name: [] for name in names}
        for ordinal, text in enumerate(self.study.dates(year)):
            labels, baseline = self.original.labels(text)
            xgb = np.array([x[2] for x in baseline["samples"]])
            saved = {}
            for name in names:
                output = {}
                if name == "xgb-full":
                    scores = xgb
                else:
                    key = selected if name.startswith("fusion-") else name
                    if key not in saved:
                        saved[key] = self.arrays(key, text)
                    output = saved[key]
                    scores = fusion_scores(xgb, output["scores"], float(name.removeprefix("fusion-")),
                                           output["fallback"]) if name.startswith("fusion-") else output["scores"]
                metric = self.original.metric(scores, labels, text)
                if "fallback" in output:
                    valid = output["fallback"] == 0
                    metric.update(fallback_count=int((~valid).sum()), effective_retrieval_count=int(valid.sum()),
                                  mean_neighbor_date_count=float(output["date_count"][valid].mean()) if valid.any() else None,
                                  mean_effective_dates=float(output["effective_dates"][valid].mean()) if valid.any() else None)
                if "risk_fallback_to_G0" in output:
                    metric["risk_fallback_to_G0_count"] = int(output["risk_fallback_to_G0"].sum())
                daily[name].append(metric)
            if ordinal % 40 == 0:
                print({"stage": "STATE_RANK_EVALUATION", "year": year, "date": text}, flush=True)
        reports = {}
        for name in names:
            report = summarize_daily(daily[name])
            report.update(candidate=name, year=year, all_periods_previously_seen=True,
                          full_original_scoring_pool=True, selected_nn=selected if name.startswith("fusion-") else None,
                          input_binding={**context, "predictions": {name: context["predictions"][name]},
                                         "selected_nn": selected if name.startswith("fusion-") else None})
            write(paths[name], report)
            reports[name] = report
        return reports

    def annual_risk(self, year):
        import numpy as np
        path = self.root / f"{year}-downside.json"
        context = self.evaluation_context(year, ("risk",))
        if path.exists():
            reports = read(path)
            require(all(x["input_binding"] == context for x in reports.values()), "STATE_RISK_EVALUATION_CONTEXT_CHANGED")
            return reports
        daily = {name: [] for name in (*RISK_NAMES, "selected-risk")}
        for text in self.study.dates(year):
            labels, _ = self.original.labels(text)
            returns = np.array([np.nan if x.raw_return is None else x.raw_return for x in labels])
            arrays = self.arrays("risk", text)
            for name in daily:
                prediction = arrays["risk_raw"] if name == "selected-risk" else arrays[name]
                valid = arrays["selected_risk_valid"] if name == "selected-risk" else arrays[
                    "valid-" + name.removeprefix("raw-").removeprefix("cal-")]
                daily[name].append(risk_day(prediction, returns, text=text,
                                           regime=self.data.days[text]["market_regime"], conditional_valid=valid))
        reports = {}
        for name, days in daily.items():
            report = summarize_risk(days)
            report["input_binding"] = context
            report["by_month"] = {month: summarize_risk([x for x in days if x["date"][:7] == month])
                                  for month in sorted({x["date"][:7] for x in days})}
            report["by_fixed_regime"] = {regime: summarize_risk([x for x in days if x["market_regime"] == regime])
                                        for regime in sorted({x["market_regime"] for x in days if x["market_regime"] is not None})}
            reports[name] = report
        write(path, reports)
        return reports

    def comparison(self):
        develop = {year: self.annual_rank(year) for year in range(2018, 2023)}
        merged = {name: combine([develop[y][name] for y in develop]) for name in RANK_ORDER}
        selected_nn = select_rank(merged, RANK_ORDER[2:])
        develop_fusion = {year: self.annual_rank(year, FUSIONS, selected=selected_nn) for year in develop}
        fusion = {name: combine([develop_fusion[y][name] for y in develop]) for name in FUSIONS}
        eligible = {**merged, **fusion}
        selected = select_rank(eligible, (*RANK_ORDER, *FUSIONS))
        # The existing full XGB is retained whenever no strict development
        # replacement is supported by the declared mean lower-bound criterion.
        if eligible[selected]["mean_rank_ic_lower_bound"] <= merged["xgb-full"]["mean_rank_ic_lower_bound"] + 1e-12:
            selected = "xgb-full"
        selection = {"schema_version": "quant.state-selection/v1", "development_years": list(develop),
                     "selected_nn": selected_nn, "default_rank": selected,
                     "criterion": "date-equal mean full-pool Rank IC lower bound", "tolerance": 1e-12,
                     "tie_order": [*RANK_ORDER, *FUSIONS], "check_year_accessed": False,
                     "final_years_accessed": False, "all_periods_previously_seen": True,
                     "development_scores": {name: value["mean_rank_ic_lower_bound"] for name, value in eligible.items()}}
        write(self.study.root / "SELECTION.json", selection)
        check = {y: self.annual_rank(y, (*RANK_ORDER, *FUSIONS), selected=selected_nn) for y in (2023, 2024, 2025)}
        final = {name: combine([check[y][name] for y in (2024, 2025)]) for name in (*RANK_ORDER, *FUSIONS)}
        calendar = [date.fromisoformat(text) for text in self.data.days if "2024-01-01" <= text <= "2025-12-31"]
        detail = {}
        for name, report in final.items():
            defined = report["selection_eligible"] and final["xgb-full"]["selection_eligible"]
            regimes = {r: summarize_daily([x for x in report["daily"] if x["market_regime"] == r])
                       for r in sorted({x["market_regime"] for x in report["daily"] if x["market_regime"] is not None})}
            detail[name] = {"summary": report, "by_fixed_regime": regimes,
                            "hac": bound_sensitivity(report["daily"], calendar) if defined else None,
                            "paired_to_full_xgb": paired_rank(report, final["xgb-full"], calendar) if defined else None}
        gate_pair = paired_rank(final["G1"], final["G0"], calendar) if all(
            final[name]["selection_eligible"] for name in ("G0", "G1")) else None
        risk = {year: self.annual_risk(year) for year in range(2018, 2026)}
        risk_final = {name: summarize_risk([day for year in (2024, 2025) for day in risk[year][name]["daily"]])
                      for name in (*RISK_NAMES, "selected-risk")}
        risk_pairs = {}
        for name in RISK_NAMES:
            a, b = risk_final[name]["daily"], risk_final["cal-constant"]["daily"]
            require([x["date"] for x in a] == [x["date"] for x in b], "STATE_RISK_PAIR_DATES")
            difference = {date.fromisoformat(x["date"]): x["mse"] - y["mse"]
                          for x, y in zip(a, b) if x["mse"] is not None and y["mse"] is not None}
            risk_pairs[name] = {str(lag): date_axis_hac(difference, calendar, lag=lag) for lag in (20, 60)} if difference else None
        result = {"schema_version": "quant.state-comparison/v1", "selection": selection,
                  "development": merged, "development_fusion": fusion, "check_2023": check[2023],
                  "final_2024_2025": detail, "gate_risk_increment": gate_pair,
                  "risk_by_year": risk, "risk_final": risk_final, "risk_paired_mse_to_constant": risk_pairs,
                  "new_blind_test": False, "financial_or_production_validation": False,
                  "training_approximation": True, "full_original_scoring_pool": True}
        write(self.study.root / "COMPARISON.json", result)
        return result
