from collections import defaultdict
import math
import statistics

from quant.contracts import TARGET_ID, require, session_of
from quant.labels.ranks import average_ranks, percentiles


def spearman(left, right):
    require(len(left) == len(right) and len(left) >= 2, "RANK_IC_SHAPE_INVALID")
    x, y = average_ranks(left), average_ranks(right)
    mx, my = statistics.fmean(x), statistics.fmean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    scale = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / scale if scale else None


def _summary(daily):
    valid = [day["rank_ic"] for day in daily if day["rank_ic"] is not None]
    mean = statistics.fmean(valid) if valid else None
    std = statistics.stdev(valid) if len(valid) >= 2 else None
    return {"days": len(daily), "valid_days": len(valid), "mean_rank_ic": mean,
            "rank_icir": mean / std if std else None,
            "date_equal_mse": statistics.fmean(day["target_mse"] for day in daily)}


def evaluate_predictions(predictions, evaluation, *, quantile_groups=5):
    require(type(quantile_groups) is int and quantile_groups >= 2, "QUANTILE_GROUP_COUNT_INVALID")
    expected = {row.key for row in evaluation.rows}
    require(len(predictions) == len(expected) and {p.key for p in predictions} == expected,
            "PREDICTION_COVERAGE_MISMATCH")
    prediction_map = {p.key: p for p in predictions}
    grouped = defaultdict(list)
    for label in evaluation.labels:
        grouped[label.key.as_of].append(label)
    daily = []
    for as_of, labels in sorted(grouped.items()):
        score = tuple(prediction_map[y.key].predicted_target_percentile for y in labels)
        value = spearman(score, tuple(y.raw_return for y in labels))
        mse = statistics.fmean((s - y.target_percentile) ** 2 for s, y in zip(score, labels))
        bins = defaultdict(list)
        for rank, label in zip(percentiles(score), labels):
            bins[min(int(rank * quantile_groups), quantile_groups - 1)].append(label.raw_return)
        quantiles = [{"group": group + 1, "count": len(bins[group]),
                      "mean_forward_return": statistics.fmean(bins[group]) if bins[group] else None}
                     for group in range(quantile_groups)]
        low, high = quantiles[0]["mean_forward_return"], quantiles[-1]["mean_forward_return"]
        daily.append({"as_of": as_of.isoformat(), "year": session_of(as_of).year,
                      "count": len(labels), "rank_ic": value, "target_mse": mse,
                      "reason": None if value is not None else "CONSTANT_RANKS",
                      "quantiles": quantiles,
                      "top_minus_bottom_forward_return": high - low if low is not None and high is not None else None})
    return summarize_daily(daily, quantile_groups=quantile_groups)


def summarize_daily(daily, *, quantile_groups=5):
    """Combine streamed daily diagnostics without changing date-equal weighting."""
    require(bool(daily), "DAILY_EVALUATION_EMPTY")
    require(len({day["as_of"] for day in daily}) == len(daily), "DUPLICATE_EVALUATION_DATE")
    yearly = defaultdict(list)
    for day in daily:
        yearly[day["year"]].append(day)
    quantile_means = []
    for group in range(quantile_groups):
        values = [day["quantiles"][group]["mean_forward_return"] for day in daily
                  if day["quantiles"][group]["mean_forward_return"] is not None]
        quantile_means.append({"group": group + 1, "days_with_members": len(values),
                              "date_equal_mean_forward_return": statistics.fmean(values) if values else None})
    spreads = [day["top_minus_bottom_forward_return"] for day in daily
               if day["top_minus_bottom_forward_return"] is not None]
    return {"metric": "holding_return_rank_ic", "target_id": TARGET_ID, "daily": daily, **_summary(daily),
            "by_year": [{"year": year, **_summary(days)} for year, days in sorted(yearly.items())],
            "quantile_groups": quantile_groups, "quantile_return_horizon_sessions": 20,
            "quantile_grouping": "AVERAGE_TIE_MODEL_RANK_PERCENTILE_BINS",
            "quantile_returns": quantile_means,
            "date_equal_top_minus_bottom_forward_return": statistics.fmean(spreads) if spreads else None,
            "limitations": ["Overlapping 20-session outcomes are dependent; no IID significance claim.",
                            "Rank IC is predictive association, not realized portfolio performance.",
                            "Quantile returns are overlapping forward-label diagnostics, not daily P&L.",
                            "Tied scores stay in the same bin; bins may be empty or unequal in size."]}
